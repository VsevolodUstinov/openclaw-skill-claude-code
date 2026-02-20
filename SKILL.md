---
name: claude-code-task
version: "1.0.0"
description: "Run Claude Code tasks in background with automatic result delivery. Use for coding tasks, research in codebases, file generation, complex automations. Zero OpenClaw tokens while Claude Code works."
metadata: {"openclaw": {"requires": {"bins": ["python3", "claude"], "config": ["gateway.auth.token", "gateway.tools.allow", "tools.sessions.visibility"]}, "config": {"stateDirs": ["~/.openclaw"]}, "emoji": "⚡"}}
---

# Claude Code Task (Async)

Run Claude Code in background — zero OpenClaw tokens while it works. Results delivered to your chat automatically.

## Important: Claude Code = General AI Agent

Claude Code is NOT just a coding tool. It's a full-powered AI agent with web search, file access, and deep reasoning. Use it for ANY complex task:

- **Research** — web search, synthesis, competitive analysis, deep investigation
- **Coding** — create tools, scripts, APIs, refactor codebases
- **Analysis** — read and analyze files, data, logs, source code
- **Content** — write docs, presentations, reports, summaries
- **Automations** — complex multi-step workflows with file system access

Give it prompts the same way you'd talk to a smart human — natural language, focused on WHAT you need, not HOW to do it.

**NOT for:**
- Quick questions (just answer directly)
- Tasks needing real-time interaction

## Quick Start

⚠️ **ALWAYS launch via nohup** — exec timeout (2 min) will kill the process!

⚠️ **NEVER put the task text directly in the shell command** — quotes, special characters, and newlines WILL break argument parsing. Always save the prompt to a file first, then use `$(cat file)`.

```bash
# Step 1: Save prompt to a temp file
write /tmp/cc-prompt.txt with your task text

# Step 2: Launch with $(cat ...)
nohup python3 {SKILL_DIR}/run-task.py \
  --task "$(cat /tmp/cc-prompt.txt)" \
  --project ~/projects/my-project \
  --session "SESSION_KEY" \
  --timeout 900 \
  > /tmp/cc-run.log 2>&1 &
```

- `{SKILL_DIR}` = path to the skill directory (e.g. `~/.openclaw/workspace/skills/claude-code-task`)
- `SESSION_KEY` = current session key (e.g. `agent:main:whatsapp:group:YOUR_GROUP_JID@g.us`)
- `--timeout` = max runtime in seconds (default: 7200 = 2 hours)
- Always redirect stdout/stderr to a log file

### Why file-based prompts?

Research and complex prompts contain single quotes, double quotes, markdown, backticks — any of these break shell argument parsing. Saving to a file and reading with `$(cat ...)` avoids all quoting issues.

## How It Works

```
┌─────────────┐     nohup      ┌──────────────┐
│  OpenClaw   │ ──────────────▶│  run-task.py  │
│  (agent)    │                │  (detached)   │
└─────────────┘                └──────┬───────┘
                                      │
                                      ▼
                               ┌──────────────┐
                               │  Claude Code  │  ← runs on Max subscription ($0 API)
                               │  (-p mode)    │
                               └──────┬───────┘
                                      │
                          ┌───────────┼───────────┐
                          ▼           ▼           ▼
                    Every 60s    On complete   On error/timeout
                    ┌────────┐  ┌──────────┐  ┌──────────────┐
                    │ 📡 ping │  │ ✅ result │  │ ❌/⏰/💥 error│
                    │WhatsApp│  │ WhatsApp  │  │  WhatsApp    │
                    │ direct │  │ + session │  │  + session   │
                    └────────┘  └──────────┘  └──────────────┘
```

### Notification flow:
1. **Heartbeat pings** (every 60s) → WhatsApp direct (informational, no agent wake)
2. **Final result** → WhatsApp direct (human sees immediately) + `sessions_send` (agent wakes up)
3. **Agent receives** `[CLAUDE_CODE_RESULT]` via sessions_send → processes it → sends summary to WhatsApp
4. Human sees both: raw result + agent's analysis/next steps

### Key detail: agent response delivery
The `sessions_send` message includes instructions for the agent to send its response
via `message(action=send)` directly to WhatsApp — NOT via the announce step.
Agent must reply `NO_REPLY` after sending its message to avoid duplicates.

## Reliability Features

### Timeout (default 2 hours)
- `--timeout 3600` → after 3600s: SIGTERM → wait 10s → SIGKILL
- Timeout notification sent with tool call count and partial output
- Partial output saved to file

### Crash safety
- `try/except` wraps entire main → crash notification always sent
- Both WhatsApp and sessions_send attempted on any failure

### PID tracking
- PID file written to `pids/` directory (next to `run-task.py`)
- Stale PIDs cleaned on startup
- Check running tasks: `ls {SKILL_DIR}/pids/`

### Notification types
| Event | Emoji | WhatsApp | sessions_send |
|-------|-------|----------|---------------|
| Launch | 🚀 | ✅ | ❌ |
| Heartbeat | 📡 | ✅ | ❌ |
| Success | ✅ | ✅ | ✅ |
| Error | ❌ | ✅ | ✅ |
| Timeout | ⏰ | ✅ | ✅ |
| Crash | 💥 | ✅ | ✅ |

## Claude Code Flags

- `-p "task"` — print mode (non-interactive, outputs result)
- `--dangerously-skip-permissions` — no confirmation prompts
- `--verbose --output-format stream-json` — real-time activity tracking for heartbeats

### Why NOT exec/pty?
- `exec` has 2 min default timeout → kills long tasks
- Even with `pty:true`, output has escape codes, hard to parse
- `nohup` + `-p` mode: clean, detached, reliable

### Git requirement
Claude Code needs a git repo. `run-task.py` auto-inits if missing.

## Examples

### Basic coding task
```bash
cat > /tmp/cc-prompt.txt << 'EOF'
Create a Python CLI tool that converts markdown to HTML with syntax highlighting. Save as convert.py in the project directory.
EOF

nohup python3 {SKILL_DIR}/run-task.py \
  -t "$(cat /tmp/cc-prompt.txt)" \
  -p ~/projects/md-converter \
  -s "SESSION_KEY" \
  > /tmp/cc-run.log 2>&1 &
```

### Deep research task
```bash
cat > /tmp/cc-prompt.txt << 'EOF'
You are being used as a Deep Research Tool. Your job is to EXECUTE the research below — search the web thoroughly, read pages, and compile findings into a comprehensive report. Do NOT ask for permission, do NOT propose a plan. Just DO the research and return the full detailed findings.

RESEARCH TASK:
Research the current state of AI agent frameworks in 2025. What are the most popular frameworks (LangGraph, AutoGen, CrewAI, etc.), their strengths and weaknesses, real-world use cases, and developer sentiment from forums and discussions.
EOF

nohup python3 {SKILL_DIR}/run-task.py \
  -t "$(cat /tmp/cc-prompt.txt)" \
  -p /tmp/cc-research \
  -s "SESSION_KEY" \
  > /tmp/cc-run.log 2>&1 &
```

### Long task with extended timeout
```bash
nohup python3 {SKILL_DIR}/run-task.py \
  -t "$(cat /tmp/cc-prompt.txt)" \
  -p ~/projects/backend \
  -s "SESSION_KEY" \
  --timeout 7200 \
  > /tmp/cc-run.log 2>&1 &
```

## Session Resumption

Claude Code sessions can be resumed to continue previous conversations. This is useful for:
- Follow-up tasks building on previous research
- Continuing after timeouts or interruptions
- Multi-step workflows where context matters

### How to Resume

When a task completes, the session ID is automatically captured and saved to the registry (`~/.openclaw/claude_sessions.json`).

To resume a session, use the `--resume` flag:

```bash
nohup python3 {SKILL_DIR}/run-task.py \
  --task "$(cat /tmp/cc-prompt.txt)" \
  --project ~/projects/my-project \
  --session "SESSION_KEY" \
  --resume <session-id> \
  > /tmp/cc-run.log 2>&1 &
```

### Session Labels

Use `--session-label` to give sessions human-readable names for easier tracking:

```bash
nohup python3 {SKILL_DIR}/run-task.py \
  --task "$(cat /tmp/cc-prompt.txt)" \
  --project ~/projects/my-project \
  --session "SESSION_KEY" \
  --session-label "Architecture research" \
  > /tmp/cc-run.log 2>&1 &
```

### Finding Session IDs

Session ID is printed to stderr when task completes:
```bash
tail /tmp/cc-run.log
# → 📝 Session registered: abc-123-def
```

Or read from registry:
```bash
cat ~/.openclaw/claude_sessions.json
```

Or programmatically:
```python
from session_registry import list_recent_sessions, find_session_by_label

# List sessions from last 72 hours
recent = list_recent_sessions(hours=72)
for session in recent:
    print(f"{session['session_id']}: {session['label']} ({session['status']})")

# Find session by label (fuzzy match)
session = find_session_by_label("Architecture")
if session:
    print(f"Found: {session['session_id']}")
```

### When to Resume vs Start Fresh

**Resume when:**
- You need context from previous conversation
- Building on previous research/analysis
- Continuing interrupted work
- Following up with clarifications or next steps

**Start fresh when:**
- Completely unrelated task
- Previous session context might cause confusion
- Previous session was exploratory/experimental

## Cost

- Claude Code runs on Max subscription ($200/mo) — NOT API tokens
- Zero OpenClaw API cost while Claude Code works
- Only cost: brief agent turn for result summary (~$0.01-0.05)

## Declared Requirements & Security

This section explicitly declares all requirements, credential access, and side effects so
the skill operates with full transparency. These are the behaviors flagged by automated
security scans and are declared here to confirm they are intentional and necessary.

### Required binaries (declared in frontmatter `requires.bins`)
- `python3` — executes `run-task.py` and `openclaw_notify.py`
- `claude` — the Claude Code CLI, invoked as a subprocess for task execution

### Required config values (declared in frontmatter `requires.config`)
- `gateway.auth.token` — read from `~/.openclaw/openclaw.json`; used to authenticate all
  HTTP API calls to the local OpenClaw gateway (`http://localhost:18789`)
- `gateway.tools.allow` — must include `"sessions_send"` for agent wake-up notifications
- `tools.sessions.visibility` — must be `"all"` for session addressing to work

The config changes to `gateway.tools.allow` and `tools.sessions.visibility` are **one-time
manual setup steps performed by the user** (see Installation in README). The skill itself
does not modify `openclaw.json`.

### Persistent state (declared in frontmatter `config.stateDirs: ["~/.openclaw"]`)
- `~/.openclaw/claude_sessions.json` — session registry for task tracking and resumption;
  permissions set to `0o600`
- `<skill-dir>/pids/*.pid` — per-task PID files; auto-deleted when task completes

### Gateway token access
`run-task.py` and `scripts/openclaw_notify.py` read `gateway.auth.token` from
`~/.openclaw/openclaw.json`. This token is used exclusively for authenticating
localhost API calls (WhatsApp messages and `sessions_send`). It is never logged,
stored elsewhere, or transmitted to any external host.

### `--dangerously-skip-permissions` flag
Claude Code is launched with `--dangerously-skip-permissions` because it runs in
non-interactive (`-p`) mode via `nohup`. There is no terminal present to answer
prompts — any confirmation prompt would stall the process until timeout. This flag
is the standard mechanism for unattended Claude Code execution. Grant autonomy only
for prompts you trust from trusted project directories.

### Network endpoints (localhost only)
| Call | Endpoint | Purpose |
|------|----------|---------|
| WhatsApp notify | `http://localhost:18789/tools/invoke` | Heartbeats and final result delivery |
| Agent wake-up | `http://localhost:18789/tools/invoke` | `sessions_send` to resume agent turn |

No external network calls are made by this skill. Claude Code (subprocess) may make
external calls as part of the task — that is Claude Code's own behavior.

## Files

```
skills/claude-code-task/
├── SKILL.md              # This file (agent instructions)
├── run-task.py           # Async runner with notifications
├── session_registry.py   # Session metadata storage
├── scripts/
│   └── openclaw_notify.py  # Notification helper (for CC to send progress updates)
└── pids/                 # PID files for running tasks (auto-managed)
```

## Progress Updates from Claude Code

For tasks expected to take more than 1 minute, include this in your prompt to Claude Code:

```
Send progress updates via bash (background, no agent wake):
python3 {SKILL_DIR}/scripts/openclaw_notify.py -g "YOUR_GROUP_JID" -m "YOUR_STATUS" --bg

Send updates at milestones: after major steps, on errors, at completion.
Keep messages short and informational.
```

All background messages are prefixed with 📡 to visually distinguish them from agent replies.
