"""
MCP Tool Registry
-----------------
Connects to MCP servers over SSE and registers their tools with autogen agents.

All sync→async bridging uses a background thread with its own event loop,
preventing any conflict with nest_asyncio or the main asyncio event loop.
"""

import asyncio
import concurrent.futures
import contextlib
import inspect
import json
import re
import threading
import typing
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Global tool registry — populated during register_mcp_tools()
# maps tool_name → server_url so text-format tool calls can be dispatched
# ---------------------------------------------------------------------------
_TOOL_REGISTRY: dict[str, dict] = {}
# maps tool_name → {param_name: json_type_string} for argument coercion
_TOOL_PROPS_REGISTRY: dict[str, dict] = {}


def get_tool_registry() -> dict[str, dict]:
    """Return the global tool registry."""
    return _TOOL_REGISTRY


def detect_text_tool_call(content: str) -> "dict | None":
    """
    Try to parse a text-formatted tool call from message content.
    Handles:
      - Valid JSON:  {"name": "fetch", "parameters": {"url": "..."}}
      - Python dicts: {"name": "fetch", "parameters": {"url": "...", "raw": False}}
      - Embedded JSON inside prose
    Returns {"name": ..., "arguments": {...}} or None.
    """
    import ast

    def _extract(data: dict) -> "dict | None":
        if not (isinstance(data, dict) and "name" in data):
            return None
        name = data["name"]
        args = data.get("parameters") or data.get("arguments") or {}
        if isinstance(args, dict):
            return {"name": name, "arguments": args}
        return None

    text = content.strip()

    # 1. Try direct JSON parse
    try:
        data = json.loads(text)
        result = _extract(data)
        if result:
            return result
    except Exception:
        pass

    # 2. Normalise Python literals → JSON, then parse
    # Replace standalone Python keywords only (word-boundary safe)
    import re as _re
    normalised = _re.sub(r'\bFalse\b', 'false', text)
    normalised = _re.sub(r'\bTrue\b', 'true', normalised)
    normalised = _re.sub(r'\bNone\b', 'null', normalised)
    try:
        data = json.loads(normalised)
        result = _extract(data)
        if result:
            return result
    except Exception:
        pass

    # 3. Use ast.literal_eval for full Python dict syntax
    try:
        data = ast.literal_eval(text)
        result = _extract(data)
        if result:
            return result
    except Exception:
        pass

    # 4. Find the first {...} object in mixed text and try the above
    for m in _re.finditer(r'\{', text):
        # Find the matching closing brace
        start = m.start()
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    chunk = text[start:i+1]
                    for parser in (
                        lambda s: json.loads(s),
                        lambda s: json.loads(_re.sub(r'\bFalse\b', 'false',
                                             _re.sub(r'\bTrue\b', 'true',
                                             _re.sub(r'\bNone\b', 'null', s)))),
                        lambda s: ast.literal_eval(s),
                    ):
                        try:
                            data = parser(chunk)
                            result = _extract(data)
                            if result:
                                return result
                        except Exception:
                            pass
                    break
    return None


def execute_text_tool_call(name: str, arguments: dict) -> "str | None":
    """
    Execute a tool by name using the global registry.
    Returns the tool result string, or None if the tool is not registered.
    Retries on transient SSE reconnect errors (supergateway restart window).
    """
    import time as _time
    target_info = _TOOL_REGISTRY.get(name)
    if not target_info:
        return None
    transport = target_info.get("transport", "sse")
    target = target_info.get("target")
    # Coerce list/dict → string for str-typed parameters using stored schema
    props = _TOOL_PROPS_REGISTRY.get(name, {})
    arguments = _coerce_args(arguments, props, set())
    last_exc: BaseException = RuntimeError("unknown")
    for attempt in range(4):
        try:
            return _run_in_thread(_call_tool(transport, target, name, arguments))
        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            if attempt < 3 and ("RemoteProtocol" in err_str or
                                "Server disconnected" in err_str or
                                "Connection" in err_str):
                wait = 2 * (attempt + 1)
                print(f"[MCP] text-tool '{name}' attempt {attempt+1} failed "
                      f"({exc!r}); retrying in {wait}s…")
                _time.sleep(wait)
                continue
            raise
    raise last_exc



# ---------------------------------------------------------------------------
# Persistent background event loop — shared across ALL MCP calls
# ---------------------------------------------------------------------------
# Creating a new asyncio loop per call causes httpx to retain stale socket
# state between loops, producing "Server disconnected" errors on the second
# call to any SSE-based MCP server. A single long-lived loop avoids this.

_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_LOCK = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _BG_LOOP
    with _BG_LOCK:
        if _BG_LOOP is None or _BG_LOOP.is_closed():
            _BG_LOOP = asyncio.new_event_loop()

            def _run(lp: asyncio.AbstractEventLoop) -> None:
                asyncio.set_event_loop(lp)
                lp.run_forever()

            t = threading.Thread(target=_run, args=(_BG_LOOP,), daemon=True)
            t.start()
        return _BG_LOOP


def _run_in_thread(coro):
    """
    Submit *coro* to the shared persistent background event loop and block
    until it completes.  Safe to call from sync or async contexts.
    """
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        result = future.result(timeout=60)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError("MCP call timed out after 60 s")
    except BaseException as exc:
        # Python 3.11 asyncio.TaskGroup raises ExceptionGroup; unwrap to
        # surface the real underlying error instead of the opaque wrapper.
        if hasattr(exc, "exceptions") and exc.exceptions:
            raise exc.exceptions[0]
        raise
    return result


# ---------------------------------------------------------------------------
# Session manager — ONE persistent ClientSession per server URL
# ---------------------------------------------------------------------------
# supergateway only supports one active SSE connection per child process.
# Opening a second connection before the first has closed crashes the gateway
# with "Already connected to a transport."  We solve this by keeping a single
# long-lived ClientSession per URL and serialising all calls through it.

class _SessionEntry:
    def __init__(self):
        self.session = None      # mcp.ClientSession
        self.ctx_stack = None    # contextlib.AsyncExitStack
        self.lock = None         # asyncio.Lock (created in bg loop)


_SESSIONS: dict[str, _SessionEntry] = {}
_SESSIONS_LOCK = threading.Lock()


class _StdioSessionEntry:
    def __init__(self):
        self.session = None
        self.ctx_stack = None
        self.lock = None


_STDIO_SESSIONS: dict[str, _StdioSessionEntry] = {}
_STDIO_SESSIONS_LOCK = threading.Lock()


async def _get_session(server_url: str):
    """Return (or create) the persistent ClientSession for *server_url*."""
    from mcp.client.sse import sse_client
    from mcp import ClientSession
    with _SESSIONS_LOCK:
        if server_url not in _SESSIONS:
            _SESSIONS[server_url] = _SessionEntry()
        entry = _SESSIONS[server_url]

    # Create the asyncio.Lock inside the bg loop the first time
    if entry.lock is None:
        entry.lock = asyncio.Lock()

    async with entry.lock:
        # If session is alive, return it
        if entry.session is not None:
            return entry.session, entry.lock

        # Open a fresh SSE connection
        stack = contextlib.AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(sse_client(server_url))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            entry.session = session
            entry.ctx_stack = stack
        except Exception:
            await stack.aclose()
            entry.session = None
            entry.ctx_stack = None
            raise

        return entry.session, entry.lock


async def _invalidate_session(server_url: str):
    """Close and drop the cached session so the next call reconnects."""
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(server_url)
    if entry is None:
        return
    if entry.lock is None:
        entry.session = None
        return
    async with entry.lock:
        if entry.ctx_stack is not None:
            try:
                await entry.ctx_stack.aclose()
            except Exception:
                pass
        entry.session = None
        entry.ctx_stack = None


def _stdio_key(target: dict) -> str:
    command = target.get("command", "")
    args = target.get("args", []) or []
    cwd = target.get("cwd", "") or ""
    return json.dumps({"command": command, "args": args, "cwd": cwd}, sort_keys=True)


async def _get_stdio_session(target: dict):
    """Return (or create) the persistent stdio MCP ClientSession for target command."""
    from mcp.client.stdio import stdio_client
    from mcp import ClientSession, StdioServerParameters

    key = _stdio_key(target)
    with _STDIO_SESSIONS_LOCK:
        if key not in _STDIO_SESSIONS:
            _STDIO_SESSIONS[key] = _StdioSessionEntry()
        entry = _STDIO_SESSIONS[key]

    if entry.lock is None:
        entry.lock = asyncio.Lock()

    async with entry.lock:
        if entry.session is not None:
            return entry.session, entry.lock

        stack = contextlib.AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=target.get("command"),
                args=target.get("args", []) or [],
                env=target.get("env") or None,
                cwd=target.get("cwd") or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            entry.session = session
            entry.ctx_stack = stack
        except Exception:
            await stack.aclose()
            entry.session = None
            entry.ctx_stack = None
            raise

        return entry.session, entry.lock


async def _invalidate_stdio_session(target: dict):
    key = _stdio_key(target)
    with _STDIO_SESSIONS_LOCK:
        entry = _STDIO_SESSIONS.get(key)
    if entry is None:
        return
    if entry.lock is None:
        entry.session = None
        return
    async with entry.lock:
        if entry.ctx_stack is not None:
            try:
                await entry.ctx_stack.aclose()
            except Exception:
                pass
        entry.session = None
        entry.ctx_stack = None


# ---------------------------------------------------------------------------
# Low-level async helpers
# ---------------------------------------------------------------------------

async def _list_tools_sse(server_url: str) -> list:
    """Return the list of Tool objects advertised by an MCP server."""
    # Use a one-shot connection for tool discovery (happens once at startup)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _list_tools_stdio(target: dict) -> list:
    """Return the list of Tool objects advertised by an MCP stdio server."""
    from mcp.client.stdio import stdio_client
    from mcp import ClientSession, StdioServerParameters

    params = StdioServerParameters(
        command=target.get("command"),
        args=target.get("args", []) or [],
        env=target.get("env") or None,
        cwd=target.get("cwd") or None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _list_tools(transport: str, target) -> list:
    if transport == "stdio":
        return await _list_tools_stdio(target)
    return await _list_tools_sse(target)


async def _call_tool_sse(server_url: str, tool_name: str, arguments: dict) -> str:
    """Call *tool_name* on *server_url* via the shared persistent session."""
    for attempt in range(3):
        try:
            session, lock = await _get_session(server_url)
            async with lock:
                result = await session.call_tool(tool_name, arguments)
            if result.content:
                parts = []
                for item in result.content:
                    if hasattr(item, "text") and item.text:
                        parts.append(item.text)
                    elif hasattr(item, "data"):
                        parts.append(str(item.data))
                return "\n".join(parts) if parts else "Success (no output)"
            return "Success (no output)"
        except Exception as exc:
            # Session may have died; invalidate so next attempt reconnects
            await _invalidate_session(server_url)
            if attempt == 2:
                raise
            import asyncio as _aio
            await _aio.sleep(1 * (attempt + 1))


async def _call_tool_stdio(target: dict, tool_name: str, arguments: dict) -> str:
    """Call *tool_name* on a stdio MCP server via a shared persistent session."""
    for attempt in range(3):
        try:
            session, lock = await _get_stdio_session(target)
            async with lock:
                result = await session.call_tool(tool_name, arguments)
            if result.content:
                parts = []
                for item in result.content:
                    if hasattr(item, "text") and item.text:
                        parts.append(item.text)
                    elif hasattr(item, "data"):
                        parts.append(str(item.data))
                return "\n".join(parts) if parts else "Success (no output)"
            return "Success (no output)"
        except Exception:
            await _invalidate_stdio_session(target)
            if attempt == 2:
                raise
            import asyncio as _aio
            await _aio.sleep(1 * (attempt + 1))


async def _call_tool(transport: str, target, tool_name: str, arguments: dict) -> str:
    if transport == "stdio":
        return await _call_tool_stdio(target, tool_name, arguments)
    return await _call_tool_sse(target, tool_name, arguments)



# ---------------------------------------------------------------------------
# Tool factory — builds a properly-typed wrapper from the MCP inputSchema
# ---------------------------------------------------------------------------

_JSON_TO_PYTHON = {"string": str, "number": float, "integer": int, "boolean": bool, "array": list, "object": dict}


def _schema_to_example(schema: dict, depth: int = 0) -> Any:
    """Recursively build a concrete example value from a JSON Schema node."""
    if depth > 4:
        return "..."
    t = schema.get("type", "string") if isinstance(schema, dict) else "string"
    if t == "string":
        # Use param name hint from description, skipping stop words
        desc = schema.get("description", "") if isinstance(schema, dict) else ""
        stop = {"the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "are"}
        words = [w.strip(".()'\"") for w in desc.split() if w.lower().strip(".()'\"") not in stop]
        hint = words[0] if words else "text"
        return hint[:20]
    elif t in ("number", "integer"):
        return 1
    elif t == "boolean":
        return True
    elif t == "array":
        item_schema = schema.get("items", {"type": "string"}) if isinstance(schema, dict) else {"type": "string"}
        return [_schema_to_example(item_schema, depth + 1)]
    elif t == "object":
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not props:
            return {}
        return {k: _schema_to_example(v, depth + 1) for k, v in props.items()}
    return "value"


def _build_description(tool_name: str, base_description: str, input_schema: dict) -> str:
    """Append a concrete usage example to the description for complex schemas."""
    props = input_schema.get("properties", {}) if input_schema else {}
    has_complex = any(
        (v.get("type") if isinstance(v, dict) else v) in ("array", "object")
        for v in props.values()
    )
    if not has_complex:
        return base_description
    example_args = {k: _schema_to_example(v) for k, v in props.items()}
    example_json = json.dumps(example_args, ensure_ascii=False)
    return f"{base_description}\nArgument format example: {example_json}"


def _coerce_args(call_args: dict, props: dict, required: set) -> dict:
    """
    Coerce any argument value to the type declared in the MCP schema.
    props can be either:
      - full inputSchema properties: {"param": {"type": "array", ...}}
      - type-string registry:        {"param": "array"}
    Rules:
      - expected string + got list/dict  → JSON-stringify
      - expected array/object + got str  → JSON-parse back
    """
    coerced = {}

    def _expected_type(raw_schema):
        if isinstance(raw_schema, str):
            return raw_schema
        if not isinstance(raw_schema, dict):
            return None
        direct = raw_schema.get("type")
        if isinstance(direct, str):
            return direct
        union = raw_schema.get("anyOf")
        if isinstance(union, list):
            types = []
            for entry in union:
                if isinstance(entry, dict) and isinstance(entry.get("type"), str):
                    types.append(entry["type"])
            for preferred in ("object", "array", "number", "integer", "boolean", "string"):
                if preferred in types:
                    return preferred
        return None

    for k, v in call_args.items():
        raw = props.get(k) if props else None
        expected_json_type = _expected_type(raw)

        if expected_json_type == "string" and not isinstance(v, str):
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            else:
                v = str(v)
        elif expected_json_type in ("array", "object") and isinstance(v, str):
            # LLM sometimes passes a JSON string for array/object params—parse it back
            try:
                v = json.loads(v)
            except Exception:
                # Last resort for array: wrap the bare string in a list
                if expected_json_type == "array":
                    v = [v]
        coerced[k] = v
    return coerced


def _make_tool_func(transport: str, target, tool_name: str, input_schema: dict) -> Callable:
    """
    Create a synchronous wrapper whose *signature* matches the MCP tool's
    inputSchema so autogen generates a correct JSON schema for the model.
    """
    props    = input_schema.get("properties", {}) if input_schema else {}
    required = set(input_schema.get("required", [])) if input_schema else set()

    # Build a list of inspect.Parameter objects in required-first order
    ordered_names = sorted(props.keys(), key=lambda k: (k not in required, k))
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {"return": str}

    for pname in ordered_names:
        pschema = props[pname]
        raw_type = _JSON_TO_PYTHON.get(pschema.get("type", "string"), str)
        if pname in required:
            py_type = raw_type
            param = inspect.Parameter(
                pname,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=py_type,
            )
        else:
            py_type = Optional[raw_type]
            param = inspect.Parameter(
                pname,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None,
                annotation=py_type,
            )
        params.append(param)
        annotations[pname] = py_type

    # Capture for closure
    _transport = transport
    _target = target
    _name  = tool_name
    _req   = required
    _all   = list(ordered_names)
    _props = props

    def tool_func(*args, **kwargs) -> str:  # type: ignore[override]
        # Merge positional args into kwargs by position
        merged: dict[str, Any] = {}
        for i, val in enumerate(args):
            if i < len(_all):
                merged[_all[i]] = val
        merged.update(kwargs)
        # Drop None optional args
        call_args = {k: v for k, v in merged.items() if v is not None or k in _req}
        # Coerce list/dict → string for str-typed parameters
        call_args = _coerce_args(call_args, _props, _req)
        # Retry on connection errors — supergateway briefly restarts between calls
        import time as _time
        last_exc: BaseException = RuntimeError("unknown")
        for attempt in range(4):
            try:
                return _run_in_thread(_call_tool(_transport, _target, _name, call_args))
            except Exception as exc:
                last_exc = exc
                err_str = str(exc)
                if attempt < 3 and ("RemoteProtocol" in err_str or
                                    "Server disconnected" in err_str or
                                    "Connection" in err_str):
                    wait = 2 * (attempt + 1)   # 2s, 4s, 6s
                    print(f"[MCP] '{_name}' attempt {attempt+1} failed ({exc!r}); "
                          f"retrying in {wait}s…")
                    _time.sleep(wait)
                    continue
                raise
        raise last_exc

    # Attach the proper signature so autogen generates the right JSON schema
    tool_func.__name__        = tool_name
    tool_func.__annotations__ = annotations
    tool_func.__signature__   = inspect.Signature(params, return_annotation=str)
    return tool_func


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_mcp_tools(config: dict, agents: dict, executor) -> None:
    """
    Read the `mcp_servers` block from *config*, connect to each server,
    discover its tools, and register them with the specified autogen agents.

    Parameters
    ----------
    config   : parsed agent_config.yaml dict
    agents   : {agent_key: AssistantAgent} dict built by LocalMultiAgentTeam
    executor : UserProxyAgent used to execute function calls
    """
    from autogen import register_function

    mcp_servers = config.get("mcp_servers", {})
    if not mcp_servers:
        print("[MCP] No mcp_servers found in config — skipping tool registration.")
        return

    print("\n[MCP] Registering tools from MCP servers...")

    for server_key, server_cfg in mcp_servers.items():
        transport = str(server_cfg.get("transport", "sse")).lower()
        target = None
        agent_names = server_cfg.get("agents", [])
        write_tools = set(server_cfg.get("write_tools", []) or [])
        write_agents = set(server_cfg.get("write_agents", []) or [])
        read_only_agents = set(server_cfg.get("read_only_agents", []) or [])

        if transport == "stdio":
            command_spec = server_cfg.get("command")
            if isinstance(command_spec, list) and command_spec:
                command = str(command_spec[0])
                args = [str(x) for x in command_spec[1:]]
            elif isinstance(command_spec, str) and command_spec.strip():
                parts = command_spec.strip().split()
                command = parts[0]
                args = parts[1:]
            else:
                print(f"  [{server_key}] WARNING: stdio server missing 'command' — skipped.")
                continue
            target = {
                "command": command,
                "args": args,
                "env": server_cfg.get("env", {}),
                "cwd": server_cfg.get("cwd"),
            }
            endpoint_label = f"stdio:{command} {' '.join(args)}".strip()
        else:
            url = server_cfg.get("url", "")
            if not url:
                print(f"  [{server_key}] WARNING: no 'url' defined — skipped.")
                continue
            target = url
            endpoint_label = url

        # ---- Discover tools ------------------------------------------------
        tools = None
        for attempt in range(3):
            try:
                tools = _run_in_thread(_list_tools(transport, target))
                break
            except BaseException as exc:
                if attempt < 2:
                    import time; time.sleep(2)
                else:
                    print(f"  [{server_key}] WARNING: cannot reach {endpoint_label} — {exc}")
        if tools is None:
            continue

        if not tools:
            print(f"  [{server_key}] No tools advertised at {endpoint_label}.")
            continue

        print(f"  [{server_key}] {endpoint_label} — {len(tools)} tool(s): "
              f"{[t.name for t in tools]}")

        # ---- Register each tool with the configured agents -----------------
        for tool in tools:
            # Extract inputSchema: MCP Tool objects expose it as .inputSchema
            raw_schema = getattr(tool, "inputSchema", None)
            if hasattr(raw_schema, "model_dump"):
                input_schema = raw_schema.model_dump()  # Pydantic v2
            elif hasattr(raw_schema, "dict"):
                input_schema = raw_schema.dict()         # Pydantic v1
            elif isinstance(raw_schema, dict):
                input_schema = raw_schema
            else:
                input_schema = {}
            fn = _make_tool_func(transport, target, tool.name, input_schema)
            description = _build_description(
                tool.name,
                (tool.description or tool.name).strip(),
                input_schema,
            )
            registered_to = []

            for agent_name in agent_names:
                if agent_name not in agents:
                    print(f"    WARNING: agent '{agent_name}' not found — skipped.")
                    continue

                # Optional per-agent tool restrictions, useful for DM-only state writes.
                if tool.name in write_tools:
                    if write_agents and agent_name not in write_agents:
                        continue
                    if agent_name in read_only_agents:
                        continue

                try:
                    register_function(
                        fn,
                        caller=agents[agent_name],
                        executor=executor,
                        name=tool.name,
                        description=description,
                    )
                    registered_to.append(agent_name)
                except Exception as exc:
                    print(f"    WARNING: could not register '{tool.name}' "
                          f"to '{agent_name}': {exc}")

            if registered_to:
                print(f"    ✓ '{tool.name}' → {registered_to}")
                # Also add to global text-tool-call registry
                _TOOL_REGISTRY[tool.name] = {
                    "transport": transport,
                    "target": target,
                }
                _TOOL_PROPS_REGISTRY[tool.name] = {
                    k: v
                    for k, v in input_schema.get("properties", {}).items()
                }
    print("[MCP] Tool registration complete.\n")
