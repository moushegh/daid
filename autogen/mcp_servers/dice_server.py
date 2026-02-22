import random
import re
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("dice")

_ALLOWED = {4, 6, 8, 10, 12, 20}


def _parse_notation(notation: str) -> tuple[int, int, int]:
    text = notation.strip().lower()
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        text = text[1:-1].strip()
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.translate(str.maketrans({"＋": "+", "−": "-", "–": "-", "—": "-"}))
    core = re.search(r"(\d*)d(\d+)", text)
    if not core:
        if re.search(r"[a-z]", text):
            return 1, 20, 0
        raise ValueError(f"Invalid dice notation: {notation}")

    count = int(core.group(1)) if core.group(1) else 1
    sides = int(core.group(2))

    tail = re.sub(r"\s+", "", text[core.end():])
    mod_match = re.search(r"([+-])(\d+)", tail)
    if mod_match:
        magnitude = int(mod_match.group(2))
        modifier = magnitude if mod_match.group(1) == "+" else -magnitude
    else:
        modifier = 0
    if count < 1 or count > 20:
        raise ValueError(f"Invalid dice count: {count}")
    if sides not in _ALLOWED:
        raise ValueError(f"Unsupported die type: d{sides}")
    return count, sides, modifier


@mcp.tool()
def validate_notation(notation: str) -> dict:
    try:
        count, sides, modifier = _parse_notation(notation)
        return {
            "ok": True,
            "notation": notation.strip().lower(),
            "count": count,
            "sides": sides,
            "modifier": modifier,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "notation": notation}


@mcp.tool()
def roll(notation: str, purpose: str = "", actor: str = "") -> dict:
    count, sides, modifier = _parse_notation(notation)
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier
    nat20 = sides == 20 and count == 1 and rolls[0] == 20
    nat1 = sides == 20 and count == 1 and rolls[0] == 1
    return {
        "ok": True,
        "roll_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "notation": notation.strip().lower(),
        "purpose": purpose,
        "actor": actor,
        "rolls": rolls,
        "modifier": modifier,
        "total": total,
        "nat20": nat20,
        "nat1": nat1,
    }


@mcp.tool()
def batch_roll(rolls: list[dict]) -> dict:
    results = []
    for entry in rolls:
        notation = str(entry.get("notation", "1d20"))
        purpose = str(entry.get("purpose", ""))
        actor = str(entry.get("actor", ""))
        results.append(roll(notation=notation, purpose=purpose, actor=actor))
    return {"ok": True, "count": len(results), "results": results}


if __name__ == "__main__":
    mcp.run(transport="stdio")
