#!/usr/bin/env python3
"""
microagent — minimal coding agent
Single file, zero dependencies, OpenAI-compatible API (tool calling).

Usage:
    python agent.py                            # interactive REPL
    python agent.py "add error handling"       # one-shot
    WORKDIR=/path/to/project python agent.py   # specify working directory
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


# ── Config ───────────────────────────────────────────────────────────────────

def _load_dotenv():
    env_file = Path(".env")
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val

_load_dotenv()

API_KEY   = os.environ.get("OPENAI_API_KEY", "")
BASE_URL  = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL     = os.environ.get("OPENAI_MODEL", "gpt-4o")
WORKDIR   = Path(os.environ.get("WORKDIR", ".")).resolve()


# ── Cancellation ─────────────────────────────────────────────────────────────

_cancel_event = threading.Event()
_current_conn: http.client.HTTPConnection | None = None

def _sigint_handler(sig, frame):
    global _current_conn
    _cancel_event.set()
    conn = _current_conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    print("\n[interrupted]", flush=True)

signal.signal(signal.SIGINT, _sigint_handler)


# ── HTTP API call ─────────────────────────────────────────────────────────────

def _api_call_impl(messages: list, tools: list) -> tuple[dict | None, str | None]:
    """Send POST /v1/chat/completions, return (data, error)."""
    global _current_conn

    parsed   = urlparse(BASE_URL)
    host     = parsed.hostname
    port     = parsed.port
    path     = parsed.path.rstrip("/") + "/chat/completions"
    use_ssl  = parsed.scheme == "https"

    payload = {"model": MODEL, "messages": messages, "temperature": 0}
    if tools:
        payload["tools"] = tools

    body    = json.dumps(payload).encode()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "Content-Length": str(len(body)),
    }

    if use_ssl:
        import ssl
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port or 443, context=ctx, timeout=300)
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=300)

    _current_conn = conn
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw  = resp.read().decode()
        if resp.status != 200:
            return None, f"HTTP {resp.status}: {raw[:500]}"
        return json.loads(raw), None
    except OSError as e:
        if _cancel_event.is_set():
            return None, "cancelled"
        return None, f"Connection error: {e}"
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    finally:
        _current_conn = None
        try:
            conn.close()
        except Exception:
            pass


def api_call(messages: list, tools: list) -> tuple[dict | None, str | None]:
    """Run API call in a thread so SIGINT can cancel it cleanly."""
    _cancel_event.clear()
    result: list = [None, None]

    def _run():
        result[0], result[1] = _api_call_impl(messages, tools)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    while t.is_alive():
        t.join(timeout=0.1)
        if _cancel_event.is_set():
            t.join(timeout=3)
            return None, "cancelled"

    return result[0], result[1]


# ── Path safety ───────────────────────────────────────────────────────────────

def safe_path(rel: str) -> tuple[Path | None, str | None]:
    try:
        resolved = (WORKDIR / rel).resolve()
    except Exception as e:
        return None, str(e)
    if not resolved.is_relative_to(WORKDIR):
        return None, f"Path traversal denied: {rel!r}"
    return resolved, None


# ── Tools ─────────────────────────────────────────────────────────────────────

def tool_read_file(path: str) -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    if not p.exists():
        return f"Error: file not found: {path}"
    if not p.is_file():
        return f"Error: not a file: {path}"
    try:
        text = p.read_text(errors="replace")
        if len(text) > 50000:
            text = text[:50000] + f"\n... [truncated — total {len(text)} chars]"
        return text
    except Exception as e:
        return f"Error: {e}"


def tool_write_file(path: str, content: str) -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK: wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error: {e}"


def tool_patch_file(path: str, old_str: str, new_str: str) -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        text  = p.read_text(errors="replace")
        count = text.count(old_str)
        if count == 0:
            return f"Error: old_str not found in {path}"
        if count > 1:
            return f"Error: old_str found {count} times — make it more specific"
        p.write_text(text.replace(old_str, new_str, 1))
        return f"OK: patched {path}"
    except Exception as e:
        return f"Error: {e}"


def tool_ls(path: str = ".") -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    if not p.exists():
        return f"Error: path not found: {path}"
    if not p.is_dir():
        return f"Error: not a directory: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        lines   = [e.name + ("/" if e.is_dir() else "") for e in entries]
        return "\n".join(lines) if lines else "(empty)"
    except Exception as e:
        return f"Error: {e}"


def tool_grep(pattern: str, path: str = ".") -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    try:
        res = subprocess.run(
            ["grep", "-rn", pattern, str(p)],
            capture_output=True, text=True, timeout=15, cwd=str(WORKDIR),
        )
        out = res.stdout or res.stderr or "(no matches)"
        if len(out) > 20000:
            out = out[:20000] + "\n... [truncated]"
        return out
    except subprocess.TimeoutExpired:
        return "Error: grep timed out"
    except FileNotFoundError:
        return "Error: grep not available"
    except Exception as e:
        return f"Error: {e}"


# Removed rm, cp, mv, mkdir, cat — destructive or filesystem-escaping.
# shell=False (list form) prevents metacharacter injection ($(), &&, |, ;, etc.)
SHELL_WHITELIST = {
    "find", "wc", "head", "tail",
    "sort", "uniq", "diff", "echo", "pwd", "date",
}

def tool_shell(command: str) -> str:
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"Error: cannot parse command: {e}"

    if not parts:
        return "Error: empty command"

    cmd = parts[0]
    if cmd not in SHELL_WHITELIST:
        allowed = ", ".join(sorted(SHELL_WHITELIST))
        return f"Error: '{cmd}' not allowed. Allowed commands: {allowed}"

    # Extra guard: reject shell metacharacters in raw command string
    if any(c in command for c in '|&;$`><()!'):
        return "Error: shell metacharacters not allowed"

    try:
        res = subprocess.run(
            parts,                   # list — no shell interpretation
            capture_output=True, text=True,
            timeout=15, cwd=str(WORKDIR),
        )
        out = (res.stdout + res.stderr).strip() or "(no output)"
        if len(out) > 20000:
            out = out[:20000] + "\n... [truncated]"
        return out
    except subprocess.TimeoutExpired:
        return "Error: command timed out (15s)"
    except FileNotFoundError:
        return f"Error: command not found: {cmd}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "read_file":  lambda a: tool_read_file(a["path"]),
    "write_file": lambda a: tool_write_file(a["path"], a["content"]),
    "patch_file": lambda a: tool_patch_file(a["path"], a["old_str"], a["new_str"]),
    "ls":         lambda a: tool_ls(a.get("path", ".")),
    "grep":       lambda a: tool_grep(a["pattern"], a.get("path", ".")),
    "shell":      lambda a: tool_shell(a["command"]),
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file relative to WORKDIR. Returns content (max 50000 chars).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path relative to WORKDIR"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file. Creates parent directories automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path relative to WORKDIR"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "Replace exactly one occurrence of old_str with new_str. "
                "Fails if old_str is missing or appears multiple times. "
                "Use new_str='' to delete a fragment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path relative to WORKDIR"},
                    "old_str": {"type": "string", "description": "Exact string to find and replace"},
                    "new_str": {"type": "string", "description": "Replacement string (empty string to delete)"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List files and directories (non-recursive). Directories are suffixed with /.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path (default: .)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Recursive text search. Returns filename:line:content matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path":    {"type": "string", "description": "Directory or file to search (default: .)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a whitelisted shell command. "
                f"Allowed: {', '.join(sorted(SHELL_WHITELIST))}."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command to execute"}},
                "required": ["command"],
            },
        },
    },
]


# ── Worklog ───────────────────────────────────────────────────────────────────

WORKLOG = WORKDIR / ".worklog.md"

def _wlog(text: str):
    with open(WORKLOG, "a") as f:
        f.write(text)

def worklog_session_start(task: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _wlog(f"\n## Session {ts}\n\n**Task:** {task}\n\n")

def worklog_tool(name: str, args: dict, result: str):
    brief_args = ", ".join(
        (f"{k}=<{len(v)} chars>" if k == "content" else f"{k}={repr(v)[:50]}")
        for k, v in args.items()
    )
    summary = result.splitlines()[0][:100] if result else ""
    _wlog(f"- `{name}({brief_args})` → {summary}\n")

def worklog_result(summary: str):
    _wlog(f"\n**Result:** {summary[:300]}\n\n---\n")


# ── Agent loop ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a coding agent working in the directory: {WORKDIR}

You can read, create, and edit files, and run basic shell commands.
Always read a file before editing it.
Use patch_file for targeted changes; use write_file only for new files or full rewrites.
Be precise with patch_file — old_str must match the file exactly, including whitespace.

Function signature rule: whenever you add, remove, or rename a parameter in a function
definition, you MUST use grep to find every call site of that function across the entire
codebase and update each one. Never leave callers out of sync with the new signature.

When done, summarize what you did."""


def _run_task(messages: list) -> str | None:
    """
    Run the agentic loop for the current messages list.
    Returns the final assistant text, or None on cancellation/error.
    """
    while True:
        data, err = api_call(messages, TOOLS_SCHEMA)

        if err == "cancelled":
            return None

        if err:
            print(f"[API error] {err}", file=sys.stderr)
            return None

        choice = data["choices"][0]
        msg    = choice["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls")

        if tool_calls:
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                # Brief display label
                label = args.get("path") or args.get("command") or args.get("pattern") or ""
                print(f"⚡ {name} {label}", flush=True)

                handler = TOOL_HANDLERS.get(name)
                result  = handler(args) if handler else f"Error: unknown tool {name}"

                worklog_tool(name, args, result)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      result,
                })

        else:
            text = msg.get("content") or ""
            return text


# ── Session persistence ───────────────────────────────────────────────────────

SESSIONS_DIR       = WORKDIR / ".sessions"
MAX_CONTEXT_CHARS  = 40_000   # ~10k tokens; offer summarization above this


def _sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR


def session_save(messages: list) -> Path:
    """Write current messages to a timestamped JSON file. Returns path."""
    d   = _sessions_dir()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = d / f"session_{ts}.json"
    payload = {
        "started":      ts,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "model":        MODEL,
        "workdir":      str(WORKDIR),
        "messages":     messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def session_autosave(messages: list, current_path: list):
    """Overwrite current session file (or create one). current_path is a 1-elem list."""
    if not current_path[0]:
        current_path[0] = session_save(messages)
    else:
        payload = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "model":        MODEL,
            "workdir":      str(WORKDIR),
            "messages":     messages,
        }
        current_path[0].write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def session_list() -> list[Path]:
    """Return session files sorted newest-first."""
    d = _sessions_dir()
    return sorted(d.glob("session_*.json"), reverse=True)


def session_load(path: Path) -> list:
    """Load messages from a session file."""
    data = json.loads(path.read_text())
    msgs = data.get("messages", [])
    # Replace system prompt with current one (workdir may have changed)
    if msgs and msgs[0]["role"] == "system":
        msgs[0]["content"] = SYSTEM_PROMPT
    return msgs


def _context_chars(messages: list) -> int:
    """Rough character count of all message content."""
    return sum(
        len(m.get("content") or "") +
        sum(len(tc["function"].get("arguments", "")) for tc in m.get("tool_calls") or [])
        for m in messages
    )


def summarize_session(messages: list) -> str | None:
    """
    Ask the model for a compact summary of the conversation so far.
    Returns the summary string, or None on error.
    """
    summary_request = (
        "Summarize this conversation as concisely as possible. "
        "Include: files touched, changes made, key decisions, and any open issues. "
        "This summary will replace the full history in the next session."
    )
    slim = [m for m in messages if m["role"] in ("system", "user", "assistant")
            and not m.get("tool_calls")]   # skip tool noise for summary
    slim.append({"role": "user", "content": summary_request})

    data, err = api_call(slim, tools=None)
    if err or not data:
        return None
    return data["choices"][0]["message"].get("content", "").strip()


def offer_summarization(messages: list, current_session: list, force: bool = False) -> list:
    """
    Called when context is large. Asks user whether to summarize and restart.
    Returns (possibly new) messages list.
    """
    chars = _context_chars(messages)
    if force:
        print("[summarizing current session…]", flush=True)
    else:
        print(
            f"\n[context is {chars:,} chars (~{chars//4:,} tokens). "
            f"Summarize and start a fresh session? (y/N)] ",
            end="", flush=True,
        )
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return messages
        if answer != "y":
            return messages
        print("[summarizing…]", flush=True)

    print("[summarizing…]", flush=True)
    summary = summarize_session(messages)
    if not summary:
        print("[summarization failed — continuing with full context]\n")
        return messages

    # Archive old session before starting fresh
    session_autosave(messages, current_session)
    old_name = current_session[0].name if current_session[0] else "previous"
    print(f"[archived as {old_name}]\n")
    print("── Summary ─────────────────────────────────────")
    print(summary)
    print("─────────────────────────────────────────────────\n")

    # New session: system prompt + summary as first assistant message
    new_messages = [
        {"role": "system",    "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": f"[Session summary]\n{summary}"},
    ]
    current_session[0] = None   # will get a new file on next autosave
    return new_messages


# ── Interactive mode ──────────────────────────────────────────────────────────

_HELP = """\
Commands:
  /clear          — wipe conversation context (start fresh)
  /sessions       — list saved sessions
  /resume [N]     — resume session N from the list (default: last)
  /save           — force-save current session now
  /summarize      — summarize and compress context right now
  exit / quit     — save and exit

Context is auto-saved after every turn. When it exceeds ~40k chars
you will be asked whether to summarize and start a fresh session.
"""


def run_interactive(resume_path: Path | None = None):
    print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
    print(f"base_url: {BASE_URL}")
    print("Type /help for commands, 'exit' to quit.\n")

    current_session: list[Path | None] = [None]  # mutable ref for autosave

    if resume_path:
        messages = session_load(resume_path)
        current_session[0] = resume_path
        print(f"[resumed {resume_path.name} — {len(messages)-1} messages in context]\n")
    else:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        # Show context size hint when it's getting large
        n = len(messages) - 1
        prompt = f"[{n}]> " if n > 10 else "> "

        try:
            user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            session_autosave(messages, current_session)
            return

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            session_autosave(messages, current_session)
            print(f"[saved {current_session[0].name}]")
            print("Bye!")
            return

        if user_input == "/help":
            print(_HELP)
            continue

        if user_input == "/clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            current_session[0] = None
            print("[context cleared]\n")
            continue

        if user_input == "/save":
            session_autosave(messages, current_session)
            print(f"[saved {current_session[0].name}]\n")
            continue

        if user_input == "/summarize":
            messages = offer_summarization(messages, current_session, force=True)
            continue

        if user_input == "/sessions":
            files = session_list()
            if not files:
                print("(no saved sessions)\n")
            else:
                for i, f in enumerate(files):
                    try:
                        meta = json.loads(f.read_text())
                        nm   = len([m for m in meta.get("messages", []) if m["role"] != "system"])
                        ts   = meta.get("last_updated", f.stem)
                        print(f"  [{i}] {f.name}  {nm} msgs  updated {ts}")
                    except Exception:
                        print(f"  [{i}] {f.name}")
                print()
            continue

        if user_input.startswith("/resume"):
            files = session_list()
            if not files:
                print("[no sessions to resume]\n")
                continue
            parts = user_input.split()
            idx   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if idx >= len(files):
                print(f"[no session {idx}]\n")
                continue
            messages        = session_load(files[idx])
            current_session[0] = files[idx]
            print(f"[resumed {files[idx].name} — {len(messages)-1} messages]\n")
            continue

        # Offer summarization when context grows large
        if _context_chars(messages) > MAX_CONTEXT_CHARS:
            messages = offer_summarization(messages, current_session)

        messages.append({"role": "user", "content": user_input})
        worklog_session_start(user_input)

        text = _run_task(messages)

        if text:
            print(text, flush=True)
            worklog_result(text)

        # Auto-save after every completed turn
        session_autosave(messages, current_session)


def run_oneshot(task: str):
    print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
    print(f"base_url: {BASE_URL}\n", flush=True)

    worklog_session_start(task)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": task},
    ]

    text = _run_task(messages)
    if text:
        print(text, flush=True)
        worklog_result(text)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    # --resume [path|index]
    if args and args[0] == "--resume":
        files = session_list()
        if not files:
            print("No saved sessions found.")
            sys.exit(1)
        if len(args) > 1:
            ref = args[1]
            if ref.isdigit():
                idx = int(ref)
                resume = files[idx] if idx < len(files) else None
            else:
                resume = Path(ref)
        else:
            resume = files[0]   # last session
        run_interactive(resume_path=resume)

    elif args:
        run_oneshot(" ".join(args))

    else:
        run_interactive()
