#!/usr/bin/env python3
"""
microagent — minimal coding agent
Single file, zero dependencies, OpenAI-compatible API (JSON tool parsing).

Usage:
    python agent.py                            # interactive REPL
    python agent.py "add error handling"       # one-shot
    WORKDIR=/path/to/project python agent.py   # specify working directory
"""

import ast as _ast
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import readline  # noqa: F401 — enables arrow keys, history, and line editing in input()
except ImportError:
    pass  # Windows without pyreadline — input() still works, just no arrow keys


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

API_KEY        = os.environ.get("OPENAI_API_KEY", "")
BASE_URL       = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL          = os.environ.get("OPENAI_MODEL", "gpt-4o")
WORKDIR        = Path(os.environ.get("WORKDIR", ".")).resolve()
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "50"))


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

def _api_call_impl(messages: list) -> tuple[dict | None, str | None]:
    """Send POST /v1/chat/completions, return (data, error)."""
    global _current_conn

    parsed   = urlparse(BASE_URL)
    host     = parsed.hostname
    port     = parsed.port
    path     = parsed.path.rstrip("/") + "/chat/completions"
    use_ssl  = parsed.scheme == "https"

    payload = {"model": MODEL, "messages": messages, "temperature": 0}

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


def api_call(messages: list) -> tuple[dict | None, str | None]:
    """Run API call in a thread so SIGINT can cancel it cleanly."""
    _cancel_event.clear()
    result: list = [None, None]

    def _run():
        result[0], result[1] = _api_call_impl(messages)

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


_MAX_WRITE_CHARS = 2_000_000   # 2 MB safety limit

def tool_write_file(path: str, content: str) -> str:
    p, err = safe_path(path)
    if err:
        return f"Error: {err}"
    if len(content) > _MAX_WRITE_CHARS:
        return f"Error: content too large ({len(content):,} chars, limit {_MAX_WRITE_CHARS:,})"
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
        exclude_args = [f"--exclude-dir={d}" for d in _SKIP_DIRS]
        res = subprocess.run(
            ["grep", "-rn", "--"] + exclude_args + [pattern, str(p)],
            capture_output=True, text=True, timeout=15, cwd=str(WORKDIR),
        )
        out = res.stdout or res.stderr.strip() or "(no matches)"
        # Strip absolute WORKDIR prefix so model gets relative paths
        out = out.replace(str(WORKDIR) + "/", "")
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

    # Ensure any absolute-path arguments stay within WORKDIR
    for arg in parts[1:]:
        if arg.startswith("/") or arg.startswith("~"):
            try:
                resolved = Path(arg).expanduser().resolve()
            except Exception:
                return f"Error: invalid path argument: {arg!r}"
            if not resolved.is_relative_to(WORKDIR):
                return f"Error: path argument outside WORKDIR: {arg!r}"

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

# Tools are described in the system prompt; the model outputs JSON to invoke them.
TOOLS_PROMPT = f"""
## Tools

To call a tool, output ONLY a JSON object — no text before or after it:
{{"tool": "TOOL_NAME", "args": {{"param": "value"}}}}

After receiving the result you may call another tool or give your final answer as plain text.

Available tools:

read_file   — Read a file (max 50k chars)
  args: path (string, required)

write_file  — Create or overwrite a file (max 2 MB)
  args: path (string, required), content (string, required)

patch_file  — Replace exactly one occurrence of old_str with new_str; use new_str="" to delete
  args: path (string, required), old_str (string, required), new_str (string, required)

ls          — List directory contents (non-recursive)
  args: path (string, optional, default ".")

grep        — Recursive text search; returns file:line:content matches
  args: pattern (string, required), path (string, optional, default ".")

shell       — Run a whitelisted command: {' '.join(sorted(SHELL_WHITELIST))}
  args: command (string, required)
"""


def _parse_tool_call(content: str) -> dict | None:
    """
    Try to extract a tool call from model output.
    Accepts bare JSON or a ```json ... ``` fenced block.
    Returns {{"tool": str, "args": dict}} or None.
    """
    candidates = [content.strip()]

    # Also try to extract from a fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())

    for text in candidates:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "tool" in obj:
                return {"tool": obj["tool"], "args": obj.get("args", {})}
        except (json.JSONDecodeError, ValueError):
            pass

    return None


# ── Worklog ───────────────────────────────────────────────────────────────────

WORKLOG = WORKDIR / ".worklog.md"

def _wlog(text: str):
    with open(WORKLOG, "a") as f:
        f.write(text)

def worklog_session_open(mode: str, resumed_from: "Path | None" = None):
    """Write the opening header once per agent run."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"\n## Session {ts}  [{mode} | model: {MODEL}]\n"
    if resumed_from:
        header += f"_Resumed: {resumed_from.name}_\n"
    header += "\n"
    _wlog(header)

def worklog_session_close():
    _wlog("---\n")

def worklog_turn(task: str):
    """Log one user turn inside an open session."""
    _wlog(f"**Turn:** {task}\n\n")

def worklog_tool(name: str, args: dict, result: str):
    brief_args = ", ".join(
        (f"{k}=<{len(v)} chars>" if k == "content" else f"{k}={repr(v)[:50]}")
        for k, v in args.items()
    )
    summary = result.splitlines()[0][:100] if result else ""
    _wlog(f"- `{name}({brief_args})` → {summary}\n")

def worklog_result(summary: str):
    _wlog(f"\n→ {summary[:300]}\n\n")

# kept for one-shot compatibility
def worklog_session_start(task: str):
    worklog_session_open("one-shot")
    worklog_turn(task)


# ── Agent loop ────────────────────────────────────────────────────────────────

CONTEXT_FILE = WORKDIR / ".agent_context.md"


def load_repo_context() -> str:
    """Return injected context string if .agent_context.md exists, else ''."""
    if CONTEXT_FILE.exists():
        try:
            content = CONTEXT_FILE.read_text().strip()
            return f"\n\n---\n{content}\n---"
        except Exception:
            pass
    return ""


_SYSTEM_BASE = f"""You are a coding agent working in the directory: {WORKDIR}

Always read a file before editing it.
Use patch_file for targeted changes; use write_file only for new files or full rewrites.
Be precise with patch_file — old_str must match the file exactly, including whitespace.

Function signature rule: whenever you add, remove, or rename a parameter in a function
definition, you MUST use grep to find every call site of that function across the entire
codebase and update each one. Never leave callers out of sync with the new signature.

When done, respond with plain text summarizing what you did.
{TOOLS_PROMPT}"""


def build_system_prompt() -> str:
    """Base prompt + repo context if .agent_context.md exists."""
    return _SYSTEM_BASE + load_repo_context()


# Keep SYSTEM_PROMPT as an alias used by session-load (overwritten at session start)
SYSTEM_PROMPT = build_system_prompt()


def _run_task(messages: list) -> str | None:
    """
    Run the agentic loop for the current messages list.
    The model signals tool calls by outputting a JSON object; we parse and execute it.
    Returns the final assistant text, or None on cancellation/error.
    """
    tool_call_count = 0

    while True:
        data, err = api_call(messages)

        if err == "cancelled":
            return None

        if err:
            print(f"[API error] {err}", file=sys.stderr)
            return None

        choice  = data["choices"][0]
        msg     = choice["message"]
        content = msg.get("content") or ""
        messages.append(msg)

        tc = _parse_tool_call(content)

        if tc and tool_call_count < MAX_TOOL_CALLS:
            tool_call_count += 1
            name = tc["tool"]
            args = tc["args"]

            label = args.get("path") or args.get("command") or args.get("pattern") or ""
            print(f"⚡ {name} {label}", end="", flush=True)

            handler = TOOL_HANDLERS.get(name)
            try:
                result = handler(args) if handler else f"Error: unknown tool {name!r}"
            except KeyError as e:
                result = f"Error: missing required argument {e}"
            except Exception as e:
                result = f"Error: {e}"

            # Inline result summary
            first_line = result.splitlines()[0] if result else ""
            if result.startswith("Error") or result.startswith("OK"):
                print(f"  → {first_line}", flush=True)
            else:
                print(f"  → {result.count(chr(10)) + 1} lines, {len(result)} chars", flush=True)

            worklog_tool(name, args, result)

            messages.append({
                "role":    "user",
                "content": f"[tool result: {name}]\n{result}",
            })

        elif tc and tool_call_count >= MAX_TOOL_CALLS:
            print(f"[max tool calls ({MAX_TOOL_CALLS}) reached — stopping]", file=sys.stderr)
            return content

        else:
            return content


# ── Repo context (.agent_context.md) ─────────────────────────────────────────
#
# Inspired by aider's repo-map and Cline's Memory Bank:
# - scan_repo()  builds a compact file map using Python ast (no deps)
# - /init        scan + ask model to write purpose & architecture notes
# - /update      ask model to revise context based on current session
# - /reinit      rescan + ask model to reconcile map with existing notes
# The resulting .agent_context.md is injected into the system prompt on start.

_SKIP_DIRS  = {".git", "__pycache__", "venv", ".venv", ".sessions",
               "node_modules", "dist", "build", ".tox", ".mypy_cache"}
_SKIP_FILES = {".agent_context.md", ".worklog.md"}


def _func_sig(node) -> str:
    """Reconstruct function signature string from AST node (no body)."""
    args   = node.args
    parts  = []
    n_args = len(args.args)
    n_def  = len(args.defaults)

    for i, arg in enumerate(args.args):
        if arg.arg == "self":
            continue
        di = i - (n_args - n_def)
        if di >= 0:
            try:
                default = _ast.unparse(args.defaults[di])
                parts.append(f"{arg.arg}={default}")
            except Exception:
                parts.append(arg.arg)
        else:
            parts.append(arg.arg)

    for arg in args.posonlyargs:
        parts.insert(0, arg.arg)

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for kwarg in args.kwonlyargs:
        parts.append(kwarg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    prefix = "async def" if isinstance(node, _ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(parts)})"


def _scan_py(path: Path) -> dict:
    """Parse one .py file → {docstring, defs:[{kind,sig/name,doc,line,methods?}]}."""
    try:
        tree = _ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return {"docstring": None, "defs": []}

    docstring = _ast.get_docstring(tree)
    defs      = []

    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            defs.append({
                "kind": "func",
                "sig":  _func_sig(node),
                "doc":  (_ast.get_docstring(node) or "").split("\n")[0][:80],
                "line": node.lineno,
            })
        elif isinstance(node, _ast.ClassDef):
            methods = []
            for item in _ast.iter_child_nodes(node):
                if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    methods.append({
                        "sig":  _func_sig(item),
                        "doc":  (_ast.get_docstring(item) or "").split("\n")[0][:80],
                        "line": item.lineno,
                    })
            defs.append({
                "kind":    "class",
                "name":    node.name,
                "doc":     (_ast.get_docstring(node) or "").split("\n")[0][:80],
                "line":    node.lineno,
                "methods": methods,
            })

    return {"docstring": docstring, "defs": defs}


def scan_repo() -> str:
    """
    Walk WORKDIR, extract structure from .py files via ast,
    list other notable files. Returns a compact markdown file-map string.
    """
    lines = []
    other = []   # non-python notable files

    for root, dirs, files in os.walk(WORKDIR):
        root_path = Path(root)
        # Prune ignored dirs in-place
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))

        rel_root = root_path.relative_to(WORKDIR)

        for fname in sorted(files):
            fpath    = root_path / fname
            rel_path = str(fpath.relative_to(WORKDIR))

            if fname in _SKIP_FILES or fname.startswith("."):
                continue

            if fname.endswith(".py"):
                info = _scan_py(fpath)
                header = rel_path
                if info["docstring"]:
                    header += f" — {info['docstring'].split(chr(10))[0][:80]}"
                lines.append(f"\n{header}")

                for d in info["defs"]:
                    if d["kind"] == "func":
                        doc_hint = f"  # {d['doc']}" if d["doc"] else ""
                        lines.append(f"  {d['sig']}{doc_hint}")
                    elif d["kind"] == "class":
                        doc_hint = f"  # {d['doc']}" if d["doc"] else ""
                        lines.append(f"  class {d['name']}{doc_hint}")
                        for m in d["methods"]:
                            mdoc = f"  # {m['doc']}" if m["doc"] else ""
                            lines.append(f"    {m['sig']}{mdoc}")
            else:
                # Include non-python files as a flat list (skip binaries by ext)
                _skip_exts = {".pyc", ".pyo", ".jpg", ".jpeg", ".png", ".gif",
                              ".ico", ".woff", ".woff2", ".ttf", ".eot",
                              ".db", ".sqlite", ".sqlite3", ".lock"}
                if fpath.suffix.lower() not in _skip_exts:
                    other.append(rel_path)

    result = "## File map\n" + "\n".join(lines)
    if other:
        result += "\n\n## Other files\n" + "\n".join(f"  {p}" for p in other)
    return result


def _ask_for_context(file_map: str, existing: str | None = None) -> str | None:
    """Ask the model to produce a .agent_context.md given the file map."""
    if existing:
        prompt = (
            "Below is an updated file map of the repository, followed by the "
            "existing .agent_context.md.\n\n"
            "Produce a revised .agent_context.md that:\n"
            "1. Updates the File Map section verbatim from the new scan below.\n"
            "2. Preserves and lightly updates the Purpose and Architecture Notes "
            "sections based on any new/changed/removed files.\n"
            "Output ONLY the markdown content, starting with '# Repo Context'.\n\n"
            f"=== NEW FILE MAP ===\n{file_map}\n\n"
            f"=== EXISTING CONTEXT ===\n{existing}"
        )
    else:
        prompt = (
            "Below is a file map of a software repository extracted with Python ast.\n"
            "Write a .agent_context.md with exactly these three sections:\n\n"
            "# Repo Context\n\n"
            "## Purpose\n"
            "<2-4 sentences: what this repo does, its main goal>\n\n"
            "## Architecture Notes\n"
            "<bullet points: key modules, how they fit together, important patterns>\n\n"
            "## File Map\n"
            "<paste the file map below verbatim>\n\n"
            "Output ONLY the markdown, no commentary.\n\n"
            f"{file_map}"
        )

    msgs = [
        {"role": "system",  "content": "You are a technical documentation writer."},
        {"role": "user",    "content": prompt},
    ]
    data, err = api_call(msgs)
    if err or not data:
        return None
    return data["choices"][0]["message"].get("content", "").strip()


def _update_context_from_session(messages: list) -> str | None:
    """Ask model to revise .agent_context.md based on what happened in this session."""
    existing = CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else "(none)"
    prompt = (
        "Based on the conversation above, update the .agent_context.md below.\n"
        "Revise the Purpose and Architecture Notes if anything changed.\n"
        "Update the File Map only for files that were created or modified.\n"
        "Output ONLY the full revised markdown, starting with '# Repo Context'.\n\n"
        f"=== CURRENT .agent_context.md ===\n{existing}"
    )
    msgs = list(messages) + [{"role": "user", "content": prompt}]
    data, err = api_call(msgs)
    if err or not data:
        return None
    return data["choices"][0]["message"].get("content", "").strip()


# ── Session persistence ───────────────────────────────────────────────────────

SESSIONS_DIR       = WORKDIR / ".sessions"
MAX_CONTEXT_CHARS  = int(os.environ.get("MAX_CONTEXT_CHARS", 1_000_000))  # ~256k tokens
MAX_SESSIONS_KEEP  = int(os.environ.get("MAX_SESSIONS_KEEP", 50))


def _sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR


def _first_user_msg(messages: list) -> str:
    """Return the first user message content (truncated), for session previews."""
    for m in messages:
        if m.get("role") == "user":
            text = (m.get("content") or "").replace("\n", " ").strip()
            return text[:80] + ("…" if len(text) > 80 else "")
    return ""


def _rotate_sessions(d: Path):
    """Delete oldest session files beyond MAX_SESSIONS_KEEP."""
    files = sorted(d.glob("session_*.json"), reverse=True)
    for old in files[MAX_SESSIONS_KEEP:]:
        try:
            old.unlink()
        except Exception:
            pass


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
        "preview":      _first_user_msg(messages),
        "messages":     messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    _rotate_sessions(d)
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
            "preview":      _first_user_msg(messages),
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
        msgs[0]["content"] = build_system_prompt()
    return msgs


def _context_chars(messages: list) -> int:
    """Rough character count of all message content."""
    return sum(len(m.get("content") or "") for m in messages)


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
    slim = [m for m in messages if m["role"] in ("system", "user", "assistant")]
    slim.append({"role": "user", "content": summary_request})

    data, err = api_call(slim)
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
        {"role": "system",    "content": build_system_prompt()},
        {"role": "assistant", "content": f"[Session summary]\n{summary}"},
    ]
    current_session[0] = None   # will get a new file on next autosave
    return new_messages


# ── Interactive mode ──────────────────────────────────────────────────────────

_HELP = """\
Commands:
  /init           — scan repo, generate .agent_context.md (injected every session)
  /reinit         — rescan repo + reconcile with existing .agent_context.md
  /update         — revise .agent_context.md based on this session's changes
  /sessions       — list saved sessions with preview
  /resume [N]     — resume session N from the list (default: last)
  /save           — force-save current session now
  /summarize      — summarize and compress context right now
  /clear          — wipe conversation context (start fresh)
  exit / quit     — save and exit

CLI flags (one-shot or interactive):
  --model MODEL   — override OPENAI_MODEL
  --url URL       — override OPENAI_BASE_URL
  --resume [N]    — resume last (or Nth) session at startup
  --init          — generate .agent_context.md and exit

.agent_context.md is injected into the system prompt on every session start.
Context auto-saves after every turn. When it exceeds MAX_CONTEXT_CHARS
you will be asked whether to summarize and start a fresh session.
"""


def _handle_context_command(cmd: str, messages: list):
    """/init | /reinit | /update — generate or refresh .agent_context.md."""
    if cmd in ("/init", "/reinit"):
        print("[scanning repo…]", flush=True)
        file_map = scan_repo()

        existing = None
        if cmd == "/reinit" and CONTEXT_FILE.exists():
            existing = CONTEXT_FILE.read_text()
            print("[reconciling with existing context…]", flush=True)
        else:
            print("[generating context with AI…]", flush=True)

        content = _ask_for_context(file_map, existing)
        if not content:
            print("[failed to generate context — check API]\n")
            return

        # Ensure it starts with the right header
        if not content.startswith("# Repo Context"):
            content = "# Repo Context\n\n" + content

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = f"{content}\n\n_Generated: {ts}_\n"
        CONTEXT_FILE.write_text(content)
        print(f"[written {CONTEXT_FILE.name} — {len(content)} chars]\n")

    elif cmd == "/update":
        if not CONTEXT_FILE.exists():
            print("[no .agent_context.md found — run /init first]\n")
            return
        print("[updating context based on this session…]", flush=True)
        content = _update_context_from_session(messages)
        if not content:
            print("[failed — check API]\n")
            return
        if not content.startswith("# Repo Context"):
            content = "# Repo Context\n\n" + content
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = f"{content}\n\n_Updated: {ts}_\n"
        CONTEXT_FILE.write_text(content)
        print(f"[updated {CONTEXT_FILE.name}]\n")


def _refresh_system_msg(messages: list) -> list:
    """Replace the first system message with a freshly built system prompt."""
    new_sys = build_system_prompt()
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = new_sys
    else:
        messages.insert(0, {"role": "system", "content": new_sys})
    return messages


def run_interactive(resume_path: Path | None = None):
    print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
    print(f"base_url: {BASE_URL}")
    if CONTEXT_FILE.exists():
        print(f"[context: {CONTEXT_FILE.name} loaded]")
    print("Type /help for commands, 'exit' to quit.\n")

    current_session: list[Path | None] = [None]  # mutable ref for autosave

    if resume_path:
        messages = session_load(resume_path)
        messages = _refresh_system_msg(messages)   # inject latest context
        current_session[0] = resume_path
        print(f"[resumed {resume_path.name} — {len(messages)-1} messages in context]\n")
        worklog_session_open("interactive", resumed_from=resume_path)
    else:
        messages = [{"role": "system", "content": build_system_prompt()}]
        worklog_session_open("interactive")

    while True:
        # Show context size hint when it's getting large
        n = len(messages) - 1
        prompt = f"[{n}]> " if n > 10 else "> "

        try:
            user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            session_autosave(messages, current_session)
            worklog_session_close()
            return

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            session_autosave(messages, current_session)
            worklog_session_close()
            print(f"[saved {current_session[0].name}]")
            print("Bye!")
            return

        if user_input == "/help":
            print(_HELP)
            continue

        if user_input in ("/init", "/reinit", "/update"):
            _handle_context_command(user_input, messages)
            messages = _refresh_system_msg(messages)
            continue

        if user_input == "/clear":
            messages = [{"role": "system", "content": build_system_prompt()}]
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
                        meta    = json.loads(f.read_text())
                        nm      = len([m for m in meta.get("messages", []) if m["role"] != "system"])
                        ts      = meta.get("last_updated", f.stem)[:16]
                        preview = meta.get("preview") or _first_user_msg(meta.get("messages", []))
                        print(f"  [{i}] {ts}  {nm:>3} msgs  {preview}")
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
        worklog_turn(user_input)

        text = _run_task(messages)

        if text:
            print(text, flush=True)
            worklog_result(text)

        # Auto-save after every completed turn
        session_autosave(messages, current_session)


def run_oneshot(task: str):
    print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
    print(f"base_url: {BASE_URL}\n", flush=True)

    worklog_session_open("one-shot")
    worklog_turn(task)
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user",   "content": task},
    ]

    text = _run_task(messages)
    if text:
        print(text, flush=True)
        worklog_result(text)
    worklog_session_close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = list(sys.argv[1:])

    # Parse --model and --url overrides before anything else
    def _pop_flag(flag: str) -> str | None:
        for i, a in enumerate(args):
            if a == flag and i + 1 < len(args):
                args.pop(i); return args.pop(i)
            if a.startswith(flag + "="):
                args.pop(i); return a.split("=", 1)[1]
        return None

    if (v := _pop_flag("--model")):
        MODEL = v
    if (v := _pop_flag("--url")):
        BASE_URL = v.rstrip("/")

    # --init: generate .agent_context.md then exit
    if args and args[0] == "--init":
        print(f"microagent  |  workdir: {WORKDIR}  |  model: {MODEL}")
        msgs_dummy: list = []   # no session context needed for init
        _handle_context_command("/init", msgs_dummy)
        sys.exit(0)

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
