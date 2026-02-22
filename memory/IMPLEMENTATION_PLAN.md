# D&D AI Agent Game — Implementation Plan (Memory Bank)

> Status: ACTIVE — Core stack deployed and running. Hardening + game-loop completion in progress.
> Last updated: 2026-02-22
> Goal: One-click autonomous AI D&D demo where user watches agents play to a clear Win/Lose conclusion.

---

## 1) Product Goal

Build a complete autonomous D&D game demo on top of current AgentForge:
- Human clicks one button in web UI: **Start Adventure**.
- 5 AI agents run the whole game: **DungeonMaster + 4 Players**.
- Game always terminates with a clear ending:
  - **Victory** (party wins)
  - **Defeat** (party dies / fail condition / timeout)
- No human input during runtime.

This should demonstrate:
- multi-agent coordination
- deterministic tool-based resolution (dice/state/calc)
- real-time streaming narrative
- robust autonomous completion

---

## 2) Rules Alignment From README

Core constraints from `README.md` that architecture must enforce:
- DM is authoritative world/rules controller.
- Players act only on their turn, stay in character.
- All uncertainty resolved by dice; all dice must be logged.
- DM output must include narrative outcome + updated game state + clear next prompt.
- Determinism required: no fabricated rolls, no hidden retroactive changes.

Platform interpretation:
- We convert these into mandatory MCP tool usage rules.
- LLM decides intent; MCP tools decide numeric truth.

---

## 3) Updated Architecture (Per Your Requirements)

### 3.1 Mandatory Change: MCP transport must be local STDIO, not HTTP/SSE

Current setup uses HTTP via `supergateway` containers.
Target setup:
- Run MCP servers as **local subprocesses inside `autogen` container**.
- Connect through MCP stdio client (`stdio` transport), not HTTP endpoints.

Implications:
- Remove dependency on `mcp-*` service containers for gameplay path.
- Lower latency and fewer network failure points.
- Simpler lifecycle: game process owns tool subprocesses.

### 3.2 Required MCP servers for game

Create these 3 first-class MCP servers under `autogen/mcp_servers/`:
1. `dice_server.py` (Dice MCP)
2. `calc_server.py` (Calc MCP)
3. `game_state_server.py` (Game State MCP)

Optional: keep existing external MCP stack only for old dev workflow; D&D path uses STDIO-local servers exclusively.

### 3.3 Authority model (hard rule)

- **All agents** at start of their turn must:
  1) read current state from Game State MCP
  2) use Dice MCP for roll requests relevant to their declared action
- **DungeonMaster only** is allowed to:
  - write/update canonical state in Game State MCP
  - validate player action expressions with Calc MCP
  - finalize outcomes and transitions
- Players are read-only on state.

---

## 4) MCP Server Specifications

## 4.1 Dice MCP (`dice_server.py`)

Purpose:
- deterministic dice rolling and roll auditing

Required tools:
- `roll(notation: str, purpose: str, actor: str) -> dict`
  - supports `d4,d6,d8,d10,d12,d20`
  - supports notation like `1d20+3`, `2d6`, `4d8-1`
  - returns:
    - notation
    - raw rolls
    - modifier
    - total
    - crit flags (`nat20`, `nat1`)
    - timestamp
    - roll_id (unique)
- `validate_notation(notation: str) -> dict`
- `batch_roll(rolls: list[dict]) -> dict`

Determinism:
- every roll emits immutable roll record
- roll_id stored into game state event log

## 4.2 Calc MCP (`calc_server.py`)

Purpose:
- arithmetic/sanity validation for DM during resolution

Required tools:
- `eval_expr(expression: str) -> dict`
  - safe parser only (no arbitrary code)
- `check_threshold(value: float, comparator: str, target: float) -> dict`
  - comparator in `>=, >, <=, <, ==`
- `compute_modifier(attribute: int) -> dict`
  - `floor((attr-10)/2)`
- `sum_damage(parts: list[int], bonus: int=0) -> dict`

DM uses this to verify:
- player-declared assumptions
- AC/HP calculations
- DC pass/fail logic

## 4.3 Game State MCP (`game_state_server.py`)

Purpose:
- single source of truth for world state

State shape (minimum):
- `game_id`, `status`, `scene_id`, `round`, `turn_index`
- `party[]`: hp/max_hp/ac/status/resources
- `enemies[]`: hp/max_hp/ac/status
- `initiative_order[]`
- `flags` (quest flags, puzzle states, boss phases)
- `event_log[]` (narrative + roll ids + deltas)
- `last_actor`, `next_actor`

Required tools:
- Read tools (all agents):
  - `get_state(game_id)`
  - `get_turn_context(game_id, actor)`
  - `get_recent_events(game_id, limit)`
- Write tools (DM only by registration):
  - `init_game(game_config)`
  - `apply_patch(game_id, patch, reason)`
  - `append_event(game_id, event)`
  - `advance_turn(game_id)`
  - `set_game_result(game_id, result, summary)`

Consistency:
- optimistic versioning with `state_version`
- DM updates include `expected_version`
- reject stale writes

---

## 5) Agent Interaction Protocol (Turn-Level)

## 5.1 Global turn protocol

At beginning of every agent turn:
1. agent calls `get_turn_context(game_id, actor)` from Game State MCP
2. agent acts in role
3. required dice usage:
   - if uncertain action => call Dice MCP `roll(...)`
4. DM resolves and writes state updates

## 5.2 Player output contract

Each player response remains:
- Intent
- Action
- Dialogue (optional)

But now also includes MCP-linked grounding:
- references current turn context id
- references roll_id(s) when used

## 5.3 DM output contract

DM response must include:
- narrative outcome
- tool-backed resolution details (roll ids / totals)
- authoritative state delta summary
- explicit next actor prompt

DM performs:
- Calc MCP validation for declared checks/combat math
- State MCP write operations

---

## 6) Orchestrator Design (`autogen/dnd_game.py`)

Create `DnDGame` orchestration layer with:
- agent creation from D&D config
- registration of MCP tools per agent role
- strict speaker selector (DM + 4 players + executor)
- termination checks

### 6.1 Speaker selector policy

Flow:
- DM opens scene/turn
- current player acts
- DM resolves + updates state
- next player
- round advance

During combat:
- order follows initiative in state store

### 6.2 Tool registration policy

- DM: `dice + calc + game_state(read/write)`
- Players: `dice + game_state(read-only)`
- No player write access to state tools

### 6.3 Auto-stop rules

Game ends when any condition met:
- Shadow Lord defeated => victory
- party wiped => defeat
- hard round limit exceeded => defeat
- unrecoverable state error => fail-safe defeat with explanation

On end:
- DM calls `set_game_result(...)`
- emit `GAME_OVER`

---

## 7) Web Server + UI Plan

## 7.1 Backend (`autogen/web_server.py`)

Required endpoints:
- `POST /api/game/start`
- `GET /api/game/{id}/stream` (SSE)
- `GET /api/game/{id}/state`
- `POST /api/game/{id}/stop`

Task runner starts `DnDGame.run_adventure(...)` and streams messages.

SSE event categories:
- `narrative`
- `turn_start`
- `dice_roll`
- `calc_validation`
- `state_update`
- `combat_update`
- `scene_change`
- `game_over`

## 7.2 Frontend (`autogen/static/index.html`)

Single action UX:
- one primary button: **Start Adventure**

Display panels:
- party state cards
- enemies (when present)
- narrative feed
- dice roll feed (with totals/crit indicators)
- game status header (scene, round, current actor)

End screen:
- clear VICTORY/DEFEAT summary

No human turn input UI.

---

## 8) Config + Runtime Changes

## 8.1 `configs/agent_config.yaml`

Replace dev-team config with D&D mode profile:
- agents: `dungeon_master`, `thorin`, `elara`, `shadow`, `aldric`
- mcp_servers must support stdio execution descriptors (not URL)

Proposed schema extension:
```yaml
mcp_servers:
  dice:
    transport: stdio
    command: ["python", "/app/mcp_servers/dice_server.py"]
    agents: [dungeon_master, thorin, elara, shadow, aldric]
  calc:
    transport: stdio
    command: ["python", "/app/mcp_servers/calc_server.py"]
    agents: [dungeon_master]
  game_state:
    transport: stdio
    command: ["python", "/app/mcp_servers/game_state_server.py", "--db", "/memory/game_state.json"]
    agents: [dungeon_master, thorin, elara, shadow, aldric]
```

Agent role policy in config:
- DM prompt explicitly says: only DM writes state
- Players prompted to read turn context first and roll via Dice MCP for uncertain actions

## 8.2 `autogen/mcp_tools.py`

Extend registry to support two transports:
- existing `sse` (legacy)
- new `stdio` (D&D)

Needed additions:
- stdio session manager keyed by command tuple
- tool discovery and call wrappers for stdio client
- per-agent permission filtering for game_state write tools

## 8.3 `docker-compose.yml`

D&D path target:
- keep `ollama`, `web`
- run D&D MCP servers as subprocesses inside `web/autogen` runtime (stdio)
- remove hard dependency on external `mcp-*` containers for D&D mode

Compatibility option:
- preserve old services for legacy software-dev demo mode

---

## 9) Adventure Content Plan (`autogen/adventure.py`)

Use deterministic mini-campaign:
1. Village Hook (RP + info checks)
2. Crypt Entrance (exploration + trap)
3. Skeleton Encounter (combat)
4. Boss Chamber (Shadow Lord)
5. Epilogue

Include explicit fail/success outcomes and bounded branching.

---

## 10) Data Persistence Plan (Memory Bank)

Two persistence layers:
1. `memory/memory.json` (long-term platform memory / meta)
2. `memory/game_state.json` (active run state for Game State MCP)

DM writes major milestones to memory server at:
- scene transitions
- major loot/flag changes
- game result

---

## 11) Safety, Robustness, and Determinism

Hard constraints:
- all dice from Dice MCP only
- all state updates through Game State MCP write tools (DM only)
- all arithmetic validations through Calc MCP (DM)
- no silent state mutation in free text

Operational safeguards:
- turn timeout per agent
- retry policy for transient MCP subprocess failures
- max rounds cap
- max consecutive invalid actions cap

---

## 12) Implementation Status

### ✅ DONE — Fully Implemented and Deployed

#### Infrastructure
- [x] Docker Compose stack: `ai-web`, `ollama`, legacy `mcp-*` containers all running
- [x] `ai-web` container bind-mounts `./autogen:/app` — no rebuild needed for code changes
- [x] `memory/game_state.json` persists game state across container restarts

#### MCP Servers (`autogen/mcp_servers/`)
- [x] `dice_server.py` — `roll`, `validate_notation`, `batch_roll`; hardened: non-dice text → 1d20 fallback
- [x] `calc_server.py` — `eval_expr` (accepts `str|int|float`), `check_threshold`, `compute_modifier`, `sum_damage`
- [x] `game_state_server.py` — all 12 tools; `game_id` is now optional on all tools (auto-resolves to latest running game); string `event`/`details` normalised to dicts

#### Agent Orchestration (`autogen/dnd_game.py`)
- [x] `DnDGame` class with 5 agents: `DungeonMaster`, `Thorin`, `Elara`, `Shadow`, `Aldric`
- [x] AutoGen `GroupChat` + `GroupChatManager` with custom speaker selector
- [x] Dual tool-call interception: text-format intercept (`_text_tool_intercept`) + native `tool_calls` path
- [x] `_sanitize_tool_args` — client-side hardening for text-intercept path
- [x] MCP tool permissions: DM has write tools, players get read-only game_state
- [x] Game initialised via `init_game` with full party from `adventure.py`

#### Config (`configs/agent_config.yaml`)
- [x] All 5 D&D agents with character-accurate system prompts
- [x] STDIO MCP server descriptors for dice, calc, game_state
- [x] Per-agent tool permission lists (`write_agents`, `read_only_agents`)

#### Web Server (`autogen/web_server.py`)
- [x] `POST /api/game/start` — creates task, spawns DnDGame async
- [x] `GET /api/tasks/{id}/stream` — SSE stream of all messages
- [x] `GET /api/tasks/{id}` — full task + message history
- [x] `POST /api/tasks/{id}/stop` and `POST /api/tasks/stop-all`
- [x] `GET /api/tasks` — task list for sidebar polling

#### Frontend (`autogen/static/index.html`)
- [x] Single-button **Start Adventure** UI
- [x] Sidebar with task list, status badges, message count
- [x] **Log tab** — full agent feed with tool-call badges and markdown rendering
- [x] **Story tab** — narrative-only view; filters out JSON/tool-call lines; larger readable font
- [x] Correct D&D agent names, colours, initials (`DM`, `Thorin`, `Elara`, `Shadow`, `Aldric`)
- [x] Agent legend with role labels (Fighter, Wizard, Rogue, Cleric)
- [x] Stop / Stop All buttons; running-task pulse animation

#### Validation Hardening (all errors observed in live runs fixed)
| Error | Fix location |
|---|---|
| `roll` — non-dice text (e.g. "Perception") | `dice_server.py` `_parse_notation` |
| `append_event` — flat args without `event` dict | `dnd_game.py` `_sanitize_tool_args` |
| `append_event` — `event` as plain string | `game_state_server.py` type `dict\|str\|None` |
| `append_event` — `details` as plain string | `game_state_server.py` type `dict\|str\|None` |
| `eval_expr` — numeric value passed | `calc_server.py` type `str\|int\|float` |
| `apply_damage` — `target` alias instead of `target_name` | `game_state_server.py` alias fallback |
| `set_scene` — `game_id` missing via native tool_calls | All tools: `game_id` optional, auto-resolve |
| `mcp_tools` — union/anyOf schema coerced to string | `mcp_tools.py` `_expected_type()` helper |

---

### ✅ RESOLVED — Previously Known Issues

| Issue | Fix |
|---|---|
| `advance_turn` never called by DM | `_dm_turn_done_hook()` in `dnd_game.py`: auto-injects `advance_turn` after every DM narrative turn |
| `update_initiative_order` hallucination | `game_state_server.py`: new alias tool that delegates to `apply_patch` |
| `next_actor` not respected | Speaker selector now calls `_get_next_actor_from_state()` after every DM turn |
| Scene stuck at 0 forever | `_dm_turn_done_hook`: forces `set_scene` if `round > (scene+1)*8` |
| No game-state panel in UI | `#status-bar` above tab bar: scene, round, actor, party HP bars, enemy HP bars |

**Verified in task `e8c1d7fe`:**
- `round=2, turn_idx=0, next_actor=DungeonMaster` after ~5 minutes → full round cycled correctly
- `state_version=17`, `events=10` → state updating, events logging
- Zero validation errors in 4-minute scan

### 🔴 REMAINING — Open Issues

#### P1 — Full combat loop not yet sustained
- `apply_damage` / `apply_heal` are rarely called in sequence for a full combat exchange
- Enemies are set with `set_enemies` but DM doesn't consistently drive attack/damage resolution each round
- **Fix needed:** DM system prompt needs explicit combat round checklist: `set_enemies → roll attack → apply_damage → check HP → advance_turn`

#### P2 — GAME_OVER not yet triggered in practice
- `check_end_conditions` now runs every DM turn (via hook), but victory requires `scene_id >= 2` + `boss_alive=False`, which hasn't been reached yet
- **Fix needed:** Auto-scene-advance will eventually push into scene 2; once enemies die, hook will detect victory. May need to reduce scene threshold or seed enemies earlier.

---

## 13) Next Work Items (Ordered)

### ✅ 13.1 Fix `advance_turn` — DONE
Auto-injected in `_dm_turn_done_hook` in `dnd_game.py`. After every DM narrative turn, if no `advance_turn` found in last 25 messages, orchestrator calls it directly via MCP.

### ✅ 13.2 Remove `update_initiative_order` hallucination — DONE
Added `update_initiative_order` alias tool in `game_state_server.py` that delegates to `apply_patch` with `initiative_order` field.

### ✅ 13.3 Enforce strict `next_actor` speaker selection — DONE
`_get_next_actor_from_state()` closure in `dnd_game.py` reads `next_actor` from live game state and selects the correct agent. Used as the final fallback in `speaker_selector` after every DM turn.

### ✅ 13.4 Add game-state status panel to UI — DONE
`#status-bar` in `index.html` polls `/api/game/{id}/state` every 4s. Shows: scene name pill, current actor pill (colored), party HP bars (green >60%/amber >30%/red), enemy HP bars (red, only alive enemies).

### ✅ 13.5 Scene progression protocol — DONE
`_dm_turn_done_hook` forces `set_scene` every 8 rounds per scene (`SCENE_THRESHOLD=8`). Scene titles: `["The Village of Millhaven", "Crypt Entrance", "The Shadow Lord's Chamber"]`.

---

### ✅ 13.6 Drive sustained combat loop — DONE
Combat nudge added to `_dm_turn_done_hook`: when enemies are alive and no `apply_damage`/`apply_heal` in last 10 events, injects a `[GameEngine] COMBAT ACTIVE` message forcing DM to resolve combat. Added `COMBAT PROTOCOL` section to DM system prompt in `agent_config.yaml`.

**Verified:** Task `03e9fb06` — `[hook] combat nudge injected — enemies: Sorceress` fired 3 times; Aldric HP 24→9, Elara HP 18→17; ended `VICTORY`.

### ✅ 13.7 Auto-seed enemies on scene advance — DONE
In the scene-advance branch of `_dm_turn_done_hook`, after `set_scene`: seeds 2 skeletons for scene 1 (via `create_skeleton()`) and Shadow Lord for scene 2 (via `create_shadow_lord()`). DM can override with its own `set_enemies` call.

---

## 14) Definition of Done (Updated)

Project is done when all are true:
- [x] User clicks Start Adventure → game immediately begins streaming
- [x] All 5 agents (DM + 4 players) appear and speak in narrative
- [x] Dice rolls are MCP-backed (no fabricated numbers)
- [x] Game state is persisted in `game_state.json`
- [x] Story tab shows clean human-readable narrative
- [x] **Turn order follows `next_actor` from game state (enforced via `_get_next_actor_from_state()`)**
- [x] **`advance_turn` is called reliably every round (auto-injected by `_dm_turn_done_hook`)**
- [x] **Game progresses through at least 3 scenes (forced every 8 rounds per scene)**
- [x] **Combat occurs with real damage/heal exchanges (apply_damage/apply_heal consistently called via nudge)**
- [x] **Game terminates with GAME_OVER: VICTORY or GAME_OVER: DEFEAT message**
- [x] **UI shows live party/enemy HP panel (status bar above tab bar)**

---

## 15) Architecture Reference (Current State)

```
Browser
  └─ GET /         → static/index.html
  └─ POST /api/game/start
  └─ GET  /api/tasks/{id}/stream  (SSE)
  └─ GET  /api/tasks              (poll sidebar)

ai-web container (port 8080)
  └─ web_server.py (FastAPI)
       └─ DnDGame.run_adventure()
            └─ AutoGen GroupChat (5 agents)
                 ├─ DungeonMaster  → dice + calc + game_state(write)
                 ├─ Thorin         → dice + game_state(read)
                 ├─ Elara          → dice + game_state(read)
                 ├─ Shadow         → dice + game_state(read)
                 └─ Aldric         → dice + game_state(read)
            └─ MCP clients (stdio subprocesses)
                 ├─ dice_server.py
                 ├─ calc_server.py
                 └─ game_state_server.py ──→ memory/game_state.json

ollama container (port 11434)
  └─ llama3.1:8b  (all agents share same model)
```

## 16) File Index

| File | Purpose | Status |
|---|---|---|
| `autogen/dnd_game.py` | Game orchestrator, speaker selector, tool interception | ✅ Live, needs advance_turn fix |
| `autogen/web_server.py` | FastAPI backend, SSE streaming, task management | ✅ Live |
| `autogen/mcp_tools.py` | MCP stdio client, tool registry, arg coercion | ✅ Live |
| `autogen/adventure.py` | Party/enemy definitions, scene content | ✅ Live |
| `autogen/static/index.html` | Single-page UI, Log + Story tabs | ✅ Live |
| `autogen/mcp_servers/dice_server.py` | Dice MCP (roll, validate, batch) | ✅ Live |
| `autogen/mcp_servers/calc_server.py` | Calc MCP (eval, threshold, modifier) | ✅ Live |
| `autogen/mcp_servers/game_state_server.py` | State MCP (all 12 tools, auto game_id) | ✅ Live |
| `configs/agent_config.yaml` | Agent prompts, MCP descriptors, permissions | ✅ Live |
| `memory/game_state.json` | Persisted game state (written by state MCP) | ✅ Live |
| `docker-compose.yml` | Container definitions, volume mounts | ✅ Live |

