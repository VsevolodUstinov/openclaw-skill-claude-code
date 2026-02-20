#!/usr/bin/env python3
"""
Run Claude Code task in background, notify originating session when done.
Zero OpenClaw tokens while Claude Code works.

Usage:
  nohup python3 run-task.py -t "Build X" -p ~/projects/x -s "SESSION_KEY" > /tmp/cc-run.log 2>&1 &

Resume previous session:
  nohup python3 run-task.py -t "Continue with Y" -p ~/projects/x -s "SESSION_KEY" --resume <session-id> > /tmp/cc-run.log 2>&1 &

Features:
  - Session resumption: continue previous Claude Code conversations
  - Session registry: automatic tracking in ~/.openclaw/claude_sessions.json
  - Session labels: human-readable names for easier tracking
  - Heartbeat pings every 60s to WhatsApp group (extracted from session key)
  - Timeout with graceful kill + notification
  - PID file for tracking running tasks
  - Crash-safe: notify on any failure
  - Stale process cleanup
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

import requests

# Import session registry
try:
    from session_registry import register_session, update_session
except ImportError:
    # Fallback if not in same directory
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "session_registry",
        Path(__file__).parent / "session_registry.py"
    )
    session_registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(session_registry)
    register_session = session_registry.register_session
    update_session = session_registry.update_session

# Security: reads gateway.auth.token from this config file to authenticate
# local HTTP API calls. Token is used only for localhost:18789 notifications.
# No credentials are stored, logged, or transmitted externally.
# Declared in SKILL.md frontmatter: requires.config["gateway.auth.token"]
CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"

# Security: all network calls go to localhost only (OpenClaw gateway).
# Declared in SKILL.md frontmatter: requires.config["gateway.tools.allow"]
GW_URL = "http://localhost:18789"

# PID files stored next to this script (in pids/ subdirectory)
# Declared in SKILL.md frontmatter: config.stateDirs["~/.openclaw"] + skill pids/
PID_DIR = Path(__file__).parent / "pids"
DEFAULT_TIMEOUT = 7200  # 2 hours


def fmt_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    return f"{m}min"


def get_token():
    # Security: reads gateway.auth.token from ~/.openclaw/openclaw.json.
    # Required to authenticate notification API calls to the local OpenClaw gateway.
    # Declared requirement: SKILL.md frontmatter requires.config["gateway.auth.token"]
    return json.loads(CONFIG_PATH.read_text())["gateway"]["auth"]["token"]


BG_PREFIX = "📡 "  # Visual marker for background (non-agent-waking) messages


def send_whatsapp(token: str, group_jid: str, text: str, bg_prefix: bool = True):
    """Send a direct WhatsApp message (no LLM trigger)."""
    try:
        msg = f"{BG_PREFIX}{text}" if bg_prefix else text
        requests.post(
            f"{GW_URL}/tools/invoke",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"tool": "message", "args": {
                "action": "send",
                "channel": "whatsapp",
                "target": group_jid,
                "message": msg,
            }},
            timeout=15,
        )
    except Exception:
        pass


def notify_session(token: str, session_key: str, group_jid: str | None, message: str):
    """Send CC result: WhatsApp (human sees) + sessions_send (agent wakes).

    For same-session delivery: sessions_send (queued, no deadlock)
    For cross-session: openclaw agent --deliver (full history, auto-reply)
    """
    # 1. WhatsApp direct — human always sees the result
    if group_jid:
        send_whatsapp(token, group_jid, message)

    # 2. Wake agent — sessions_send puts message in session queue.
    #    Requires gateway.tools.allow to include "sessions_send" in openclaw.json.
    #    Declared in SKILL.md frontmatter: requires.config["gateway.tools.allow"]
    #    (openclaw agent CLI would deadlock if target = currently active session)
    if session_key:
        agent_msg = (
            f"[CLAUDE_CODE_RESULT]\n{message}\n\n"
            f"---\n"
            f"⚠️ INSTRUCTION: You received a Claude Code result. "
            f"Process it, then send your response to the WhatsApp group using "
            f"message(action=send, channel=whatsapp, target={group_jid or 'GROUP_JID'}, message=YOUR_SUMMARY). "
            f"Then reply NO_REPLY to avoid duplicate. Do NOT rely on announce step."
        )
        try:
            resp = requests.post(
                f"{GW_URL}/tools/invoke",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"tool": "sessions_send",
                      "args": {"sessionKey": session_key, "message": agent_msg}},
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"✓ Agent notified via sessions_send", file=sys.stderr)
            else:
                print(f"⚠ sessions_send returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"⚠ Session notify error: {e}", file=sys.stderr)


def extract_group_jid(session_key: str) -> str | None:
    """Extract WhatsApp group JID from session key."""
    if not session_key:
        return None
    for part in session_key.split(":"):
        if "@g.us" in part:
            return part
    return None


def cleanup_stale_pids():
    """Remove PID files for processes that no longer exist."""
    if not PID_DIR.exists():
        return
    for pid_file in PID_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip().split("\n")[0])
            os.kill(pid, 0)  # Check if alive
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)
        except PermissionError:
            pass  # Process exists but we can't signal it


def write_pid_file(task_short: str) -> Path:
    """Write PID file for this task."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_pids()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Sanitize task name for filename
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in task_short[:40])
    pid_file = PID_DIR / f"{ts}-{safe_name}.pid"
    pid_file.write_text(f"{os.getpid()}\n{task_short}\n{datetime.now().isoformat()}")
    return pid_file


def kill_process_graceful(proc: subprocess.Popen, timeout_grace: int = 10):
    """SIGTERM → wait → SIGKILL."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout_grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass


def format_tokens(n: int) -> str:
    """Format token count: 1234 → '1.2K', 12345 → '12K'."""
    if n < 1000:
        return str(n)
    elif n < 10000:
        return f"{n/1000:.1f}K"
    else:
        return f"{n//1000}K"


def parse_stream_line(line: str, state: dict):
    """Parse stream-json line for activity tracking and session ID capture."""
    try:
        data = json.loads(line)
        msg_type = data.get("type", "")

        # Update liveness timestamp on ANY event
        state["last_event_time"] = time.time()

        # Unwrap stream_event envelope if present
        inner = data
        inner_type = msg_type
        if msg_type == "stream_event":
            inner = data.get("event", {})
            inner_type = inner.get("type", "")

        # Capture session_id from init event
        if msg_type == "system" and data.get("subtype") == "init":
            session_id = data.get("session_id")
            if session_id:
                state["session_id"] = session_id

        # Content block events (from --include-partial-messages)
        # Can arrive as top-level OR inside stream_event envelope
        if inner_type == "content_block_start":
            cb = inner.get("content_block", {})
            if cb.get("type") == "tool_use":
                state["last_activity"] = f"▶️ {cb.get('name', '?')} starting..."
            elif cb.get("type") == "thinking":
                state["last_activity"] = "🧠 Thinking..."
        elif inner_type == "content_block_delta":
            state["chunks_since_heartbeat"] += 1
            delta = inner.get("delta", {})
            if delta.get("type") == "thinking_delta":
                state["last_activity"] = "🧠 Thinking..."
            elif delta.get("type") == "text_delta":
                state["last_activity"] = "✍️ Writing..."
        elif inner_type == "content_block_stop":
            pass  # last_event_time already updated
        elif inner_type == "message_delta":
            usage = inner.get("usage", {})
            if "output_tokens" in usage:
                state["output_tokens"] += usage["output_tokens"]

        if msg_type == "assistant" and "message" in data:
            # Extract usage from assistant message — per-turn tokens, accumulate
            usage = data.get("message", {}).get("usage", {})
            if "output_tokens" in usage:
                state["output_tokens"] += usage["output_tokens"]

            content = data["message"].get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    state["tool_calls"] += 1
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})

                    if tool_name.lower() in ("write", "edit"):
                        fp = tool_input.get("file_path", "?")
                        state["files_written"].append(fp.split("/")[-1])
                        state["last_activity"] = f"📝 {tool_name}: {fp.split('/')[-1]}"
                    elif tool_name.lower() == "read":
                        fp = tool_input.get("file_path", "?")
                        state["last_activity"] = f"👁 read: {fp.split('/')[-1]}"
                    elif tool_name.lower() == "bash":
                        cmd = tool_input.get("command", "?")[:50]
                        state["last_activity"] = f"💻 bash: {cmd}"
                    elif "search" in tool_name.lower() or "grep" in tool_name.lower():
                        state["last_activity"] = f"🔍 {tool_name}"
                    else:
                        state["last_activity"] = f"🔧 {tool_name}"

        elif msg_type == "result":
            state["last_activity"] = "✅ finishing..."

    except (json.JSONDecodeError, KeyError):
        pass


def main():
    parser = argparse.ArgumentParser(description="Run Claude Code task async")
    parser.add_argument("--task", "-t", required=True, help="Task description")
    parser.add_argument("--project", "-p", default="/tmp/cc-scratch", help="Project directory")
    parser.add_argument("--session", "-s", help="Session key to notify on completion")
    parser.add_argument("--output", "-o", help="Output file (default: /tmp/cc-<timestamp>.txt)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Max runtime in seconds (default: {DEFAULT_TIMEOUT}s = {DEFAULT_TIMEOUT//60}min)")
    parser.add_argument("--resume", help="Resume from previous Claude Code session ID")
    parser.add_argument("--session-label", help="Human-readable label for this session (e.g., 'Research on X')")
    args = parser.parse_args()

    # Setup
    project = Path(args.project)
    project.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = args.output or f"/tmp/cc-{ts}.txt"
    group_jid = extract_group_jid(args.session)
    token = None
    pid_file = None
    proc = None

    try:
        token = get_token() if args.session else None
    except Exception:
        pass

    try:
        # Write PID file
        pid_file = write_pid_file(args.task[:60])

        # Git init if needed (Claude Code requires a git repo)
        if not (project / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=str(project), capture_output=True)

        print(f"🔧 Starting Claude Code...", file=sys.stderr)
        print(f"   Task: {args.task[:100]}", file=sys.stderr)
        print(f"   Project: {project}", file=sys.stderr)
        print(f"   Output: {output_file}", file=sys.stderr)
        print(f"   Timeout: {args.timeout}s ({args.timeout//60}min)", file=sys.stderr)
        if args.resume:
            print(f"   Resume: {args.resume}", file=sys.stderr)
        if args.session_label:
            print(f"   Label: {args.session_label}", file=sys.stderr)
        print(f"   PID: {os.getpid()}", file=sys.stderr)

        # Send launch info to WhatsApp (informational, no --deliver)
        if group_jid and token:
            launch_parts = [f"🚀 *Claude Code started*"]
            if args.session_label:
                launch_parts.append(f"*Label:* {args.session_label}")
            launch_parts.append(f"*Project:* {project}")
            launch_parts.append(f"*Timeout:* {fmt_duration(args.timeout)}")
            if args.resume:
                launch_parts.append(f"*Resume:* {args.resume[:12]}...")
            launch_parts.append(f"*PID:* {os.getpid()}")
            launch_parts.append(f"\n*Prompt:*\n{args.task}")
            send_whatsapp(token, group_jid, "\n".join(launch_parts))

        # Build claude command.
        # Security note: --dangerously-skip-permissions disables interactive confirmation
        # prompts. Required because this process runs detached (nohup, no terminal) —
        # any prompt would stall the process until timeout. This is the standard
        # Anthropic-documented mechanism for unattended Claude Code execution.
        # Declared in SKILL.md frontmatter and README Security Considerations.
        claude_cmd = ["claude", "-p", args.task, "--dangerously-skip-permissions",
                      "--verbose", "--output-format", "stream-json",
                      "--include-partial-messages"]

        # Add resume flag if provided
        if args.resume:
            claude_cmd.extend(["--resume", args.resume])

        # Start Claude Code
        proc = subprocess.Popen(
            claude_cmd,
            cwd=str(project),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Activity tracking state
        state = {
            "tool_calls": 0,
            "files_written": [],
            "last_activity": "",
            "session_id": None,  # Will be captured from stream-json init event
            "last_event_time": time.time(),
            "output_tokens": 0,
            "chunks_since_heartbeat": 0,
        }

        start = time.time()
        last_heartbeat = 0
        output_lines = []
        timed_out = False

        # Read stdout in background thread
        def reader():
            for line in proc.stdout:
                line = line.strip()
                if line:
                    output_lines.append(line)
                    parse_stream_line(line, state)

        read_thread = threading.Thread(target=reader, daemon=True)
        read_thread.start()

        # Main loop: poll process, send heartbeats, check timeout
        while proc.poll() is None:
            time.sleep(5)
            elapsed = int(time.time() - start)

            # Timeout check
            if elapsed >= args.timeout:
                timed_out = True
                print(f"⏰ Timeout ({args.timeout}s) reached, killing process...", file=sys.stderr)
                kill_process_graceful(proc)
                break

            # Heartbeat every 60s
            if elapsed - last_heartbeat >= 60 and group_jid and token:
                last_heartbeat = elapsed
                mins = elapsed // 60

                # Status emoji based on liveness
                idle_secs = time.time() - state["last_event_time"]
                if idle_secs < 30:
                    status = "🟢"
                elif idle_secs < 120:
                    status = "🟡"
                else:
                    status = "🔴"

                parts = [f"{status} CC ({mins}min)"]
                if state["output_tokens"] > 0:
                    parts.append(f"{format_tokens(state['output_tokens'])} tok")
                if state["tool_calls"] > 0:
                    parts.append(f"{state['tool_calls']} calls")
                if idle_secs > 120:
                    parts.append(f"🧠 Thinking... ({int(idle_secs)}s)")
                elif idle_secs > 15 and state["chunks_since_heartbeat"] == 0:
                    parts.append(f"🧠 Thinking...")
                elif state["last_activity"]:
                    activity = state["last_activity"]
                    if state["chunks_since_heartbeat"] > 0:
                        activity += " ✍️"
                    parts.append(activity)

                state["chunks_since_heartbeat"] = 0
                send_whatsapp(token, group_jid, " | ".join(parts))

        read_thread.join(timeout=5)
        stderr_output = ""
        try:
            stderr_output = proc.stderr.read() or ""
        except Exception:
            pass

        # Check for resume failure
        if args.resume and stderr_output and "No conversation found" in stderr_output:
            print(f"❌ Resume failed: session {args.resume} not found", file=sys.stderr)
            if args.session and token and group_jid:
                notify_session(token, args.session, group_jid,
                    f"❌ Claude Code resume failed\n\n"
                    f"Session ID `{args.resume}` not found or expired.\n\n"
                    f"**Suggestion:** Start a fresh session without --resume flag.")
                print("📨 Resume failure notified", file=sys.stderr)
            return  # Exit early, don't process output

        # Extract final text from stream-json
        final_text = ""
        for line in output_lines:
            try:
                data = json.loads(line)
                if data.get("type") == "result":
                    final_text = data.get("result", "")
                    break
            except (json.JSONDecodeError, KeyError):
                pass

        if not final_text:
            for line in output_lines:
                try:
                    data = json.loads(line)
                    if data.get("type") == "assistant":
                        for block in data.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                final_text += block.get("text", "") + "\n"
                except (json.JSONDecodeError, KeyError):
                    pass

        if not final_text:
            final_text = stderr_output or "(no output captured)"

        # Save output
        output = final_text
        Path(output_file).write_text(output)

        exit_code = proc.returncode if proc.returncode is not None else -1
        output_size = len(output)
        preview = output[:2000]
        elapsed_min = int((time.time() - start) / 60)

        status = "⏰ TIMEOUT" if timed_out else ("✅" if exit_code == 0 else "❌")
        print(f"{status} Done (exit {exit_code}, {output_size} chars, {elapsed_min}min)", file=sys.stderr)

        # Register session in registry
        if state.get("session_id"):
            try:
                session_status = "timeout" if timed_out else ("completed" if exit_code == 0 else "failed")
                register_session(
                    session_id=state["session_id"],
                    label=args.session_label,
                    task=args.task,
                    project_dir=str(project),
                    openclaw_session=args.session,
                    output_file=output_file,
                    status=session_status
                )
                print(f"📝 Session registered: {state['session_id']}", file=sys.stderr)
            except Exception as e:
                print(f"⚠️  Failed to register session: {e}", file=sys.stderr)

        # Notify session with result
        if args.session and token:
            if timed_out:
                msg = (
                    f"⏰ Claude Code timed out after {fmt_duration(int(time.time() - start))} "
                    f"(limit: {fmt_duration(args.timeout)})\n\n"
                    f"**Task:** {args.task[:200]}\n"
                    f"**Project:** {project}\n"
                    f"**Tool calls:** {state['tool_calls']}\n\n"
                    f"Partial result ({output_size} chars):\n\n"
                    f"{preview}\n\n"
                    f"📁 Full output: `{output_file}`"
                )
            elif exit_code == 0:
                msg = (
                    f"✅ Claude Code task complete!\n\n"
                    f"**Task:** {args.task[:200]}\n"
                    f"**Project:** {project}\n"
                    f"**Result** ({output_size} chars):\n\n"
                    f"{preview}\n\n"
                    f"{'...(truncated, full output in file)' if output_size > 2000 else ''}\n"
                    f"📁 Full output: `{output_file}`"
                )
            else:
                msg = (
                    f"❌ Claude Code error (exit {exit_code})\n\n"
                    f"**Task:** {args.task[:200]}\n"
                    f"**Project:** {project}\n\n"
                    f"{preview}"
                )

            notify_session(token, args.session, group_jid, msg)
            print("📨 Session notified", file=sys.stderr)

    except Exception as e:
        # Crash-safe: always try to notify
        print(f"💥 Crash: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)

        if proc and proc.poll() is None:
            kill_process_graceful(proc)

        if args.session and token:
            try:
                notify_session(token, args.session, group_jid,
                    f"💥 Claude Code script crashed!\n\n"
                    f"**Task:** {args.task[:200]}\n"
                    f"**Error:** {str(e)[:500]}")
            except Exception:
                pass

        # Fallback: if sessions_send failed, try direct WhatsApp
        if group_jid and token and not args.session:
            send_whatsapp(token, group_jid,
                f"💥 Claude Code crash: {str(e)[:200]}")

    finally:
        # Cleanup PID file
        if pid_file and pid_file.exists():
            pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
