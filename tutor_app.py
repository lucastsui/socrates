"""
Adaptive Tutor Web Frontend — FastAPI backend.

Bridges the browser UI to the claude CLI, which connects to the
tutor MCP server.  No API key needed; uses the existing claude subscription.

    python3 tutor_app.py
    open http://localhost:8000
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
HOME = str(Path.home())
PROJECT_DIR = Path(__file__).resolve().parent
CLAUDE_MD = PROJECT_DIR / "CLAUDE.md"
STATIC_DIR = PROJECT_DIR / "static"
LEARNERS_DIR = Path(HOME) / ".claude" / "tutoring" / "learners"

LATEX_INSTRUCTION = (
    "\n\n## Formatting (Web UI)\n"
    "You are being displayed in a web browser with KaTeX support.\n"
    "- Use `$...$` for inline math and `$$...$$` for display math.\n"
    "- Use standard LaTeX commands: \\frac, \\sqrt, \\int, \\sum, \\lim, etc.\n"
    "- Use markdown for bold, tables, code blocks, and lists.\n"
    "- Do NOT use \\( \\) or \\[ \\] delimiters — only dollar signs.\n"
)

FIRST_TURN_TEMPLATE = (
    "You are an adaptive tutor.  The learner wants to study: {topic}\n"
    "Their learner_id is: {learner_id}\n\n"
    "Begin by calling start_session(learner_id=\"{learner_id}\", topic=\"{topic}\") "
    "and then follow the tutoring loop from your system prompt exactly. "
    "If needs_topic_graph is true, generate and store a prerequisite graph. "
    "Then greet the learner and ask your first question."
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Per-client state
# ---------------------------------------------------------------------------

class ClientSession:
    def __init__(self):
        self.session_id: str | None = None
        self.process: asyncio.subprocess.Process | None = None

    async def kill(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.process.kill()
            except ProcessLookupError:
                pass
            self.process = None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    parts = []
    if CLAUDE_MD.exists():
        parts.append(CLAUDE_MD.read_text())
    parts.append(LATEX_INSTRUCTION)
    return "\n".join(parts)


def format_ui_context(ui_state: dict | None) -> str:
    if not ui_state:
        return ""
    parts = ["[Current UI State]"]
    ct = ui_state.get("current_topic", "")
    parts.append(f"Active topic: {ct or '(none)'}")
    parts.append(f"Learner ID: {ui_state.get('learner_id', '')}")
    topics = ui_state.get("topics", [])
    parts.append(f"Topics in sidebar: {', '.join(topics) if topics else '(empty)'}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude subprocess
# ---------------------------------------------------------------------------

def _clean_env() -> dict:
    """Return env without CLAUDECODE to prevent nested-session errors."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("CLAUDE"):
            del env[key]
    return env


def _base_args() -> list[str]:
    return [
        CLAUDE_CMD,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools", "mcp__AdaptiveTutor__*",
        "--permission-mode", "bypassPermissions",
    ]


async def spawn_claude(
    prompt: str,
    session: ClientSession,
    content_blocks: list[dict] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a claude subprocess for one turn.

    If content_blocks is provided, the turn is sent via stdin (stream-json)
    with those blocks plus a final text block for the prompt.
    Otherwise the prompt is passed via -p.
    """

    await session.kill()

    system_prompt = build_system_prompt()
    args = _base_args()
    args += ["--append-system-prompt", system_prompt]

    if session.session_id:
        args += ["--resume", session.session_id]

    use_stdin = bool(content_blocks)

    if use_stdin:
        # stream-json input requires -p (non-interactive mode)
        args += ["-p", "--input-format", "stream-json"]
    else:
        args += ["-p", prompt]

    env = _clean_env()

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if use_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=HOME,
    )

    if use_stdin:
        content = list(content_blocks)
        content.append({"type": "text", "text": prompt})
        msg = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
        }) + "\n"
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    session.process = proc
    return proc


# ---------------------------------------------------------------------------
# NDJSON stream reader
# ---------------------------------------------------------------------------

async def stream_tokens(proc: asyncio.subprocess.Process, session: ClientSession, ws: WebSocket):
    """Read NDJSON from claude stdout, forward text deltas to the WebSocket."""

    buf = b""
    assert proc.stdout is not None

    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")

            # Capture session_id from init or result
            if msg_type == "system" and obj.get("subtype") == "init":
                sid = obj.get("session_id")
                if sid:
                    session.session_id = sid

            elif msg_type == "stream_event":
                event = obj.get("event", {})
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            await ws.send_json({"type": "token", "content": text})

            # Short/non-streamed responses arrive as "assistant" messages
            elif msg_type == "assistant":
                message = obj.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            await ws.send_json({"type": "token", "content": text})

            elif msg_type == "result":
                sid = obj.get("session_id")
                if sid:
                    session.session_id = sid

    # Wait for process to finish
    await proc.wait()


# ---------------------------------------------------------------------------
# REST API — read-only views of learner data
# ---------------------------------------------------------------------------

@app.get("/api/learners")
async def list_learners():
    """List all known learner IDs with their topics and mastery."""
    if not LEARNERS_DIR.exists():
        return JSONResponse([])
    result = []
    for f in sorted(LEARNERS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            topics = {}
            for name, info in data.get("topics", {}).items():
                topics[name] = {
                    "mastery": info.get("mastery_level", 0),
                    "trajectory": info.get("trajectory", "unknown"),
                }
            result.append({
                "learner_id": data.get("learner_id", f.stem),
                "session_count": data.get("session_count", 0),
                "topics": topics,
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return JSONResponse(result)


@app.get("/api/profile/{learner_id}")
async def get_profile(learner_id: str):
    """Return full learner profile."""
    path = LEARNERS_DIR / f"{learner_id}.json"
    if not path.exists():
        return JSONResponse({"error": "Learner not found"}, status_code=404)
    try:
        data = json.loads(path.read_text())
        return JSONResponse(data)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Corrupt profile"}, status_code=500)


@app.delete("/api/profile/{learner_id}/topics/{topic}")
async def delete_topic(learner_id: str, topic: str):
    """Delete a topic from the learner's profile."""
    path = LEARNERS_DIR / f"{learner_id}.json"
    if not path.exists():
        return JSONResponse({"error": "Learner not found"}, status_code=404)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return JSONResponse({"error": "Corrupt profile"}, status_code=500)

    # Normalize topic key
    topic_key = topic.strip().lower().replace(" ", "_").replace("-", "_")

    removed = False
    if topic_key in data.get("topics", {}):
        del data["topics"][topic_key]
        removed = True
    if topic_key in data.get("topic_graphs", {}):
        del data["topic_graphs"][topic_key]

    if not removed:
        return JSONResponse({"error": f"Topic '{topic_key}' not found"}, status_code=404)

    path.write_text(json.dumps(data, indent=2))
    return JSONResponse({"deleted": True, "topic": topic_key})


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(ws: WebSocket, client_id: str):
    await ws.accept()
    session = ClientSession()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "start":
                topic = msg.get("topic", "").strip()
                learner_id = msg.get("learner_id", "default").strip() or "default"
                if not topic:
                    await ws.send_json({"type": "error", "content": "Topic is required"})
                    continue
                prompt = FIRST_TURN_TEMPLATE.format(topic=topic, learner_id=learner_id)
                ui_context = format_ui_context(msg.get("ui_state"))
                if ui_context:
                    prompt = ui_context + "\n\n" + prompt
                # Reset session for fresh start
                session.session_id = None

            elif msg_type == "text":
                prompt = msg.get("content", "").strip()
                if not prompt:
                    continue
                ui_context = format_ui_context(msg.get("ui_state"))
                if ui_context:
                    prompt = ui_context + "\n\n" + prompt

            elif msg_type in ("image_text", "files"):
                prompt = msg.get("content", "").strip() or "Here are my attached files. Please review them."
                ui_context = format_ui_context(msg.get("ui_state"))
                if ui_context:
                    prompt = ui_context + "\n\n" + prompt
                content_blocks = []

                # Images
                for img in msg.get("images", []):
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["mime"],
                            "data": img["data"],
                        },
                    })

                # PDFs (sent as document type)
                for doc in msg.get("documents", []):
                    content_blocks.append({
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": doc["mime"],
                            "data": doc["data"],
                        },
                    })

                # Text files (decode and inline as text)
                for tf in msg.get("text_files", []):
                    try:
                        text_content = base64.b64decode(tf["data"]).decode("utf-8", errors="replace")
                    except Exception:
                        text_content = "(could not decode file)"
                    content_blocks.append({
                        "type": "text",
                        "text": f"--- File: {tf['name']} ---\n{text_content}\n--- End of {tf['name']} ---",
                    })

                if not content_blocks:
                    await ws.send_json({"type": "error", "content": "No files provided"})
                    continue

                await ws.send_json({"type": "stream_start"})
                try:
                    proc = await spawn_claude(prompt, session, content_blocks=content_blocks)
                    await stream_tokens(proc, session, ws)
                except Exception as e:
                    await ws.send_json({"type": "error", "content": str(e)})
                finally:
                    await ws.send_json({"type": "stream_end"})
                continue

            elif msg_type == "end_session":
                # Tell claude to end the tutoring session
                prompt = "The learner wants to end the session. Call end_session and share the summary."
                ui_context = format_ui_context(msg.get("ui_state"))
                if ui_context:
                    prompt = ui_context + "\n\n" + prompt

            else:
                await ws.send_json({"type": "error", "content": f"Unknown message type: {msg_type}"})
                continue

            # --- text-only turn ---
            await ws.send_json({"type": "stream_start"})
            try:
                proc = await spawn_claude(prompt, session)
                await stream_tokens(proc, session, ws)
            except Exception as e:
                await ws.send_json({"type": "error", "content": str(e)})
            finally:
                await ws.send_json({"type": "stream_end"})

    except WebSocketDisconnect:
        pass
    finally:
        await session.kill()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

logger = logging.getLogger("tutor_app")

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    # Validate claude CLI
    resolved = shutil.which(CLAUDE_CMD)
    if not resolved:
        logger.error(
            f"Claude CLI not found: '{CLAUDE_CMD}'. "
            f"Install it (https://docs.anthropic.com/en/docs/claude-code) "
            f"or set CLAUDE_CMD to the correct path."
        )
        sys.exit(1)
    logger.info(f"Using Claude CLI: {resolved}")

    # Warn if system prompt is missing
    if not CLAUDE_MD.exists():
        logger.warning(
            f"System prompt not found at {CLAUDE_MD}. "
            f"The tutor will run without pedagogical instructions."
        )
    else:
        logger.info(f"System prompt: {CLAUDE_MD}")

    uvicorn.run(app, host="127.0.0.1", port=8000)
