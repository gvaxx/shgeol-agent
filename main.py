#!/usr/bin/env python3
"""
agent.py — unified LLM chat + coding agent web application.

Launch:
    python agent.py --work-dir /path/to/project --port 8080
"""

import argparse
import ast as _ast
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue

# ── Config ───────────────────────────────────────────────────────────────────

def _load_dotenv():
    for env_file in (Path(__file__).resolve().parent / ".env", Path(".env")):
        if not env_file.exists():
            continue
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
        break

_load_dotenv()

WORKDIR: Path = Path(os.environ.get("WORKDIR", ".")).resolve()
MAX_TOOL_CALLS: int = int(os.environ.get("MAX_TOOL_CALLS", "50"))
AGENT_DATA_PATH: Path = Path(
    os.environ.get("AGENT_DATA_PATH", str(Path(__file__).resolve().parent / "agent_data.json"))
)

_SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "node_modules",
    "dist", "build", ".tox", ".mypy_cache", ".sessions",
    ".egg-info", ".eggs", ".pytest_cache", ".ruff_cache",
}

_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".sqlite", ".db", ".sqlite3",
}

_SHELL_WHITELIST = {"find", "wc", "head", "tail", "sort", "uniq", "diff", "echo", "pwd", "date"}
_SHELL_META = set(";&|`$(){}><\n\\!")

# ── Path safety ──────────────────────────────────────────────────────────────

def safe_path(rel: str, workdir: Path = None) -> Path:
    """Resolve *rel* relative to workdir (default: global WORKDIR). Block directory traversal."""
    wd = workdir if workdir is not None else WORKDIR
    resolved = (wd / rel).resolve()
    if not (str(resolved) == str(wd) or str(resolved).startswith(str(wd) + os.sep)):
        raise ValueError(f"Path escapes workdir: {rel}")
    return resolved


# ── Tools ────────────────────────────────────────────────────────────────────

def tool_read_file(path: str, workdir: Path = None) -> str:
    p = safe_path(path, workdir)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    content = p.read_text(errors="replace")
    truncated = ""
    if len(content) > 50_000:
        content = content[:50_000]
        truncated = f"\n... [truncated at 50k chars]"
    lines = content.splitlines()
    numbered = "\n".join(f"{i+1:4}: {line}" for i, line in enumerate(lines))
    return numbered + truncated


def tool_write_file(path: str, content: str, workdir: Path = None) -> str:
    if len(content) > 2_000_000:
        return "ERROR: content exceeds 2MB limit"
    p = safe_path(path, workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: wrote {len(content)} chars to {path}"


def tool_patch_file(path: str, old_str: str, new_str: str, workdir: Path = None) -> str:
    p = safe_path(path, workdir)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    text = p.read_text(errors="replace")
    count = text.count(old_str)
    if count == 0:
        return f"ERROR: old_str not found in {path}. Make sure whitespace and indentation matches exactly."
    if count > 1:
        return f"ERROR: old_str appears {count} times in {path} (must be exactly 1)"
    text = text.replace(old_str, new_str, 1)
    p.write_text(text)
    return f"OK: patched {path}"


def tool_edit_lines(path: str, start: int, end: int, new_content: str, workdir: Path = None) -> str:
    """Replace lines start..end (1-indexed, inclusive) with new_content."""
    p = safe_path(path, workdir)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    lines = p.read_text(errors="replace").splitlines(keepends=True)
    total = len(lines)
    if start < 1 or end < start or start > total:
        return f"ERROR: invalid range {start}-{end}, file has {total} lines"
    end = min(end, total)
    # Ensure new_content ends with newline
    replacement = new_content
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    new_lines = replacement.splitlines(keepends=True)
    result = lines[:start - 1] + new_lines + lines[end:]
    p.write_text("".join(result))
    return f"OK: replaced lines {start}-{end} with {len(new_lines)} lines in {path}"


def tool_ls(path: str = ".", workdir: Path = None) -> str:
    p = safe_path(path, workdir)
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for e in entries:
        if e.name in _SKIP_DIRS:
            continue
        lines.append(e.name + ("/" if e.is_dir() else ""))
    return "\n".join(lines) if lines else "(empty directory)"


def tool_grep(pattern: str, path: str = ".", workdir: Path = None) -> str:
    p = safe_path(path, workdir)
    exclude_args = []
    for d in _SKIP_DIRS:
        exclude_args.extend(["--exclude-dir", d])
    cmd = ["grep", "-rn", "--color=never"] + exclude_args + ["--", pattern, str(p)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out after 30s"
    output = result.stdout
    workdir_prefix = str(workdir or WORKDIR) + os.sep
    output = output.replace(workdir_prefix, "")
    lines = output.strip().split("\n")
    if len(lines) > 200:
        lines = lines[:200]
        lines.append(f"... [truncated, {len(lines)} lines total]")
    return "\n".join(lines) if lines[0] else "(no matches)"


def tool_shell(command: str, workdir: Path = None) -> str:
    if any(ch in command for ch in _SHELL_META):
        return "ERROR: shell metacharacters are not allowed"
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"ERROR: failed to parse command: {e}"
    if not parts:
        return "ERROR: empty command"
    if parts[0] not in _SHELL_WHITELIST:
        return f"ERROR: command '{parts[0]}' is not in the whitelist: {', '.join(sorted(_SHELL_WHITELIST))}"
    wd = workdir or WORKDIR
    for arg in parts[1:]:
        if arg.startswith("/"):
            arg_path = Path(arg).resolve()
            if not (str(arg_path) == str(wd) or str(arg_path).startswith(str(wd) + os.sep)):
                return f"ERROR: absolute path outside WORKDIR: {arg}"
    try:
        result = subprocess.run(parts, capture_output=True, text=True, timeout=30, cwd=str(wd))
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 30s"
    output = (result.stdout + result.stderr).strip()
    if len(output) > 50_000:
        output = output[:50_000] + "\n... [truncated]"
    return output if output else "(no output)"


def make_tool_handlers(workdir: Path = None):
    wd = workdir or WORKDIR
    return {
        "read_file":  lambda args: tool_read_file(args.get("path", ""), wd),
        "write_file": lambda args: tool_write_file(args.get("path", ""), args.get("content", ""), wd),
        "patch_file": lambda args: tool_patch_file(args.get("path", ""), args.get("old_str", ""), args.get("new_str", ""), wd),
        "edit_lines": lambda args: tool_edit_lines(args.get("path", ""), int(args.get("start", 1)), int(args.get("end", 1)), args.get("content", ""), wd),
        "ls":         lambda args: tool_ls(args.get("path", "."), wd),
        "grep":       lambda args: tool_grep(args.get("pattern", ""), args.get("path", "."), wd),
        "shell":      lambda args: tool_shell(args.get("command", ""), wd),
    }


def get_tools_prompt() -> str:
    return """You have access to the following tools. To call a tool, respond with ONLY the XML block — no other text.

1. Read a file (returns content with line numbers):
<read_file>
  <path>relative/path/to/file</path>
</read_file>

2. Write a new file or overwrite entirely (max 2MB):
<write_file>
  <path>relative/path/to/file</path>
  <content>full content here</content>
</write_file>

3. Edit lines — replace lines start..end (1-indexed, inclusive) with new content.
   PREFERRED for modifying existing files. After read_file you know exact line numbers.
<edit_lines>
  <path>relative/path/to/file</path>
  <start>10</start>
  <end>14</end>
  <content>replacement lines here</content>
</edit_lines>

4. Patch file — replace exactly 1 occurrence of a string. Use only when you are certain the text is unique.
<patch_file>
  <path>relative/path/to/file</path>
  <old_str>exact existing text including indentation</old_str>
  <new_str>replacement text</new_str>
</patch_file>

5. List directory:
<ls>
  <path>.</path>
</ls>

6. Grep — recursive search by regex:
<grep>
  <pattern>regex_pattern</pattern>
  <path>.</path>
</grep>

7. Shell (allowed: find, wc, head, tail, sort, uniq, diff, echo, pwd, date):
<shell>
  <command>wc -l main.py</command>
</shell>

RULES:
- ONE tool call per response. Wait for the result before calling the next.
- To modify an existing file: read_file first → see line numbers → use edit_lines.
- When finished with all tool calls, write your final answer as plain text (no XML).
"""


def _parse_tool_call(content: str):
    """Extract <tool>...</tool> XML from model output. Returns dict or None."""
    # Убираем возможные маркдаун блоки от модели, собирая строку динамически,
    # чтобы парсер LLM чата не сломался об три обратные кавычки подряд.
    bt = "`" * 3
    m_md = re.search(fr"{bt}(?:xml)?\s*\n?(<.*?>)\s*\n?{bt}", content, re.DOTALL)
    if m_md:
        content = m_md.group(1)

    # Match the outer tool tag, e.g., <read_file>...</read_file>
    m = re.search(r'<([a-z_]+)>([\s\S]*?)</\1>', content)
    if not m:
        return None
        
    tool_name = m.group(1)
    inner_xml = m.group(2)

    if tool_name not in ["read_file", "write_file", "patch_file", "edit_lines", "ls", "grep", "shell"]:
        return None

    args = {}
    # Parse simple <key>value</key> arguments inside the tool tag
    for arg_m in re.finditer(r'<([a-z_]+)>([\s\S]*?)</\1>', inner_xml):
        key = arg_m.group(1)
        val = arg_m.group(2).strip("\n")  # Strip only newlines to preserve indentation
        args[key] = val

    return {"tool": tool_name, "args": args}


# ── Repo context ─────────────────────────────────────────────────────────────

def scan_repo() -> str:
    """Walk WORKDIR, extract Python signatures/docstrings. List other files."""
    lines = []
    py_files = []
    other_files = []

    for root, dirs, files in os.walk(WORKDIR):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        rel_root = Path(root).relative_to(WORKDIR)
        for fname in sorted(files):
            fpath = Path(root) / fname
            rel = str(rel_root / fname) if str(rel_root) != "." else fname
            ext = fpath.suffix.lower()
            if ext == ".py":
                py_files.append((rel, fpath))
            elif ext not in _BINARY_EXTS:
                other_files.append(rel)

    for rel, fpath in py_files:
        lines.append(f"\n### {rel}")
        try:
            source = fpath.read_text(errors="replace")
            tree = _ast.parse(source)
        except SyntaxError:
            lines.append("  (SyntaxError — could not parse)")
            continue
        except Exception:
            lines.append("  (could not read)")
            continue
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                args_list = []
                for arg in node.args.args:
                    ann = ""
                    if arg.annotation:
                        try:
                            ann = ": " + _ast.unparse(arg.annotation)
                        except Exception:
                            pass
                    args_list.append(arg.arg + ann)
                ret = ""
                if node.returns:
                    try:
                        ret = " -> " + _ast.unparse(node.returns)
                    except Exception:
                        pass
                sig = f"def {node.name}({', '.join(args_list)}){ret}"
                lines.append(f"  {sig}")
                doc = _ast.get_docstring(node)
                if doc:
                    first_line = doc.strip().split("\n")[0]
                    lines.append(f"    \"\"\"{first_line}\"\"\"")
            elif isinstance(node, _ast.ClassDef):
                lines.append(f"  class {node.name}")
                doc = _ast.get_docstring(node)
                if doc:
                    first_line = doc.strip().split("\n")[0]
                    lines.append(f"    \"\"\"{first_line}\"\"\"")

    if other_files:
        lines.append("\n### Other files")
        for f in other_files[:100]:
            lines.append(f"  {f}")
        if len(other_files) > 100:
            lines.append(f"  ... and {len(other_files) - 100} more")

    return "\n".join(lines)


def load_repo_context() -> str:
    ctx_file = WORKDIR / ".agent_context.md"
    if ctx_file.is_file():
        return ctx_file.read_text(errors="replace")[:20_000]
    return ""


def build_agent_system_prompt(custom_system: str = "", workdir: Path = None) -> str:
    parts = []
    parts.append(
        "You are an expert coding agent. You achieve goals by utilizing tools.\n"
        "\n"
        "STRICT RULES:\n"
        "1. THINKING: Before taking ANY action, you MUST write down your thought process inside <thinking> tags.\n"
        "2. ONE ACTION AT A TIME: Use ONLY ONE tool per response. Wait for the result.\n"
        "3. EXACT MATCHING: When using <patch_file>, the <old_str> block MUST match the existing file content exactly, including spaces and indentation.\n"
    )
    if custom_system:
        parts.append(custom_system)
    parts.append(get_tools_prompt())
    ctx = load_repo_context()
    if ctx:
        parts.append(f"## Project context (.agent_context.md)\n{ctx}")
    parts.append(f"Working directory: {workdir or WORKDIR}")
    return "\n\n".join(parts)


# ── JSON storage (chats + settings) ──────────────────────────────────────────

_data_lock = threading.Lock()

def _empty_data():
    return {
        "settings": {
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "api_key": os.environ.get("OPENAI_API_KEY", ""),
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
            "system_prompt": "",
            "agent_extra_prompt": "",
            "temperature": 0.2,
            "max_tokens": 4096,
            "mode": "chat",
            "workdir": str(WORKDIR),
        },
        "chats": {},
    }

def load_data() -> dict:
    with _data_lock:
        if AGENT_DATA_PATH.is_file():
            try:
                return json.loads(AGENT_DATA_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return _empty_data()

def save_data(data: dict):
    with _data_lock:
        AGENT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── History storage ───────────────────────────────────────────────────────────
# Stores flat list of {role, content, timestamp, tool_calls?} objects.
# Tool call details are stored for display but NOT fed back to model context.

HISTORY_PATH: Path = Path(
    os.environ.get("HISTORY_PATH", str(Path(__file__).resolve().parent / "history.json"))
)
_history_lock = threading.Lock()


def load_history() -> list:
    with _history_lock:
        if HISTORY_PATH.is_file():
            try:
                return json.loads(HISTORY_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return []


def save_history(messages: list):
    with _history_lock:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(messages, ensure_ascii=False, indent=2))


def append_messages(new_msgs: list):
    """Thread-safe append of new messages to the history file."""
    with _history_lock:
        existing = []
        if HISTORY_PATH.is_file():
            try:
                existing = json.loads(HISTORY_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        existing.extend(new_msgs)
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


# ── FastAPI app ──────────────────────────────────────────────────────────────

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

app = FastAPI()

_HTML_FILE = Path(__file__).resolve().parent / "index.html"


# ── Routes: HTML ─────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(_HTML_FILE)


# ── Routes: History ───────────────────────────────────────────────────────────

@app.get("/api/history")
async def get_history():
    return JSONResponse(load_history())


@app.delete("/api/history")
async def clear_history():
    save_history([])
    return JSONResponse({"ok": True})


# ── Routes: Settings ─────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    data = load_data()
    return JSONResponse(data["settings"])


@app.post("/api/settings")
async def post_settings(req: Request):
    global WORKDIR
    body = await req.json()
    data = load_data()
    for key in ("base_url", "api_key", "model", "system_prompt", "agent_extra_prompt", "temperature", "max_tokens", "mode", "workdir"):
        if key in body:
            data["settings"][key] = body[key]
    # Validate and update WORKDIR
    if "workdir" in body:
        new_wd = Path(body["workdir"]).resolve()
        if new_wd.is_dir():
            WORKDIR = new_wd
            data["settings"]["workdir"] = str(WORKDIR)
        else:
            return JSONResponse({"error": f"Directory does not exist: {body['workdir']}"}, status_code=400)
    save_data(data)
    return JSONResponse(data["settings"])


# ── Routes: Chats ────────────────────────────────────────────────────────────

@app.get("/api/chats")
async def list_chats(mode: str = None):
    data = load_data()
    chats = []
    for cid, chat in data["chats"].items():
        chat_mode = chat.get("mode", "chat")
        if mode and chat_mode != mode:
            continue
        chats.append({
            "id": cid,
            "title": chat["title"],
            "mode": chat_mode,
            "workdir": chat.get("workdir", ""),
            "created_at": chat["created_at"],
            "updated_at": chat["updated_at"],
        })
    chats.sort(key=lambda c: c["updated_at"], reverse=True)
    return JSONResponse(chats)


@app.post("/api/chats")
async def create_chat(req: Request):
    body = await req.json() if (await req.body()) else {}
    data = load_data()
    cid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    chat_mode = body.get("mode", "chat")
    chat_entry = {
        "title": body.get("title", "Новый чат"),
        "mode": chat_mode,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    if chat_mode == "code":
        chat_entry["workdir"] = body.get("workdir") or str(WORKDIR)
    data["chats"][cid] = chat_entry
    save_data(data)
    return JSONResponse({"id": cid, "title": chat_entry["title"], "mode": chat_mode, "workdir": chat_entry.get("workdir", "")})


@app.put("/api/chats/{chat_id}")
async def update_chat(chat_id: str, req: Request):
    body = await req.json()
    data = load_data()
    if chat_id not in data["chats"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    if "title" in body:
        data["chats"][chat_id]["title"] = body["title"]
    data["chats"][chat_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_data(data)
    return JSONResponse({"ok": True})


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    data = load_data()
    if chat_id in data["chats"]:
        del data["chats"][chat_id]
        save_data(data)
    return JSONResponse({"ok": True})


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str):
    data = load_data()
    if chat_id not in data["chats"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(data["chats"][chat_id]["messages"])


@app.post("/api/chats/{chat_id}/messages")
async def post_message(chat_id: str, req: Request):
    body = await req.json()
    data = load_data()
    if chat_id not in data["chats"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    msg = {
        "role": body["role"],
        "content": body["content"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if "tool_calls" in body:
        msg["tool_calls"] = body["tool_calls"]
    data["chats"][chat_id]["messages"].append(msg)
    data["chats"][chat_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Auto-title
    if data["chats"][chat_id]["title"] == "Новый чат" and body["role"] == "user":
        data["chats"][chat_id]["title"] = body["content"][:40].strip()
    save_data(data)
    return JSONResponse({"ok": True})


# ── Routes: Chat completions (streaming) ─────────────────────────────────────

@app.post("/api/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    settings = body.get("settings", {})

    base_url = settings.get("base_url", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    api_key = settings.get("api_key", os.environ.get("OPENAI_API_KEY", ""))
    model = settings.get("model", os.environ.get("OPENAI_MODEL", "gpt-4o"))
    temperature = float(settings.get("temperature", 0.7))
    max_tokens = int(settings.get("max_tokens", 4096))

    def generate():
        try:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key)
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    yield f"data: {json.dumps({'content': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Routes: Agent run (SSE) ──────────────────────────────────────────────────

@app.post("/api/agent/run")
async def agent_run(req: Request):
    body = await req.json()
    chat_id = body.get("chat_id", "")
    user_message = body.get("message", "")
    settings = body.get("settings", {})

    base_url = settings.get("base_url", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    api_key = settings.get("api_key", os.environ.get("OPENAI_API_KEY", ""))
    model = settings.get("model", os.environ.get("OPENAI_MODEL", "gpt-4o"))
    temperature = float(settings.get("temperature", 0))
    max_tokens = int(settings.get("max_tokens", 4096))

    queue: Queue = Queue()

    def agent_thread():
        try:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key)

            # Workdir from settings
            chat_workdir = WORKDIR
            wd_str = settings.get("workdir", "")
            if wd_str:
                wd_candidate = Path(wd_str).resolve()
                if wd_candidate.is_dir():
                    chat_workdir = wd_candidate

            tool_handlers = make_tool_handlers(chat_workdir)
            system_prompt = build_agent_system_prompt(settings.get("agent_extra_prompt", ""), chat_workdir)

            # Load conversation history from chat storage
            data = load_data()
            history_msgs = []
            if chat_id and chat_id in data["chats"]:
                for m in data["chats"][chat_id]["messages"]:
                    history_msgs.append({"role": m["role"], "content": m["content"]})

            api_messages = [{"role": "system", "content": system_prompt}]
            api_messages.extend(history_msgs)
            api_messages.append({"role": "user", "content": user_message})

            tool_calls_log = []

            for iteration in range(MAX_TOOL_CALLS):
                stream = client.chat.completions.create(
                    model=model,
                    messages=api_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )

                content_buf = ""

                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content or ""
                    if not delta:
                        continue
                    content_buf += delta
                    queue.put({"type": "token", "content": delta})

                content = content_buf
                tc = _parse_tool_call(content)

                if tc is None:
                    # Final text response — save assistant message to chat storage
                    if chat_id:
                        data2 = load_data()
                        if chat_id in data2["chats"]:
                            msg = {
                                "role": "assistant",
                                "content": content,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                            if tool_calls_log:
                                msg["tool_calls"] = tool_calls_log
                            data2["chats"][chat_id]["messages"].append(msg)
                            data2["chats"][chat_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                            save_data(data2)
                    break

                tool_name = tc["tool"]
                tool_args = tc["args"]

                handler = tool_handlers.get(tool_name)
                if handler is None:
                    result = f"ERROR: unknown tool '{tool_name}'"
                else:
                    try:
                        result = handler(tool_args)
                    except Exception as e:
                        result = f"ERROR: {e}"

                # Summarize for display
                summary = _tool_summary(tool_name, tool_args, result)
                tool_calls_log.append({"tool": tool_name, "args": tool_args, "summary": summary})
                queue.put({"type": "tool", "tool": tool_name, "summary": summary})

                # Status update instead of markdown (prevents weird parsing of '---')
                queue.put({"type": "status", "content": "Analyzing tool output..."})

                # Feed result back to the model
                api_messages.append({"role": "assistant", "content": content})
                api_messages.append({"role": "user", "content": f"[Tool result for {tool_name}]\n{result}"})
            else:
                queue.put({"type": "text", "content": f"(Reached maximum of {MAX_TOOL_CALLS} tool calls. Stopping.)"})

        except Exception as e:
            queue.put({"type": "error", "content": str(e)})
        finally:
            queue.put(None)  # sentinel

    threading.Thread(target=agent_thread, daemon=True).start()

    def generate():
        while True:
            event = queue.get()
            if event is None:
                yield "data: [DONE]\n\n"
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _tool_summary(tool_name: str, args: dict, result: str) -> str:
    if tool_name == "read_file":
        lines = result.count("\n") + 1 if result and not result.startswith("ERROR") else 0
        chars = len(result)
        return f"read_file {args.get('path', '?')} → {lines} lines, {chars} chars"
    elif tool_name == "write_file":
        if result.startswith("OK"):
            return f"write_file {args.get('path', '?')} → {result}"
        return f"write_file {args.get('path', '?')} → {result[:80]}"
    elif tool_name == "patch_file":
        return f"patch_file {args.get('path', '?')} → {result[:80]}"
    elif tool_name == "edit_lines":
        return f"edit_lines {args.get('path', '?')} [{args.get('start')}–{args.get('end')}] → {result[:60]}"
    elif tool_name == "ls":
        count = len(result.split("\n")) if result and not result.startswith("ERROR") else 0
        return f"ls {args.get('path', '.')} → {count} entries"
    elif tool_name == "grep":
        count = len(result.split("\n")) if result and not result.startswith("ERROR") and result != "(no matches)" else 0
        return f"grep '{args.get('pattern', '?')}' → {count} matches"
    elif tool_name == "shell":
        return f"shell: {args.get('command', '?')} → {len(result)} chars"
    return f"{tool_name} → {result[:60]}"


# ── Routes: Agent commands (/init, /reinit) ──────────────────────────────────

@app.post("/api/agent/cmd")
async def agent_cmd(req: Request):
    body = await req.json()
    command = body.get("command", "")
    settings = body.get("settings", {})

    if command not in ("init", "reinit"):
        return JSONResponse({"error": f"Unknown command: {command}"}, status_code=400)

    base_url = settings.get("base_url", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    api_key = settings.get("api_key", os.environ.get("OPENAI_API_KEY", ""))
    model = settings.get("model", os.environ.get("OPENAI_MODEL", "gpt-4o"))

    # Scan repo
    repo_scan = scan_repo()
    if not repo_scan.strip():
        return JSONResponse({"result": "No files found in workdir.", "scan": ""})

    # Ask LLM to generate .agent_context.md
    try:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a senior developer. Given a repository scan (file list + Python signatures), write a concise .agent_context.md that helps a coding agent understand the project. Include: purpose, architecture, key files, how to run/test. Be brief and practical. Output ONLY the markdown content."},
                {"role": "user", "content": f"Repository scan:\n{repo_scan}"},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        ctx_content = resp.choices[0].message.content or ""
        ctx_file = WORKDIR / ".agent_context.md"
        ctx_file.write_text(ctx_content)
        return JSONResponse({"result": f"Generated .agent_context.md ({len(ctx_content)} chars)", "scan": repo_scan[:5000]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    global WORKDIR

    parser = argparse.ArgumentParser(description="Agent — coding agent web app")
    _default_sandbox = str(Path(__file__).resolve().parent / "sandbox")
    parser.add_argument("--work-dir", type=str, default=_default_sandbox, help="Working directory for the agent")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    args = parser.parse_args()

    WORKDIR = Path(args.work_dir).resolve()
    WORKDIR.mkdir(parents=True, exist_ok=True)

    print(f"Agent  →  http://{args.host}:{args.port}")
    print(f"Workdir: {WORKDIR}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
