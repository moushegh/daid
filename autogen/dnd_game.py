import nest_asyncio
nest_asyncio.apply()

import asyncio
import json
import os
import re
import uuid
import warnings
import yaml

try:
    from adventure import create_party, create_skeleton, create_shadow_lord
except Exception:
    create_party = None
    create_skeleton = None
    create_shadow_lord = None

from autogen import AssistantAgent, GroupChat, GroupChatManager, UserProxyAgent
from mcp_tools import (
    detect_text_tool_call,
    execute_text_tool_call,
    get_tool_registry,
    register_mcp_tools,
)

warnings.filterwarnings(
    "ignore", category=UserWarning, message="Function '.*' is being overridden.*"
)


class _NotifyList(list):
    def __init__(self, queue):
        self._queue = queue
        super().__init__()

    def append(self, item):  # type: ignore[override]
        super().append(item)
        try:
            self._queue.put_nowait(item)
        except Exception:
            pass


class DnDGame:
    def __init__(self, config_path="/configs/agent_config.yaml"):
        self.config_path = config_path
        self.agents = {}
        self.turn_cycle = []
        self.game_id = str(uuid.uuid4())[:8]
        self.load_config()

    def load_config(self):
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        model = self.config["models"]["local_llm"]
        self.llm_config = {
            "config_list": [{
                "model": model["model"],
                "base_url": model["base_url"],
                "api_key": "not-needed",
                "price": model.get("price", [0, 0]),
            }],
            "temperature": model.get("temperature", 0.3),
            "max_tokens": model.get("max_tokens", 1200),
            "top_p": 0.9,
        }

    def create_agents(self):
        for agent_key, agent_cfg in self.config["agents"].items():
            self.agents[agent_key] = AssistantAgent(
                name=agent_cfg["name"],
                system_message=agent_cfg["system_message"],
                llm_config=self.llm_config,
                code_execution_config=agent_cfg.get("code_execution_config", False),
            )

        self.user_proxy = UserProxyAgent(
            name="GameEngine",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=100000,
            is_termination_msg=lambda x: "GAME_OVER" in (x.get("content") or ""),
            code_execution_config=False,
        )

        register_mcp_tools(self.config, self.agents, self.user_proxy)

        order = self.config.get("turn_order", [
            "dungeon_master",
            "thorin",
            "elara",
            "shadow",
            "aldric",
        ])
        self.turn_cycle = [name for name in order if name in self.agents]
        if not self.turn_cycle:
            raise ValueError("No valid turn_order agents configured")

    async def run_adventure(self, message_queue=None):
        pending_text_caller = {"name": None}
        repeated_tool_call = {"sig": None, "count": 0}

        cycle = self.turn_cycle
        dm_agent = self.agents[cycle[0]]
        name_to_agent = {a.name: a for a in self.agents.values()}
        name_to_key = {v["name"]: k for k, v in self.config["agents"].items()}

        def next_in_cycle(current_name: str):
            current_key = name_to_key.get(current_name)
            if current_key not in cycle:
                return self.agents[cycle[0]]
            idx = cycle.index(current_key)
            return self.agents[cycle[(idx + 1) % len(cycle)]]

        def _dm_turn_done_hook(messages):
            """
            Called once per DM narrative turn (no pending tool call).
            1. Auto-injects advance_turn if DM didn't call it this turn.
            2. Auto-advances scene if game is stalled.
            3. Calls check_end_conditions and signals GAME_OVER if ended.
            Returns end_data dict if game ended, else None.
            """
            recent_n = list(messages)[-25:]

            # Was advance_turn already called this DM turn?
            advance_called = any(
                "advance_turn" in (m.get("content") or "")
                for m in recent_n
                if m.get("name") == "GameEngine"
            ) or any(
                any(
                    tc.get("function", {}).get("name") == "advance_turn"
                    for tc in (m.get("tool_calls") or [])
                )
                for m in recent_n
                if m.get("name") == dm_agent.name
            )

            if not advance_called:
                try:
                    execute_text_tool_call("advance_turn", {"game_id": self.game_id})
                    print(f"[hook] auto advance_turn for game {self.game_id}")
                except Exception as exc:
                    print(f"[hook] advance_turn failed: {exc}")

            # Scene auto-advance: force scene change every 8 rounds per scene
            try:
                state_raw = execute_text_tool_call("get_state", {"game_id": self.game_id})
                state_data = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
                gs = (state_data.get("state") if isinstance(state_data, dict) else {}) or {}
                current_round = int(gs.get("round", 1))
                current_scene = int(gs.get("scene_id", 0))
                _SCENE_TITLES = [
                    "The Village of Millhaven",
                    "Crypt Entrance",
                    "The Shadow Lord's Chamber",
                ]
                _SCENE_THRESHOLD = 8  # rounds per scene before forced advance
                advance_at = (current_scene + 1) * _SCENE_THRESHOLD
                if current_scene < 2 and current_round > advance_at:
                    next_sid = current_scene + 1
                    title = _SCENE_TITLES[next_sid] if next_sid < len(_SCENE_TITLES) else f"Scene {next_sid}"
                    execute_text_tool_call("set_scene", {
                        "game_id": self.game_id,
                        "scene_id": next_sid,
                        "scene_title": title,
                        "narration": "The party presses onward.",
                        "next_actor": "DungeonMaster",
                    })
                    # Auto-seed enemies for the new scene
                    try:
                        if next_sid == 1 and create_skeleton is not None:
                            sk1 = create_skeleton().to_dict()
                            sk2 = create_skeleton().to_dict()
                            sk1["name"] = "Skeleton Guard 1"
                            sk2["name"] = "Skeleton Guard 2"
                            execute_text_tool_call("set_enemies", {
                                "game_id": self.game_id,
                                "enemies": [sk1, sk2],
                            })
                            print(f"[hook] seeded 2 skeletons for scene {next_sid}")
                        elif next_sid == 2 and create_shadow_lord is not None:
                            sl = create_shadow_lord().to_dict()
                            execute_text_tool_call("set_enemies", {
                                "game_id": self.game_id,
                                "enemies": [sl],
                            })
                            print(f"[hook] seeded Shadow Lord for scene {next_sid}")
                    except Exception as _seed_exc:
                        print(f"[hook] enemy seeding failed: {_seed_exc}")
                    messages.append({
                        "role": "user",
                        "name": "GameEngine",
                        "content": (
                            f"[auto] Scene advanced to {next_sid}: '{title}' after round {current_round}. "
                            f"Enemies have been placed in this area. "
                            f"DungeonMaster: describe the new scene and the enemies threatening the party, "
                            f"then begin combat by calling apply_damage when enemies attack."
                        ),
                    })
                    print(f"[hook] auto scene advance → scene {next_sid} after round {current_round}")
            except Exception as exc:
                print(f"[hook] scene auto-advance failed: {exc}")

            # Combat activity nudge: if enemies alive but no damage/heal in last 10 events
            try:
                cr_raw = execute_text_tool_call("get_state", {"game_id": self.game_id})
                cr_data = json.loads(cr_raw) if isinstance(cr_raw, str) else (cr_raw or {})
                cr_gs = (cr_data.get("state") if isinstance(cr_data, dict) else {}) or {}
                cr_enemies = [e for e in (cr_gs.get("enemies") or []) if e.get("alive") is not False and (e.get("current_hp") or 0) > 0]
                if cr_enemies:
                    cr_events = cr_gs.get("events") or []
                    recent_events = cr_events[-10:]
                    combat_seen = any(
                        ev.get("type") in ("apply_damage", "apply_heal") or
                        "apply_damage" in str(ev) or "apply_heal" in str(ev)
                        for ev in recent_events
                    )
                    if not combat_seen:
                        enemy_names = ", ".join(e["name"] for e in cr_enemies)
                        messages.append({
                            "role": "user",
                            "name": "GameEngine",
                            "content": (
                                f"[GameEngine] COMBAT ACTIVE — enemies present: {enemy_names}. "
                                f"DungeonMaster: you MUST call apply_damage(game_id, target_name, amount) "
                                f"when enemies attack and apply_heal when healing occurs. "
                                f"Do NOT skip combat mechanics."
                            ),
                        })
                        print(f"[hook] combat nudge injected — enemies: {enemy_names}")
            except Exception as _combat_exc:
                print(f"[hook] combat nudge check failed: {_combat_exc}")

            # Check end conditions
            try:
                end_raw = execute_text_tool_call("check_end_conditions", {"game_id": self.game_id})
                end_data = json.loads(end_raw) if isinstance(end_raw, str) else (end_raw or {})
                if isinstance(end_data, dict) and end_data.get("ended"):
                    result = end_data.get("result", "DEFEAT")
                    messages.append({
                        "role": "user",
                        "name": "GameEngine",
                        "content": f"GAME_OVER: {result}",
                    })
                    print(f"[hook] game ended: {result}")
                    return end_data
            except Exception as exc:
                print(f"[hook] check_end_conditions failed: {exc}")

            return None

        def _get_next_actor_from_state():
            """Read next_actor from live game state; fall back to next_in_cycle."""
            try:
                state_raw = execute_text_tool_call("get_state", {"game_id": self.game_id})
                state_data = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
                gs = (state_data.get("state") if isinstance(state_data, dict) else {}) or {}
                next_actor = str(gs.get("next_actor", "")).strip()
                if next_actor and next_actor in name_to_agent:
                    return name_to_agent[next_actor]
            except Exception:
                pass
            return next_in_cycle(dm_agent.name)

        def speaker_selector(last_speaker, groupchat):
            messages = groupchat.messages
            if not messages:
                return self.agents[cycle[0]]

            last_msg = messages[-1]
            content = (last_msg.get("content") or "").strip()
            has_game_over_line = any(
                line.strip().upper().startswith("GAME_OVER:")
                for line in content.splitlines()
            )

            if has_game_over_line:
                return None

            if last_msg.get("tool_calls"):
                pending_text_caller["name"] = None
                return self.user_proxy

            if content and last_speaker.name != self.user_proxy.name:
                parsed = detect_text_tool_call(content)
                if parsed and parsed["name"] in get_tool_registry():
                    sig = (
                        last_speaker.name,
                        parsed["name"],
                        json.dumps(parsed.get("arguments") or {}, sort_keys=True),
                    )
                    if repeated_tool_call["sig"] == sig:
                        repeated_tool_call["count"] += 1
                    else:
                        repeated_tool_call["sig"] = sig
                        repeated_tool_call["count"] = 1

                    if repeated_tool_call["count"] >= 3:
                        print(f"[loop-break] repeated tool call suppressed: {sig}")
                        repeated_tool_call["sig"] = None
                        repeated_tool_call["count"] = 0
                        pending_text_caller["name"] = None
                        if last_speaker.name == dm_agent.name:
                            return next_in_cycle(last_speaker.name)
                        return dm_agent

                    pending_text_caller["name"] = last_speaker.name
                    return self.user_proxy

            if last_speaker.name == self.user_proxy.name:
                if pending_text_caller["name"] and pending_text_caller["name"] in name_to_agent:
                    back = pending_text_caller["name"]
                    pending_text_caller["name"] = None
                    return name_to_agent[back]
                for msg in reversed(messages[:-1]):
                    if msg.get("tool_calls"):
                        caller = msg.get("name", "")
                        if caller in name_to_agent:
                            return name_to_agent[caller]
                return self.agents[cycle[0]]

            # DM just delivered a pure-narrative turn — run the post-DM hook,
            # then enforce next_actor from authoritative game state.
            if last_speaker.name == dm_agent.name:
                end_data = _dm_turn_done_hook(messages)
                if end_data:
                    return None  # terminates GroupChat
                return _get_next_actor_from_state()

            return next_in_cycle(last_speaker.name)

        group_chat = GroupChat(
            agents=[self.agents[k] for k in cycle] + [self.user_proxy],
            messages=[],
            max_round=100000,
            speaker_selection_method=speaker_selector,
            allow_repeat_speaker=True,
        )

        if message_queue is not None:
            group_chat.messages = _NotifyList(message_queue)

        manager = GroupChatManager(groupchat=group_chat, llm_config=self.llm_config)

        initial_party = []
        if create_party is not None:
            try:
                initial_party = [c.to_dict() for c in create_party()]
            except Exception:
                initial_party = []

        # Bootstrap canonical game state once before LLM turn loop.
        if "init_game" in get_tool_registry():
            execute_text_tool_call("init_game", {
                "game_config": {
                    "game_id": self.game_id,
                    "status": "running",
                    "scene_id": 0,
                    "round": 1,
                    "turn_index": 0,
                    "last_actor": "",
                    "next_actor": "DungeonMaster",
                    "party": initial_party,
                    "enemies": [],
                    "initiative_order": ["DungeonMaster", "Thorin", "Elara", "Shadow", "Aldric"],
                    "flags": {"adventure": "crypt_of_the_shadow_lord"},
                    "event_log": [],
                }
            })

        valid_actor_names = {a.name for a in self.agents.values()}

        def _normalize_game_id_value(value):
            text = str(value or "").strip()
            if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
                text = text[1:-1].strip()
            return text

        def _sanitize_tool_args(tool_name: str, tool_args: dict, sender_name: str | None) -> dict:
            args = dict(tool_args or {})

            game_id_tools = {
                "get_state",
                "get_turn_context",
                "get_recent_events",
                "append_event",
                "apply_patch",
                "advance_turn",
                "set_game_result",
                "set_scene",
                "set_enemies",
                "apply_damage",
                "apply_heal",
                "check_end_conditions",
            }

            if tool_name in game_id_tools:
                raw_game_id = _normalize_game_id_value(args.get("game_id", ""))
                args["game_id"] = raw_game_id or self.game_id

            if tool_name == "init_game":
                game_config = dict(args.get("game_config") or {})
                raw_game_id = _normalize_game_id_value(game_config.get("game_id", ""))
                game_config["game_id"] = raw_game_id or self.game_id
                game_config.setdefault("status", "running")
                game_config.setdefault("round", 1)
                game_config.setdefault("turn_index", 0)
                game_config.setdefault("next_actor", "DungeonMaster")
                game_config.setdefault("initiative_order", ["DungeonMaster", "Thorin", "Elara", "Shadow", "Aldric"])
                if initial_party:
                    game_config.setdefault("party", initial_party)
                args["game_config"] = game_config

            if tool_name == "get_turn_context":
                actor = str(args.get("actor") or "").strip()
                if actor not in valid_actor_names:
                    fallback = sender_name if sender_name in valid_actor_names else "DungeonMaster"
                    args["actor"] = fallback

            if tool_name == "set_scene":
                next_actor = str(args.get("next_actor") or "").strip()
                if next_actor and next_actor not in valid_actor_names:
                    args["next_actor"] = "DungeonMaster"

            if tool_name == "append_event":
                event_value = args.get("event")
                if not isinstance(event_value, dict):
                    wrapped_event = {
                        k: v
                        for k, v in args.items()
                        if k != "game_id"
                    }
                    args = {
                        "game_id": args.get("game_id", self.game_id),
                        "event": wrapped_event,
                    }

            if tool_name == "roll":
                notation = str(args.get("notation") or "").strip()
                is_dice_notation = bool(re.search(r"\d*\s*d\s*\d+", notation.lower()))
                if not notation:
                    args["notation"] = "1d20"
                elif not is_dice_notation:
                    if not str(args.get("purpose") or "").strip():
                        args["purpose"] = notation
                    args["notation"] = "1d20"

            if tool_name == "eval_expr" and "expression" in args:
                if not isinstance(args.get("expression"), str):
                    args["expression"] = str(args.get("expression"))

            if tool_name in {"apply_damage", "apply_heal"}:
                if not str(args.get("target_name") or "").strip():
                    target_alias = str(args.get("target") or "").strip()
                    if target_alias:
                        args["target_name"] = target_alias
                if "amount" in args:
                    try:
                        args["amount"] = abs(int(args.get("amount")))
                    except Exception:
                        pass

            return args

        def _text_tool_intercept(recipient, messages=None, sender=None, config=None):
            if not messages:
                return False, None
            last = messages[-1]
            content = (last.get("content") or "").strip()
            if not content:
                return False, None
            parsed = detect_text_tool_call(content)
            if not parsed:
                return False, None
            tool_name = parsed["name"]
            tool_args = parsed.get("arguments") or {}
            caller_name = str(last.get("name") or "").strip() or getattr(sender, "name", None)
            tool_args = _sanitize_tool_args(tool_name, tool_args, caller_name)
            if tool_name not in get_tool_registry():
                return False, None
            try:
                result = execute_text_tool_call(tool_name, tool_args)
                if result is None:
                    return False, None
                return True, f"Tool '{tool_name}' result:\n{result}"
            except Exception as exc:
                return True, f"Tool '{tool_name}' error: {exc}"

        self.user_proxy.register_reply(
            trigger=lambda _: True,
            reply_func=_text_tool_intercept,
            position=0,
        )

        intro = f"""ADVENTURE START

Game ID: {self.game_id}

This is an autonomous D&D session.
- At the START of EACH turn, the active agent MUST call get_turn_context(game_id, actor).
- If an action has uncertainty, use roll(notation, purpose, actor) from dice MCP.
- DungeonMaster is the ONLY agent allowed to update authoritative state via game_state MCP writes.
- DungeonMaster uses calc MCP to validate key calculations before state updates.
- DungeonMaster should use set_scene / set_enemies / apply_damage / apply_heal for progression.
- DungeonMaster must call check_end_conditions(game_id) each DM turn.

DungeonMaster: narrate the opening scene and begin round 1.
State has already been initialized for this Game ID; do not call init_game unless explicit recovery is required.
When final outcome is reached, call set_game_result() and then write GAME_OVER: VICTORY or GAME_OVER: DEFEAT.
Do not repeat the same tool call more than once without progressing narrative or state.
"""

        await self.user_proxy.a_initiate_chat(
            manager,
            message=intro,
            clear_history=True,
        )
