import argparse
import json
import os
import threading
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("game_state")

DB_PATH = os.environ.get("GAME_STATE_DB", "/memory/game_state.json")
_LOCK = threading.Lock()
GameId = str | int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {"games": {}}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_db(db: dict) -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)


def _ensure_game(db: dict, game_id: str) -> dict:
    games = db.setdefault("games", {})
    if game_id not in games:
        games[game_id] = {
            "game_id": game_id,
            "status": "new",
            "scene_id": 0,
            "round": 0,
            "turn_index": 0,
            "state_version": 0,
            "last_actor": "",
            "next_actor": "DungeonMaster",
            "party": [],
            "enemies": [],
            "initiative_order": [],
            "flags": {},
            "event_log": [],
            "result": None,
            "updated_at": _now(),
        }
    return games[game_id]


def _get_latest_game_id() -> str:
    """Return the game_id of the most recently updated running game, or any game."""
    if not os.path.exists(DB_PATH):
        return "default"
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return "default"
    games = db.get("games") or {}
    if not games:
        return "default"
    running = {gid: g for gid, g in games.items() if g.get("status") == "running"}
    pool = running or games
    return max(pool, key=lambda gid: pool[gid].get("updated_at", ""))


def _normalize_game_id(game_id: GameId = "") -> str:
    value = str(game_id or "").strip()
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        value = value[1:-1].strip()
    if not value:
        value = _get_latest_game_id()
    return value


def _is_alive(entity: dict) -> bool:
    if not isinstance(entity, dict):
        return False
    if entity.get("alive") is False:
        return False
    return int(entity.get("current_hp", 0)) > 0


def _find_character(state: dict, target_name: str):
    name = (target_name or "").strip().lower()
    for bucket in ("party", "enemies"):
        items = state.get(bucket, [])
        for idx, item in enumerate(items):
            if str(item.get("name", "")).lower() == name:
                return bucket, idx, item
    return None, -1, None


@mcp.tool()
def init_game(game_config: dict) -> dict:
    game_id = _normalize_game_id(game_config.get("game_id", "default"))
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)

        state["status"] = str(game_config.get("status", state.get("status", "running")) or "running")
        state["scene_id"] = int(game_config.get("scene_id", state.get("scene_id", 0)))
        state["round"] = int(game_config.get("round", state.get("round", 1)))
        state["turn_index"] = int(game_config.get("turn_index", state.get("turn_index", 0)))

        if "last_actor" in game_config:
            state["last_actor"] = str(game_config.get("last_actor", ""))
        if "next_actor" in game_config:
            state["next_actor"] = str(game_config.get("next_actor", "DungeonMaster") or "DungeonMaster")

        if "party" in game_config:
            state["party"] = list(game_config.get("party") or [])
        if "enemies" in game_config:
            state["enemies"] = list(game_config.get("enemies") or [])
        if "initiative_order" in game_config:
            state["initiative_order"] = list(game_config.get("initiative_order") or [])
        if "flags" in game_config:
            state["flags"] = dict(game_config.get("flags") or {})
        if "event_log" in game_config:
            state["event_log"] = list(game_config.get("event_log") or [])

        state["updated_at"] = _now()
        state["state_version"] = int(state.get("state_version", 0)) + 1
        _save_db(db)
        return {"ok": True, "game_id": game_id, "state": state}


@mcp.tool()
def get_state(game_id: GameId = "") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        return {"ok": True, "game_id": game_id, "state": state}


@mcp.tool()
def get_turn_context(game_id: GameId = "", actor: str = "") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        return {
            "ok": True,
            "game_id": game_id,
            "actor": actor,
            "state_version": state.get("state_version", 0),
            "scene_id": state.get("scene_id"),
            "round": state.get("round"),
            "turn_index": state.get("turn_index"),
            "last_actor": state.get("last_actor"),
            "next_actor": state.get("next_actor"),
            "party": state.get("party", []),
            "enemies": state.get("enemies", []),
            "flags": state.get("flags", {}),
            "recent_events": state.get("event_log", [])[-5:],
        }


@mcp.tool()
def get_recent_events(game_id: GameId = "", limit: int = 10) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        events = state.get("event_log", [])[-max(1, min(limit, 100)):]
        return {"ok": True, "game_id": game_id, "events": events}


@mcp.tool()
def append_event(
    game_id: GameId = "",
    event: dict | str | None = None,
    type: str = "",
    actor: str = "",
    target: str = "",
    ability: str = "",
    action: str = "",
    purpose: str = "",
    outcome: str = "",
    details: dict | str | None = None,
) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        if isinstance(event, dict):
            event_obj = dict(event)
        elif isinstance(event, str) and event.strip():
            event_obj = {"type": type or "note", "detail": event.strip()}
            if details:
                event_obj["details"] = details if isinstance(details, dict) else {"note": str(details)}
        else:
            event_obj = {}
        if not event_obj:
            event_obj = {
                "type": type,
                "actor": actor,
                "target": target,
                "ability": ability,
                "action": action,
                "purpose": purpose,
                "outcome": outcome,
            }
            if details:
                event_obj["details"] = details if isinstance(details, dict) else {"note": str(details)}
            event_obj = {k: v for k, v in event_obj.items() if str(v).strip()}
        event_obj.setdefault("timestamp", _now())
        state.setdefault("event_log", []).append(event_obj)
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {"ok": True, "game_id": game_id, "state_version": state["state_version"]}


@mcp.tool()
def apply_patch(game_id: GameId = "", patch: dict = None, reason: str = "", expected_version: int = -1) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        current = int(state.get("state_version", 0))
        if expected_version >= 0 and expected_version != current:
            return {
                "ok": False,
                "error": "version_mismatch",
                "expected_version": expected_version,
                "current_version": current,
            }
        for key, value in patch.items():
            state[key] = value
        if reason:
            state.setdefault("event_log", []).append({
                "type": "patch",
                "reason": reason,
                "patch": patch,
                "timestamp": _now(),
            })
        state["state_version"] = current + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {
            "ok": True,
            "game_id": game_id,
            "state_version": state["state_version"],
            "state": state,
        }


@mcp.tool()
def advance_turn(game_id: GameId = "") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        order = state.get("initiative_order", [])
        current_idx = int(state.get("turn_index", 0))
        if order:
            next_idx = (current_idx + 1) % len(order)
            state["turn_index"] = next_idx
            state["last_actor"] = order[current_idx]
            state["next_actor"] = order[next_idx]
            if next_idx == 0:
                state["round"] = int(state.get("round", 1)) + 1
        else:
            state["turn_index"] = current_idx + 1
            state["round"] = int(state.get("round", 1)) + 1
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {"ok": True, "game_id": game_id, "state": state}


@mcp.tool()
def update_initiative_order(game_id: GameId = "", initiative_order=None) -> dict:
    """Alias: update the initiative_order list. Accepts a list or comma-separated string."""
    game_id = _normalize_game_id(game_id)
    order = initiative_order or []
    if isinstance(order, str):
        # Handle string like "[Thorin, Elara, Shadow, Aldric]" or "Thorin,Elara"
        order = [s.strip().strip("[]'\"") for s in order.replace("[", "").replace("]", "").split(",") if s.strip()]
    if not isinstance(order, list):
        order = list(order)
    return apply_patch(game_id, {"initiative_order": order}, reason="initiative_order updated")


@mcp.tool()
def set_game_result(game_id: GameId = "", result: str = "", summary: str = "") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        state["status"] = "completed"
        state["result"] = result
        state.setdefault("event_log", []).append({
            "type": "game_over",
            "result": result,
            "summary": summary,
            "timestamp": _now(),
        })
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {"ok": True, "game_id": game_id, "result": result, "state": state}


@mcp.tool()
def set_scene(
    game_id: GameId = "",
    scene_id: int = 0,
    scene_title: str = "",
    narration: str = "",
    next_actor: str = "DungeonMaster",
) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        state["scene_id"] = int(scene_id)
        flags = state.setdefault("flags", {})
        if scene_title:
            flags["scene_title"] = scene_title
        order = state.get("initiative_order", [])
        state["next_actor"] = next_actor if (not order or next_actor in order) else "DungeonMaster"
        if narration:
            state.setdefault("event_log", []).append({
                "type": "scene",
                "scene_id": int(scene_id),
                "scene_title": scene_title,
                "narration": narration,
                "timestamp": _now(),
            })
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {"ok": True, "game_id": game_id, "state": state}


@mcp.tool()
def set_enemies(game_id: GameId = "", enemies: list[dict] = None, reason: str = "") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        state["enemies"] = enemies
        if reason:
            state.setdefault("event_log", []).append({
                "type": "enemies",
                "reason": reason,
                "count": len(enemies),
                "timestamp": _now(),
            })
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {"ok": True, "game_id": game_id, "state": state}


@mcp.tool()
def apply_damage(
    game_id: GameId = "",
    target_name: str = "",
    amount: int = 0,
    source: str = "",
    reason: str = "",
    target: str = "",
) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        if not target_name and target:
            target_name = target
        if not target_name:
            return {"ok": False, "error": "target_name is required"}
        bucket, idx, target = _find_character(state, target_name)
        if target is None:
            return {"ok": False, "error": f"target not found: {target_name}"}

        dmg = max(0, abs(int(amount)))
        current = int(target.get("current_hp", 0))
        target["current_hp"] = max(0, current - dmg)
        target["alive"] = target["current_hp"] > 0
        target["incapacitated"] = target["current_hp"] <= 0
        state[bucket][idx] = target

        state.setdefault("event_log", []).append({
            "type": "damage",
            "target": target_name,
            "amount": dmg,
            "source": source,
            "reason": reason,
            "remaining_hp": target["current_hp"],
            "timestamp": _now(),
        })
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {
            "ok": True,
            "game_id": game_id,
            "target": target_name,
            "remaining_hp": target["current_hp"],
            "alive": target["alive"],
            "state": state,
        }


@mcp.tool()
def apply_heal(
    game_id: GameId = "",
    target_name: str = "",
    amount: int = 0,
    source: str = "",
    reason: str = "",
    target: str = "",
) -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        if not target_name and target:
            target_name = target
        if not target_name:
            return {"ok": False, "error": "target_name is required"}
        bucket, idx, target = _find_character(state, target_name)
        if target is None:
            return {"ok": False, "error": f"target not found: {target_name}"}

        heal = max(0, abs(int(amount)))
        current = int(target.get("current_hp", 0))
        max_hp = int(target.get("max_hp", current))
        target["current_hp"] = min(max_hp, current + heal)
        target["alive"] = target["current_hp"] > 0
        target["incapacitated"] = target["current_hp"] <= 0
        state[bucket][idx] = target

        state.setdefault("event_log", []).append({
            "type": "heal",
            "target": target_name,
            "amount": heal,
            "source": source,
            "reason": reason,
            "remaining_hp": target["current_hp"],
            "timestamp": _now(),
        })
        state["state_version"] = int(state.get("state_version", 0)) + 1
        state["updated_at"] = _now()
        _save_db(db)
        return {
            "ok": True,
            "game_id": game_id,
            "target": target_name,
            "remaining_hp": target["current_hp"],
            "alive": target["alive"],
            "state": state,
        }


@mcp.tool()
def check_end_conditions(game_id: GameId = "", boss_name: str = "Shadow Lord") -> dict:
    game_id = _normalize_game_id(game_id)
    with _LOCK:
        db = _load_db()
        state = _ensure_game(db, game_id)
        party_alive = any(_is_alive(p) for p in state.get("party", []))
        enemies = state.get("enemies", [])
        boss_alive = True
        if enemies:
            boss_matches = [e for e in enemies if str(e.get("name", "")).lower() == boss_name.lower()]
            if boss_matches:
                boss_alive = any(_is_alive(e) for e in boss_matches)
            else:
                boss_alive = any(_is_alive(e) for e in enemies)

        result = {"ok": True, "game_id": game_id, "ended": False, "result": None, "summary": ""}

        if not party_alive:
            state["status"] = "completed"
            state["result"] = "DEFEAT"
            result.update({"ended": True, "result": "DEFEAT", "summary": "All party members are down."})
        elif state.get("scene_id", 0) >= 2 and not boss_alive:
            state["status"] = "completed"
            state["result"] = "VICTORY"
            result.update({"ended": True, "result": "VICTORY", "summary": "The final boss is defeated."})

        if result["ended"]:
            state.setdefault("event_log", []).append({
                "type": "game_over",
                "result": result["result"],
                "summary": result["summary"],
                "timestamp": _now(),
            })
            state["state_version"] = int(state.get("state_version", 0)) + 1
            state["updated_at"] = _now()
            _save_db(db)

        result["state"] = state
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    DB_PATH = args.db
    os.environ["GAME_STATE_DB"] = DB_PATH
    mcp.run(transport="stdio")
