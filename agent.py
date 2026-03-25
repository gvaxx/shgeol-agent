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

def safe_path(rel: str) -> Path:
    """Resolve *rel* relative to WORKDIR.  Block directory traversal."""
    resolved = (WORKDIR / rel).resolve()
    if not (str(resolved) == str(WORKDIR) or str(resolved).startswith(str(WORKDIR) + os.sep)):
        raise ValueError(f"Path escapes workdir: {rel}")
    return resolved


# ── Tools ────────────────────────────────────────────────────────────────────

def tool_read_file(path: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    content = p.read_text(errors="replace")
    if len(content) > 50_000:
        return content[:50_000] + f"\n... [truncated at 50k chars, total {len(content)}]"
    return content


def tool_write_file(path: str, content: str) -> str:
    if len(content) > 2_000_000:
        return "ERROR: content exceeds 2MB limit"
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: wrote {len(content)} chars to {path}"


def tool_patch_file(path: str, old_str: str, new_str: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    text = p.read_text(errors="replace")
    count = text.count(old_str)
    if count == 0:
        return f"ERROR: old_str not found in {path}"
    if count > 1:
        return f"ERROR: old_str appears {count} times in {path} (must be exactly 1)"
    text = text.replace(old_str, new_str, 1)
    p.write_text(text)
    return f"OK: patched {path}"


def tool_ls(path: str = ".") -> str:
    p = safe_path(path)
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for e in entries:
        if e.name in _SKIP_DIRS:
            continue
        lines.append(e.name + ("/" if e.is_dir() else ""))
    return "\n".join(lines) if lines else "(empty directory)"


def tool_grep(pattern: str, path: str = ".") -> str:
    p = safe_path(path)
    exclude_args = []
    for d in _SKIP_DIRS:
        exclude_args.extend(["--exclude-dir", d])
    cmd = ["grep", "-rn", "--color=never"] + exclude_args + ["--", pattern, str(p)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out after 30s"
    output = result.stdout
    workdir_prefix = str(WORKDIR) + os.sep
    output = output.replace(workdir_prefix, "")
    lines = output.strip().split("\n")
    if len(lines) > 200:
        lines = lines[:200]
        lines.append(f"... [truncated, {len(lines)} lines total]")
    return "\n".join(lines) if lines[0] else "(no matches)"


def tool_shell(command: str) -> str:
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
    for arg in parts[1:]:
        if arg.startswith("/"):
            arg_path = Path(arg).resolve()
            if not (str(arg_path) == str(WORKDIR) or str(arg_path).startswith(str(WORKDIR) + os.sep)):
                return f"ERROR: absolute path outside WORKDIR: {arg}"
    try:
        result = subprocess.run(parts, capture_output=True, text=True, timeout=30, cwd=str(WORKDIR))
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 30s"
    output = (result.stdout + result.stderr).strip()
    if len(output) > 50_000:
        output = output[:50_000] + "\n... [truncated]"
    return output if output else "(no output)"


TOOL_HANDLERS = {
    "read_file":  lambda args: tool_read_file(args["path"]),
    "write_file": lambda args: tool_write_file(args["path"], args["content"]),
    "patch_file": lambda args: tool_patch_file(args["path"], args["old_str"], args["new_str"]),
    "ls":         lambda args: tool_ls(args.get("path", ".")),
    "grep":       lambda args: tool_grep(args["pattern"], args.get("path", ".")),
    "shell":      lambda args: tool_shell(args["command"]),
}


def get_tools_prompt() -> str:
    return """You have access to the following tools. To call a tool, respond with ONLY a JSON object (no other text):

{"tool": "read_file", "args": {"path": "<relative path>"}}
  Read a file (max 50k chars).

{"tool": "write_file", "args": {"path": "<relative path>", "content": "<full content>"}}
  Write a file (creates parent dirs). Max 2MB.

{"tool": "patch_file", "args": {"path": "<relative path>", "old_str": "<exact text to find>", "new_str": "<replacement>"}}
  Replace exactly 1 occurrence of old_str with new_str in a file.

{"tool": "ls", "args": {"path": "."}}
  List directory. Dirs have trailing /.

{"tool": "grep", "args": {"pattern": "<regex>", "path": "."}}
  Recursive grep. Returns file:line:match.

{"tool": "shell", "args": {"command": "<command>"}}
  Run a whitelisted command (find, wc, head, tail, sort, uniq, diff, echo, pwd, date).
  No shell metacharacters. No absolute paths outside workdir.

IMPORTANT:
- When calling a tool, output ONLY the JSON object. No explanations before or after.
- After receiving tool output, you may call another tool or provide your final answer.
- When you are done and want to reply to the user, just write your response normally (no JSON).
"""


def _parse_tool_call(content: str):
    """Extract {"tool":..., "args":...} from model output. Returns dict or None."""
    content = content.strip()
    # Try bare JSON first
    if content.startswith("{"):
        try:
            obj = json.loads(content)
            if "tool" in obj and "args" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    # Try ```json block
    m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", content, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            if "tool" in obj and "args" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    # Try to find JSON object anywhere in the text
    m = re.search(r'(\{"tool"\s*:.*?\})\s*$', content, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            if "tool" in obj and "args" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    return None


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


def build_agent_system_prompt(custom_system: str = "") -> str:
    parts = []
    parts.append("You are a coding agent. You help the user by reading, writing, and modifying files in their project.")
    if custom_system:
        parts.append(custom_system)
    parts.append(get_tools_prompt())
    ctx = load_repo_context()
    if ctx:
        parts.append(f"## Project context (.agent_context.md)\n{ctx}")
    parts.append(f"Working directory: {WORKDIR}")
    return "\n\n".join(parts)


# ── JSON storage ─────────────────────────────────────────────────────────────

_data_lock = threading.Lock()

def _empty_data():
    return {
        "settings": {
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "api_key": os.environ.get("OPENAI_API_KEY", ""),
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
            "system_prompt": "",
            "temperature": 0.7,
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


# ── FastAPI app ──────────────────────────────────────────────────────────────

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI()


# ── Routes: HTML ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


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
    for key in ("base_url", "api_key", "model", "system_prompt", "temperature", "max_tokens", "mode", "workdir"):
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
async def list_chats():
    data = load_data()
    chats = []
    for cid, chat in data["chats"].items():
        chats.append({"id": cid, "title": chat["title"], "created_at": chat["created_at"], "updated_at": chat["updated_at"]})
    chats.sort(key=lambda c: c["updated_at"], reverse=True)
    return JSONResponse(chats)


@app.post("/api/chats")
async def create_chat(req: Request):
    body = await req.json() if (await req.body()) else {}
    data = load_data()
    cid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    data["chats"][cid] = {
        "title": body.get("title", "Новый чат"),
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    save_data(data)
    return JSONResponse({"id": cid, "title": data["chats"][cid]["title"]})


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

            system_prompt = build_agent_system_prompt(settings.get("system_prompt", ""))

            # Load conversation history
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
                response = client.chat.completions.create(
                    model=model,
                    messages=api_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content = response.choices[0].message.content or ""
                tc = _parse_tool_call(content)

                if tc is None:
                    # Final text response
                    queue.put({"type": "text", "content": content})
                    # Save assistant message with tool_calls metadata
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

                handler = TOOL_HANDLERS.get(tool_name)
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


# ── HTML ─────────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent</title>
<style>
:root{
  --bg:#0c0c0e;--surface:#151518;--surface2:#1e1e23;--surface3:#26262d;
  --border:#2a2a32;--border2:#35353f;
  --text:#e4e4e7;--text2:#8b8b96;--text3:#5a5a65;
  --accent:#6d9fff;--accent2:#5580d4;--accent-dim:rgba(109,159,255,.08);
  --user-bg:#171d2a;--user-border:rgba(109,159,255,.12);
  --danger:#ef4444;--green:#34d399;
  --radius:10px;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  --mono:'SF Mono','Cascadia Code','JetBrains Mono','Fira Code',monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--font);background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex}

/* Sidebar */
.sidebar{width:260px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;transition:margin-left .2s}
.sidebar.hidden{margin-left:-260px}
.sidebar-header{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.sidebar-header h2{font-size:14px;font-weight:600;color:var(--text2)}
.btn-new-chat{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;font-weight:500}
.btn-new-chat:hover{background:var(--accent2)}
.chat-list{flex:1;overflow-y:auto;padding:8px}
.chat-item{padding:10px 12px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px;display:flex;align-items:center;justify-content:space-between}
.chat-item:hover{background:var(--surface2)}
.chat-item.active{background:var(--surface3);color:var(--text)}
.chat-item .chat-title{flex:1;overflow:hidden;text-overflow:ellipsis}
.chat-item .chat-delete{display:none;background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:0 4px;flex-shrink:0}
.chat-item:hover .chat-delete{display:block}
.chat-item .chat-delete:hover{color:var(--danger)}

/* Main */
.main{flex:1;display:flex;flex-direction:column;min-width:0}

/* Topbar */
.topbar{height:48px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
.btn-hamburger{background:none;border:none;color:var(--text2);font-size:18px;cursor:pointer;padding:4px}
.model-badge{background:var(--surface3);color:var(--text2);font-size:11px;padding:3px 8px;border-radius:5px;font-family:var(--mono)}
.mode-toggle{display:flex;background:var(--surface2);border-radius:6px;overflow:hidden;margin-left:auto}
.mode-btn{background:none;border:none;color:var(--text3);font-size:12px;padding:6px 14px;cursor:pointer;transition:all .15s}
.mode-btn.active{background:var(--accent);color:#fff}
.btn-settings{background:none;border:none;color:var(--text2);font-size:16px;cursor:pointer;padding:4px 6px}

/* Settings panel */
.settings-panel{width:340px;background:var(--surface);border-left:1px solid var(--border);flex-shrink:0;overflow-y:auto;padding:20px;display:none;flex-direction:column;gap:14px}
.settings-panel.open{display:flex}
.settings-panel h3{font-size:13px;font-weight:600;color:var(--text2);margin-bottom:4px}
.settings-panel label{font-size:12px;color:var(--text3);display:block;margin-bottom:4px}
.settings-panel input,.settings-panel textarea,.settings-panel select{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:8px 10px;font-size:13px;font-family:var(--font);outline:none}
.settings-panel input:focus,.settings-panel textarea:focus{border-color:var(--accent)}
.settings-panel textarea{resize:vertical;min-height:60px;font-family:var(--mono);font-size:12px}
.agent-section{border-top:1px solid var(--border);padding-top:14px;margin-top:6px}
.btn-cmd{background:var(--surface3);color:var(--text2);border:1px solid var(--border);border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;font-family:var(--mono)}
.btn-cmd:hover{background:var(--surface2);border-color:var(--border2)}
.cmd-status{font-size:11px;color:var(--text3);margin-top:4px;font-family:var(--mono)}

/* Messages */
.messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
.msg{max-width:800px;width:100%;margin:0 auto;padding:12px 16px;border-radius:var(--radius);font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--user-bg);border:1px solid var(--user-border)}
.msg.assistant{background:transparent}
.msg .tool-calls{margin-bottom:8px}
.tool-call-line{font-family:var(--mono);font-size:11px;color:var(--text3);padding:2px 0;line-height:1.5}
.msg .msg-content p{margin:0 0 8px 0}
.msg .msg-content p:last-child{margin-bottom:0}
.msg .msg-content pre{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:10px 12px;overflow-x:auto;margin:8px 0;font-family:var(--mono);font-size:12px;line-height:1.5}
.msg .msg-content code{font-family:var(--mono);font-size:12px;background:var(--surface2);padding:1px 5px;border-radius:3px}
.msg .msg-content pre code{background:none;padding:0}

/* Input */
.input-area{background:var(--surface);border-top:1px solid var(--border);padding:14px 20px;flex-shrink:0}
.input-wrap{max-width:800px;margin:0 auto;display:flex;gap:10px;align-items:flex-end}
.input-wrap textarea{flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:var(--radius);padding:10px 14px;font-size:14px;font-family:var(--font);line-height:1.5;resize:none;outline:none;max-height:200px;min-height:42px}
.input-wrap textarea:focus{border-color:var(--accent)}
.btn-send{background:var(--accent);color:#fff;border:none;border-radius:8px;width:40px;height:40px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.btn-send:hover{background:var(--accent2)}
.btn-send:disabled{opacity:.4;cursor:not-allowed}

/* Scrollbar */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--border2)}

/* Empty state */
.empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:14px}
</style>
</head>
<body>

<!-- Sidebar -->
<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <h2>Chats</h2>
    <button class="btn-new-chat" onclick="newChat()">+ New</button>
  </div>
  <div class="chat-list" id="chatList"></div>
</div>

<!-- Main -->
<div class="main">
  <!-- Topbar -->
  <div class="topbar">
    <button class="btn-hamburger" onclick="toggleSidebar()">&#9776;</button>
    <span class="model-badge" id="modelBadge">gpt-4o</span>
    <div class="mode-toggle">
      <button class="mode-btn active" id="btnChat" onclick="setMode('chat')">&#128172; Chat</button>
      <button class="mode-btn" id="btnCode" onclick="setMode('code')">&#9889; Code</button>
    </div>
    <button class="btn-settings" onclick="toggleSettings()">&#9881;</button>
  </div>

  <!-- Content area -->
  <div style="flex:1;display:flex;overflow:hidden">
    <!-- Messages -->
    <div style="flex:1;display:flex;flex-direction:column;min-width:0">
      <div class="messages" id="messages">
        <div class="empty-state" id="emptyState">Start a new conversation</div>
      </div>
      <div class="input-area">
        <div class="input-wrap">
          <textarea id="input" rows="1" placeholder="Type a message..." onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
          <button class="btn-send" id="btnSend" onclick="sendMessage()">&#9654;</button>
        </div>
      </div>
    </div>

    <!-- Settings panel -->
    <div class="settings-panel" id="settingsPanel">
      <h3>Settings</h3>
      <div>
        <label>Base URL</label>
        <input id="sBaseUrl" placeholder="https://api.openai.com/v1">
      </div>
      <div>
        <label>API Key</label>
        <input id="sApiKey" type="password" placeholder="sk-...">
      </div>
      <div>
        <label>Model</label>
        <input id="sModel" placeholder="gpt-4o">
      </div>
      <div>
        <label>System Prompt</label>
        <textarea id="sSystemPrompt" rows="3" placeholder="You are a helpful assistant."></textarea>
      </div>
      <div>
        <label>Temperature</label>
        <input id="sTemperature" type="number" step="0.1" min="0" max="2" value="0.7">
      </div>
      <div>
        <label>Max Tokens</label>
        <input id="sMaxTokens" type="number" step="256" min="256" max="128000" value="4096">
      </div>
      <div class="agent-section" id="agentSection" style="display:none">
        <h3>Agent (Code mode)</h3>
        <div>
          <label>Working Directory</label>
          <input id="sWorkdir" placeholder="/path/to/project">
        </div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn-cmd" onclick="agentCmd('init')">/init</button>
          <button class="btn-cmd" onclick="agentCmd('reinit')">/reinit</button>
        </div>
        <div class="cmd-status" id="cmdStatus"></div>
      </div>
    </div>
  </div>
</div>

<script>
// ── State ──
let S = {
  chats: [],
  currentChat: null,
  settings: {},
  mode: 'chat',
  streaming: false,
};

// ── Init ──
(async function init() {
  await loadSettings();
  await loadChats();
})();

// ── Settings ──
async function loadSettings() {
  const r = await fetch('/api/settings');
  S.settings = await r.json();
  applySettings();
}

function applySettings() {
  document.getElementById('sBaseUrl').value = S.settings.base_url || '';
  document.getElementById('sApiKey').value = S.settings.api_key || '';
  document.getElementById('sModel').value = S.settings.model || 'gpt-4o';
  document.getElementById('sSystemPrompt').value = S.settings.system_prompt || '';
  document.getElementById('sTemperature').value = S.settings.temperature ?? 0.7;
  document.getElementById('sMaxTokens').value = S.settings.max_tokens ?? 4096;
  document.getElementById('sWorkdir').value = S.settings.workdir || '';
  document.getElementById('modelBadge').textContent = S.settings.model || 'gpt-4o';
  setMode(S.settings.mode || 'chat');
}

function getSettings() {
  return {
    base_url: document.getElementById('sBaseUrl').value,
    api_key: document.getElementById('sApiKey').value,
    model: document.getElementById('sModel').value,
    system_prompt: document.getElementById('sSystemPrompt').value,
    temperature: parseFloat(document.getElementById('sTemperature').value) || 0.7,
    max_tokens: parseInt(document.getElementById('sMaxTokens').value) || 4096,
    mode: S.mode,
    workdir: document.getElementById('sWorkdir').value,
  };
}

async function saveSettings() {
  const s = getSettings();
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(s)});
  const data = await r.json();
  if (data.error) { alert(data.error); return; }
  S.settings = data;
  document.getElementById('modelBadge').textContent = data.model || 'gpt-4o';
}

// Debounced auto-save settings
let _saveTimer = null;
function debounceSave() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveSettings, 800);
}
document.querySelectorAll('.settings-panel input, .settings-panel textarea').forEach(el => {
  el.addEventListener('input', debounceSave);
});

// ── Mode toggle ──
function setMode(mode) {
  S.mode = mode;
  document.getElementById('btnChat').classList.toggle('active', mode === 'chat');
  document.getElementById('btnCode').classList.toggle('active', mode === 'code');
  document.getElementById('agentSection').style.display = mode === 'code' ? 'block' : 'none';
}

// ── Sidebar ──
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('hidden');
}

function toggleSettings() {
  document.getElementById('settingsPanel').classList.toggle('open');
}

// ── Chats ──
async function loadChats() {
  const r = await fetch('/api/chats');
  S.chats = await r.json();
  renderChatList();
}

function renderChatList() {
  const el = document.getElementById('chatList');
  el.innerHTML = '';
  for (const c of S.chats) {
    const d = document.createElement('div');
    d.className = 'chat-item' + (S.currentChat === c.id ? ' active' : '');
    d.innerHTML = `<span class="chat-title">${esc(c.title)}</span><button class="chat-delete" onclick="event.stopPropagation();deleteChat('${c.id}')">&times;</button>`;
    d.onclick = () => openChat(c.id);
    d.ondblclick = () => renameChat(c.id, c.title);
    el.appendChild(d);
  }
}

async function newChat() {
  const r = await fetch('/api/chats', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
  const c = await r.json();
  await loadChats();
  openChat(c.id);
}

async function openChat(id) {
  S.currentChat = id;
  renderChatList();
  const r = await fetch(`/api/chats/${id}/messages`);
  const msgs = await r.json();
  renderMessages(msgs);
}

async function deleteChat(id) {
  await fetch(`/api/chats/${id}`, {method:'DELETE'});
  if (S.currentChat === id) {
    S.currentChat = null;
    document.getElementById('messages').innerHTML = '<div class="empty-state">Start a new conversation</div>';
  }
  await loadChats();
}

async function renameChat(id, oldTitle) {
  const title = prompt('Rename chat:', oldTitle);
  if (title && title !== oldTitle) {
    await fetch(`/api/chats/${id}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title})});
    await loadChats();
  }
}

// ── Messages ──
function renderMessages(msgs) {
  const el = document.getElementById('messages');
  el.innerHTML = '';
  if (!msgs.length) {
    el.innerHTML = '<div class="empty-state">Start a new conversation</div>';
    return;
  }
  for (const m of msgs) {
    appendMessage(m.role, m.content, m.tool_calls || []);
  }
  el.scrollTop = el.scrollHeight;
}

function appendMessage(role, content, toolCalls) {
  const el = document.getElementById('messages');
  const empty = document.getElementById('emptyState');
  if (empty) empty.remove();
  const d = document.createElement('div');
  d.className = `msg ${role}`;
  let html = '';
  if (toolCalls && toolCalls.length) {
    html += '<div class="tool-calls">';
    for (const tc of toolCalls) {
      html += `<div class="tool-call-line">&#9889; ${esc(tc.summary || tc.tool)}</div>`;
    }
    html += '</div>';
  }
  html += `<div class="msg-content">${renderMarkdown(content)}</div>`;
  d.innerHTML = html;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
  return d;
}

function renderMarkdown(text) {
  if (!text) return '';
  // Code blocks
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => `<pre><code>${esc(code.trim())}</code></pre>`);
  // Inline code
  text = text.replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
  // Bold
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Paragraphs (split by double newlines)
  const parts = text.split(/\n\n+/);
  return parts.map(p => {
    if (p.startsWith('<pre>') || p.startsWith('<h')) return p;
    return `<p>${p.replace(/\n/g, '<br>')}</p>`;
  }).join('');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ── Input ──
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

// ── Send ──
async function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || S.streaming) return;

  // Ensure we have a chat
  if (!S.currentChat) {
    const r = await fetch('/api/chats', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
    const c = await r.json();
    S.currentChat = c.id;
    await loadChats();
  }

  // Save user message
  await fetch(`/api/chats/${S.currentChat}/messages`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({role:'user', content:text})
  });

  appendMessage('user', text, []);
  input.value = '';
  input.style.height = 'auto';

  S.streaming = true;
  document.getElementById('btnSend').disabled = true;

  if (S.mode === 'chat') {
    await streamChat(text);
  } else {
    await streamAgent(text);
  }

  S.streaming = false;
  document.getElementById('btnSend').disabled = false;
  await loadChats(); // refresh titles
}

async function streamChat(text) {
  const settings = getSettings();
  // Build messages from DOM
  const msgs = [];
  if (settings.system_prompt) msgs.push({role:'system', content:settings.system_prompt});
  // Load messages from current chat
  const r = await fetch(`/api/chats/${S.currentChat}/messages`);
  const chatMsgs = await r.json();
  for (const m of chatMsgs) {
    msgs.push({role: m.role, content: m.content});
  }

  const resp = await fetch('/api/chat/completions', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({messages: msgs, settings})
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let assistantText = '';
  const msgEl = appendMessage('assistant', '', []);
  const contentEl = msgEl.querySelector('.msg-content');

  let buffer = '';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream:true});
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const payload = line.slice(6);
      if (payload === '[DONE]') break;
      try {
        const d = JSON.parse(payload);
        if (d.error) { assistantText += `\n\nError: ${d.error}`; break; }
        if (d.content) assistantText += d.content;
      } catch {}
    }
    contentEl.innerHTML = renderMarkdown(assistantText);
    document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
  }

  // Save assistant message
  if (assistantText) {
    await fetch(`/api/chats/${S.currentChat}/messages`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({role:'assistant', content:assistantText})
    });
  }
}

async function streamAgent(text) {
  const settings = getSettings();
  const resp = await fetch('/api/agent/run', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({chat_id: S.currentChat, message: text, settings})
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let assistantText = '';
  let toolCalls = [];
  const msgEl = appendMessage('assistant', '', []);
  const contentEl = msgEl.querySelector('.msg-content');

  // Add tool calls container
  let toolsEl = document.createElement('div');
  toolsEl.className = 'tool-calls';
  msgEl.insertBefore(toolsEl, contentEl);

  let buffer = '';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream:true});
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const payload = line.slice(6);
      if (payload === '[DONE]') continue;
      try {
        const d = JSON.parse(payload);
        if (d.type === 'tool') {
          toolCalls.push(d);
          const tcLine = document.createElement('div');
          tcLine.className = 'tool-call-line';
          tcLine.textContent = '\u26A1 ' + (d.summary || d.tool);
          toolsEl.appendChild(tcLine);
        } else if (d.type === 'text') {
          assistantText = d.content;
          contentEl.innerHTML = renderMarkdown(assistantText);
        } else if (d.type === 'error') {
          assistantText = `Error: ${d.content}`;
          contentEl.innerHTML = renderMarkdown(assistantText);
        }
      } catch {}
    }
    document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
  }
}

// ── Agent commands ──
async function agentCmd(cmd) {
  const statusEl = document.getElementById('cmdStatus');
  statusEl.textContent = `Running /${cmd}...`;
  const settings = getSettings();
  try {
    const r = await fetch('/api/agent/cmd', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command: cmd, settings})
    });
    const data = await r.json();
    if (data.error) {
      statusEl.textContent = `Error: ${data.error}`;
    } else {
      statusEl.textContent = data.result;
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
  }
}
</script>
</body>
</html>
"""


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    global WORKDIR, AGENT_DATA_PATH

    parser = argparse.ArgumentParser(description="Agent — LLM chat + coding agent")
    parser.add_argument("--work-dir", type=str, default=None, help="Working directory for the agent")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    args = parser.parse_args()

    if args.work_dir:
        WORKDIR = Path(args.work_dir).resolve()
        if not WORKDIR.is_dir():
            print(f"Error: {WORKDIR} is not a directory", file=sys.stderr)
            sys.exit(1)

    # Update settings defaults with current WORKDIR
    data = load_data()
    data["settings"]["workdir"] = str(WORKDIR)
    save_data(data)

    print(f"Agent starting on http://{args.host}:{args.port}")
    print(f"Working directory: {WORKDIR}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
