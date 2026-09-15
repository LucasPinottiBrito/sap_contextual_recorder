"""Preserva valores JSON e descreve perdas sem armazenar conteúdo redigido."""
from typing import Any
import math

from .object_tree import SapTextContext, TextSanitizer


def capture_value(value: Any, sanitizer: TextSanitizer, context: SapTextContext,
                  *, depth: int = 0, path: str = "$") -> tuple[Any, list[dict[str, str]]]:
    if depth >= 12:
        return None, [{"path": path, "status": "truncated"}]
    if value is None:
        return None, []
    if isinstance(value, (list, tuple)):
        result, issues = [], []
        for i, child in enumerate(value):
            safe, losses = capture_value(child, sanitizer, context, depth=depth + 1, path=f"{path}[{i}]")
            result.append(safe)
            issues.extend(losses)
        return result, issues
    if isinstance(value, dict):
        result, issues = {}, []
        for key, child in value.items():
            if not isinstance(key, str):
                return None, [{"path": path, "status": "unsupported_type"}]
            child_context = context
            if context.property_name == "CaseInput":
                child_context = SapTextContext("CaseInput", f"{context.node_id}.{key}", None,
                                               f"{context.node_name}.{key}")
            safe, losses = capture_value(child, sanitizer, child_context, depth=depth + 1, path=f"{path}.{key}")
            result[key] = safe
            issues.extend(losses)
        return result, issues
    if isinstance(value, float) and not math.isfinite(value):
        return None, [{"path": path, "status": "unsupported_type"}]
    if not isinstance(value, (str, bool, int, float)):
        return None, [{"path": path, "status": "unsupported_type"}]
    try:
        safe = sanitizer(value if isinstance(value, str) else str(value), context)
    except Exception:
        return None, [{"path": path, "status": "unavailable"}]
    if safe == str(value):
        return value, []
    status = ("omitted" if safe is None else
              "redacted" if "[REDACTED]" in safe else "transformed")
    return safe, [{"path": path, "status": status}]
