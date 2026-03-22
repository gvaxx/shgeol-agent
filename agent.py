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


def run_interactive():
    print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
    print(f"base_url: {BASE_URL}")
    print("Type 'exit' or 'quit' to quit, Ctrl+C to cancel current request.\n")

    while True:
        try:
            task = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return

        if task.lower() in ("exit", "quit"):
            print("Bye!")
            return
        if not task:
            continue

        worklog_session_start(task)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": task},
        ]

        text = _run_task(messages)
        if text:
            print(text, flush=True)
            worklog_result(text)


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
    if len(sys.argv) > 1:
        run_oneshot(" ".join(sys.argv[1:]))
    else:
        run_interactive()
