"""
AI Agent Platform – Web Server
FastAPI backend that accepts tasks, runs the multi-agent team in the
background and streams every GroupChat message to the browser via SSE.
"""

import nest_asyncio
nest_asyncio.apply()

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── path so we can import LocalMultiAgentTeam ──────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

# ──────────────────────────────────────────────────────────────────────────────
# Message-capture helper
# ──────────────────────────────────────────────────────────────────────────────

class NotifyList(list):
    """A list that posts a copy of every appended item to an asyncio.Queue."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        super().__init__()

    def append(self, item: dict):           # type: ignore[override]
        super().append(item)
        enriched = dict(item)
        enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        try:
            self._queue.put_nowait(enriched)
        except asyncio.QueueFull:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Task model
# ──────────────────────────────────────────────────────────────────────────────

class AgentTask:
    def __init__(self, task_id: str, description: str):
        self.id          = task_id
        self.description = description
        self.status      = "pending"          # pending | running | completed | error | cancelled
        self.error: Optional[str] = None
        self.completed_reason: Optional[str] = None
        self.terminal_source: Optional[str] = None
        self.timeout_seconds: Optional[int] = None
        self.reconciled_at: Optional[str] = None
        self.created_at  = datetime.now(timezone.utc).isoformat()
        self.finished_at: Optional[str] = None
        self.messages: list = []              # accumulated raw GroupChat messages
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self._asyncio_task: Optional[asyncio.Task] = None  # handle for cancellation
        self.game_id: Optional[str] = None

    def to_dict(self, include_messages: bool = False) -> dict:
        d = {
            "id":            self.id,
            "description":   self.description,
            "status":        self.status,
            "created_at":    self.created_at,
            "finished_at":   self.finished_at,
            "message_count": len(self.messages),
        }
        if self.error:
            d["error"] = self.error
        if self.completed_reason:
            d["completed_reason"] = self.completed_reason
        if self.terminal_source:
            d["terminal_source"] = self.terminal_source
        if self.timeout_seconds is not None:
            d["timeout_seconds"] = self.timeout_seconds
        if self.reconciled_at:
            d["reconciled_at"] = self.reconciled_at
        if self.game_id:
            d["game_id"] = self.game_id
        if include_messages:
            d["messages"] = self.messages
        return d


_tasks: Dict[str, AgentTask] = {}
_GAME_STATE_FILE = "/memory/game_state.json"


# ──────────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────────

_TASKS_FILE = "/logs/tasks.json"


def _persist_tasks() -> None:
    """Serialize all tasks to disk (best-effort, atomic write)."""
    try:
        data = []
        for task in _tasks.values():
            data.append({
                "id":          task.id,
                "description": task.description,
                "status":      task.status,
                "error":       task.error,
                "completed_reason": task.completed_reason,
                "terminal_source": task.terminal_source,
                "timeout_seconds": task.timeout_seconds,
                "reconciled_at": task.reconciled_at,
                "created_at":  task.created_at,
                "finished_at": task.finished_at,
                "game_id":     task.game_id,
                "messages":    task.messages,
            })
        os.makedirs(os.path.dirname(_TASKS_FILE), exist_ok=True)
        tmp = _TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _TASKS_FILE)
    except Exception as exc:
        print(f"[persist] WARNING: could not save tasks: {exc}")


def _load_tasks() -> Dict[str, AgentTask]:
    """Load tasks from disk on startup. Mark any pending/running as interrupted."""
    loaded: Dict[str, AgentTask] = {}
    if not os.path.exists(_TASKS_FILE):
        return loaded
    try:
        with open(_TASKS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data:
            task             = AgentTask(item["id"], item["description"])
            task.status      = item.get("status", "unknown")
            task.error       = item.get("error")
            task.completed_reason = item.get("completed_reason")
            task.terminal_source = item.get("terminal_source")
            task.timeout_seconds = item.get("timeout_seconds")
            task.reconciled_at = item.get("reconciled_at")
            task.created_at  = item.get("created_at", task.created_at)
            task.finished_at = item.get("finished_at")
            task.game_id     = item.get("game_id")
            task.messages    = item.get("messages", [])
            if task.status in ("pending", "running"):
                task.status      = "interrupted"
                task.completed_reason = "interrupted"
                task.terminal_source = "server_restart"
                task.finished_at = datetime.now(timezone.utc).isoformat()
                task.messages.append({
                    "name":      "System",
                    "content":   "Task was interrupted by a server restart.",
                    "timestamp": task.finished_at,
                })
            loaded[task.id] = task
        print(f"[persist] Loaded {len(loaded)} task(s) from disk.")
    except Exception as exc:
        print(f"[persist] WARNING: could not load tasks: {exc}")
    return loaded


def _force_game_over_state(game_id: str, result: str, summary: str) -> bool:
    """Best-effort direct state finalization used for timeout fallback."""
    if not game_id:
        return False
    try:
        if not os.path.exists(_GAME_STATE_FILE):
            return False
        with open(_GAME_STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        games = data.setdefault("games", {})
        state = games.get(game_id)
        if not state:
            return False

        if str(state.get("status", "")).lower() != "completed":
            state["status"] = "completed"
            state["result"] = result
            state.setdefault("event_log", []).append({
                "type": "game_over",
                "result": result,
                "summary": summary,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            state["state_version"] = int(state.get("state_version", 0)) + 1
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

            os.makedirs(os.path.dirname(_GAME_STATE_FILE), exist_ok=True)
            tmp = _GAME_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, _GAME_STATE_FILE)
        return True
    except Exception as exc:
        print(f"[dnd-timeout] WARNING: could not force game over state: {exc}")
        return False


def _reconcile_loaded_task_states(tasks: Dict[str, AgentTask]) -> None:
    """Normalize historical task/game-state mismatches after loading from disk."""
    changed = False
    for task in tasks.values():
        if not task.game_id:
            continue

        if task.status in ("cancelled", "interrupted"):
            summary = (
                "Historical reconciliation: terminal game state aligned with task status "
                f"'{task.status}'."
            )
            _force_game_over_state(task.game_id, "DEFEAT", summary)
            if not task.reconciled_at:
                task.reconciled_at = datetime.now(timezone.utc).isoformat()
                changed = True
            if not task.terminal_source:
                task.terminal_source = "startup_reconcile"
                changed = True
            if not task.completed_reason:
                task.completed_reason = "cancelled" if task.status == "cancelled" else "interrupted"
                changed = True

    if changed:
        _persist_tasks()


async def _finalize_cancelled_task_now(task: AgentTask, summary: str, terminal_source: str) -> None:
    """Fallback finalization when there is no live asyncio task to cancel."""
    if task.game_id:
        _force_game_over_state(task.game_id, "DEFEAT", summary)
    task.status = "cancelled"
    task.completed_reason = "cancelled"
    task.terminal_source = terminal_source
    task.finished_at = datetime.now(timezone.utc).isoformat()
    await task.queue.put({
        "type": "system",
        "name": "System",
        "content": f"GAME_OVER: DEFEAT ({summary})" if task.game_id else "Task was stopped by user.",
        "timestamp": task.finished_at,
    })
    await task.queue.put(None)


# ──────────────────────────────────────────────────────────────────────────────
# Agent runner
# ──────────────────────────────────────────────────────────────────────────────

async def _run_agent_task(task: AgentTask) -> None:
    """Instantiate a fresh LocalMultiAgentTeam and run it for the given task."""
    task.status = "running"
    try:
        from main import LocalMultiAgentTeam   # deferred so nest_asyncio is ready

        team = LocalMultiAgentTeam()
        team.create_agents()
        await team.run_development_team(task.description, message_queue=task.queue)
        task.status = "completed"
        task.completed_reason = "normal"
        task.terminal_source = "agent_completion"
    except asyncio.CancelledError:
        task.status = "cancelled"
        task.completed_reason = "cancelled"
        task.terminal_source = task.terminal_source or "user_stop"
        await task.queue.put({
            "type":      "system",
            "name":      "System",
            "content":   "Task was stopped by user.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        task.status = "error"
        task.completed_reason = "error"
        task.terminal_source = "exception"
        task.error  = str(exc)
        await task.queue.put({
            "type":      "system",
            "name":      "System",
            "content":   f"Error: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        task.finished_at = datetime.now(timezone.utc).isoformat()
        await task.queue.put(None)   # sentinel → SSE generator closes
        _persist_tasks()


async def _run_dnd_game(task: AgentTask) -> None:
    """Run autonomous D&D game using DnDGame orchestrator."""
    task.status = "running"
    try:
        from dnd_game import DnDGame

        game = DnDGame()
        game.create_agents()
        task.game_id = game.game_id
        _persist_tasks()

        await game.run_adventure(message_queue=task.queue)
        task.status = "completed"
        task.completed_reason = "normal"
        task.terminal_source = "agent_game_over"
    except asyncio.CancelledError:
        cancel_msg = "Game cancelled by user."
        _force_game_over_state(task.game_id or "", "DEFEAT", cancel_msg)
        task.status = "cancelled"
        task.completed_reason = "cancelled"
        task.terminal_source = task.terminal_source or "user_stop"
        await task.queue.put({
            "type": "system",
            "name": "System",
            "content": f"GAME_OVER: DEFEAT ({cancel_msg})",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        task.status = "error"
        task.completed_reason = "error"
        task.terminal_source = "exception"
        task.error = str(exc)
        await task.queue.put({
            "type": "system",
            "name": "System",
            "content": f"Game error: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        task.finished_at = datetime.now(timezone.utc).isoformat()
        await task.queue.put(None)
        _persist_tasks()


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ──────────────────────────────────────────────────────────────────────────────

_tasks = _load_tasks()  # restore from disk; any pending/running → interrupted
_reconcile_loaded_task_states(_tasks)

app = FastAPI(title="AgentForge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve everything under /static (HTML, icons, …)
_static = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static), name="static")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _format_message(raw: dict) -> dict:
    """Convert a raw AutoGen GroupChat message to a client-friendly dict."""
    content = (raw.get("content") or "").strip()
    has_tool = bool(raw.get("tool_calls"))
    if not content and has_tool:
        names = [tc.get("function", {}).get("name", "?") for tc in raw["tool_calls"]]
        content = f"[tool call: {', '.join(names)}]"
    return {
        "type":      "message",
        "name":      raw.get("name", "Unknown"),
        "role":      raw.get("role", ""),
        "content":   content,
        "has_tool":  has_tool,
        "timestamp": raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
    }


def _build_state_summary(game_state: Optional[dict]) -> Optional[dict]:
    if not game_state:
        return None
    party = game_state.get("party", []) or []
    enemies = game_state.get("enemies", []) or []
    party_alive = sum(1 for p in party if bool(p.get("alive", False)) and int(p.get("current_hp", 0)) > 0)
    enemies_alive = sum(1 for e in enemies if bool(e.get("alive", False)) and int(e.get("current_hp", 0)) > 0)
    return {
        "status": game_state.get("status"),
        "result": game_state.get("result"),
        "scene_id": game_state.get("scene_id"),
        "round": game_state.get("round"),
        "turn_index": game_state.get("turn_index"),
        "next_actor": game_state.get("next_actor"),
        "state_version": game_state.get("state_version"),
        "event_count": len(game_state.get("event_log", []) or []),
        "party_alive": party_alive,
        "enemies_alive": enemies_alive,
    }


def _load_game_state(game_id: Optional[str]) -> Optional[dict]:
    if not game_id:
        return None
    if not os.path.exists(_GAME_STATE_FILE):
        return None
    with open(_GAME_STATE_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return (data.get("games") or {}).get(game_id)


# ─── routes ──────────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    task: str


@app.get("/", response_class=HTMLResponse)
async def index():
    html = os.path.join(_static, "index.html")
    if not os.path.exists(html):
        raise HTTPException(404, "Frontend not found")
    with open(html, encoding="utf-8") as fh:
        return fh.read()


@app.post("/api/tasks", status_code=201)
async def create_task(req: TaskRequest):
    if not req.task.strip():
        raise HTTPException(400, "task must not be empty")
    task_id = str(uuid.uuid4())[:8]
    task    = AgentTask(task_id, req.task.strip())
    _tasks[task_id] = task
    _persist_tasks()
    task._asyncio_task = asyncio.create_task(_run_agent_task(task))
    return task.to_dict()


@app.post("/api/game/start", status_code=201)
async def start_game():
    task_id = str(uuid.uuid4())[:8]
    task = AgentTask(task_id, "D&D Adventure: Autonomous Run")
    _tasks[task_id] = task
    _persist_tasks()
    task._asyncio_task = asyncio.create_task(_run_dnd_game(task))
    return task.to_dict()


@app.get("/api/game/{task_id}/state")
async def get_game_state(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    task = _tasks[task_id]
    if not task.game_id:
        raise HTTPException(404, "game is not initialized yet")

    game_state = _load_game_state(task.game_id)
    if not game_state:
        return {
            "task_id": task.id,
            "game_id": task.game_id,
            "status": task.status,
            "state": None,
            "detail": "initializing",
        }

    return {
        "task_id": task.id,
        "game_id": task.game_id,
        "status": task.status,
        "state": game_state,
    }


@app.get("/api/game/{task_id}/diagnostics")
async def get_game_diagnostics(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    task = _tasks[task_id]

    game_state = _load_game_state(task.game_id)
    return {
        "task_id": task.id,
        "game_id": task.game_id,
        "task_status": task.status,
        "completed_reason": task.completed_reason,
        "terminal_source": task.terminal_source,
        "timeout_seconds": task.timeout_seconds,
        "reconciled_at": task.reconciled_at,
        "error": task.error,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "state_summary": _build_state_summary(game_state),
        "detail": "initializing" if game_state is None else "ok",
    }


@app.get("/api/tasks")
async def list_tasks():
    return [t.to_dict() for t in reversed(list(_tasks.values()))]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    return _tasks[task_id].to_dict(include_messages=True)


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """Server-Sent Events stream for the task's agent messages."""
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    task = _tasks[task_id]

    async def generator() -> AsyncGenerator[str, None]:
        # 1. Replay already-captured messages (useful on reconnect)
        for raw in list(task.messages):
            yield _sse(_format_message(raw))

        # 2. Send current status
        yield _sse({"type": "status", "status": task.status})

        # 3. If already finished, send done and exit
        if task.status in ("completed", "error"):
            yield _sse({"type": "done", "status": task.status, "error": task.error})
            return

        # 4. Live stream
        while True:
            try:
                raw = await asyncio.wait_for(task.queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if raw is None:
                # Drain any messages that arrived before the sentinel
                while not task.queue.empty():
                    extra = task.queue.get_nowait()
                    if extra is not None:
                        task.messages.append(extra)
                        yield _sse(_format_message(extra))
                yield _sse({"type": "done", "status": task.status, "error": task.error})
                break

            task.messages.append(raw)
            yield _sse(_format_message(raw))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.post("/api/tasks/{task_id}/stop", status_code=200)
async def stop_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    task = _tasks[task_id]
    if task.status not in ("pending", "running"):
        raise HTTPException(400, f"task is already {task.status}")
    if task._asyncio_task and not task._asyncio_task.done():
        task.terminal_source = "user_stop"
        task._asyncio_task.cancel()
    else:
        await _finalize_cancelled_task_now(task, "Task cancelled by user.", "user_stop")
    _persist_tasks()
    return {"id": task_id, "status": "cancelling"}


@app.post("/api/tasks/stop-all", status_code=200)
async def stop_all_tasks():
    cancelled = []
    for task in list(_tasks.values()):
        if task.status in ("pending", "running"):
            if task._asyncio_task and not task._asyncio_task.done():
                task.terminal_source = "user_stop_all"
                task._asyncio_task.cancel()
            else:
                await _finalize_cancelled_task_now(task, "Task cancelled by stop-all request.", "user_stop_all")
            cancelled.append(task.id)
    _persist_tasks()
    return {"cancelled": cancelled, "count": len(cancelled)}


@app.delete("/api/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(404, "task not found")
    del _tasks[task_id]


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
