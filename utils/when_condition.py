"""Safe validation and evaluation for Dual Gate ``When`` expressions."""

from __future__ import annotations

import ast
import re
from typing import Any, Mapping, Optional, Set


_ALWAYS = {"", "always", "all"}
_ALLOWED_NODE_TYPES = {
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Name,
    ast.Load,
    ast.Constant,
}


def expression_name(value: object) -> str:
    """Return the identifier used for a context key inside an expression."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(value))


def context_namespace(context: Mapping[str, Any]) -> dict[str, Any]:
    return {expression_name(key): value for key, value in context.items()}


def validate_when_expression(
    expression: object,
    available_names: Optional[Set[str]] = None,
) -> Optional[str]:
    """Return a user-facing error, or ``None`` when the expression is safe."""
    text = str(expression).strip()
    if text.lower() in _ALWAYS:
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        detail = exc.msg or "invalid syntax"
        return f"Invalid condition syntax: {detail}. Use == for comparison."

    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODE_TYPES:
            return (
                f"Unsupported condition element: {type(node).__name__}. "
                "Use parameter names, numbers, comparisons, and/or/not."
            )
        if isinstance(node, ast.Name) and available_names is not None:
            if node.id not in available_names:
                choices = ", ".join(sorted(available_names)) or "none"
                return f"Unknown parameter '{node.id}'. Available names: {choices}."
    return None


def evaluate_when_expression(expression: object, context: Mapping[str, Any]) -> bool:
    """Evaluate a validated expression. Invalid expressions always fail closed."""
    text = str(expression).strip()
    if text.lower() in _ALWAYS:
        return True
    namespace = context_namespace(context)
    if validate_when_expression(text, set(namespace)) is not None:
        return False
    try:
        tree = ast.parse(text, mode="eval")
        code = compile(tree, "<when-condition>", "eval")
        return bool(eval(code, {"__builtins__": {}}, namespace))  # noqa: S307
    except Exception:
        return False
