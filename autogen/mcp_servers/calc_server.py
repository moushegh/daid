import ast
import math
import operator

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calc")

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")


@mcp.tool()
def eval_expr(expression: str | int | float) -> dict:
    expression = str(expression)
    try:
        tree = ast.parse(expression, mode="eval")
        value = _safe_eval(tree)
        return {"ok": True, "expression": expression, "value": value}
    except Exception as exc:
        return {"ok": False, "expression": expression, "error": str(exc)}


@mcp.tool()
def check_threshold(value: float, comparator: str, target: float) -> dict:
    ops = {
        ">=": operator.ge,
        ">": operator.gt,
        "<=": operator.le,
        "<": operator.lt,
        "==": operator.eq,
    }
    if comparator not in ops:
        return {"ok": False, "error": f"Invalid comparator: {comparator}"}
    result = ops[comparator](value, target)
    return {
        "ok": True,
        "value": value,
        "comparator": comparator,
        "target": target,
        "result": bool(result),
    }


@mcp.tool()
def compute_modifier(attribute: int) -> dict:
    mod = math.floor((attribute - 10) / 2)
    return {"ok": True, "attribute": attribute, "modifier": mod}


@mcp.tool()
def sum_damage(parts: list[int], bonus: int = 0) -> dict:
    total = int(sum(parts) + bonus)
    return {"ok": True, "parts": parts, "bonus": bonus, "total": total}


if __name__ == "__main__":
    mcp.run(transport="stdio")
