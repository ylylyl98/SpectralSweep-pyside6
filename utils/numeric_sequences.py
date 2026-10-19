"""Numeric sequence syntax shared by the Dual Gate editors (no evaluation)."""
from __future__ import annotations

import math
import re

import numpy as np


MAX_SEQUENCE_VALUES = 100_000
NUMERIC_SEQUENCE_HELP = (
    "Single value: 4\n"
    "List: -4, 0, 4 or [-4, 0, 4]\n"
    "Even spacing: linspace(-4, 4, 5) or (-4, 4, 5)\n"
    "  (start, stop, n): n is an integer point count of at least 2; both ends included.\n"
    "Step range: -4:2:4 (start:step:stop)\n"
    "  Includes the stop when the step lands on it.\n"
    "Lists and sequences can be mixed, for example: -4:2:0, 3, 5."
)


def _split_items(expression: str) -> list[str]:
    items, start, depth = [], 0, 0
    for index, char in enumerate(expression):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Sequence parentheses must be paired.")
        elif char in ",;" and depth == 0:
            items.append(expression[start:index].strip())
            start = index + 1
    if depth:
        raise ValueError("Sequence parentheses must be paired.")
    items.append(expression[start:].strip())
    if any(not item for item in items):
        raise ValueError("Remove the empty item between separators.")
    return items


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"'{text}' is not a number.") from exc
    if not math.isfinite(value):
        raise ValueError(f"'{text}' must be a finite number.")
    return value


def parse_numeric_sequence(
    text: str, *, max_values: int = MAX_SEQUENCE_VALUES,
) -> list[float]:
    """Expand scalars, lists, linspace(start, stop, n), and start:step:stop.

    Parenthesized triples are linspace shorthand; bare triples are lists.
    Enforce the total size before allocating any generated sequence.
    """
    expression = str(text).strip()
    if not expression:
        raise ValueError("Enter a number, list, or sequence.")
    if expression.startswith("[") and expression.endswith("]"):
        expression = expression[1:-1].strip()
    if "[" in expression or "]" in expression:
        raise ValueError("Use one matching pair of brackets around the full list.")
    if not expression:
        raise ValueError("The value list is empty.")

    values: list[float] = []

    def check_count(count: int | float) -> None:
        if not math.isfinite(count) or len(values) + count > max_values:
            raise ValueError(f"At most {max_values} values are allowed.")

    for token in _split_items(expression):
        linspace = re.fullmatch(r"(?:linspace\s*)?\((.*)\)", token, re.IGNORECASE)
        if linspace:
            parts = _split_items(linspace.group(1))
            if len(parts) != 3:
                raise ValueError("Use linspace(start, stop, n) or (start, stop, n).")
            start, stop, count = (_number(part) for part in parts)
            if not count.is_integer() or count < 2:
                raise ValueError("Linspace point count n must be an integer of at least 2.")
            check_count(count)
            with np.errstate(over="ignore", invalid="ignore"):
                generated = np.linspace(start, stop, int(count)).tolist()
        elif ":" in token:
            parts = token.split(":")
            if len(parts) != 3 or any(not part.strip() for part in parts):
                raise ValueError(f"'{token}' must use the range form start:step:stop.")
            start, step, stop = (_number(part.strip()) for part in parts)
            if abs(step) < 1e-15:
                raise ValueError(f"'{token}' has a zero step.")
            if start != stop and ((stop > start) != (step > 0)):
                raise ValueError(
                    f"'{token}' steps away from its stop value; reverse the step sign."
                )
            span = (stop - start) / step
            check_count(span)
            count = 1 if start == stop else math.floor(span + 1e-12) + 1
            check_count(count)
            generated = [start + index * step for index in range(count)]
        else:
            check_count(1)
            generated = [_number(token)]
        if not all(math.isfinite(value) for value in generated):
            raise ValueError("The generated sequence must contain only finite numbers.")
        values.extend(generated)
    return values
