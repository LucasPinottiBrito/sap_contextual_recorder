"""Política configurável aplicada também às cópias brutas dos eventos."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .object_tree import SapTextContext, default_text_sanitizer


@dataclass(frozen=True)
class SanitizationPolicy:
    field_patterns: tuple[str, ...] = ()
    text_patterns: tuple[str, ...] = ()
    redact_all_text: bool = False

    @classmethod
    def from_file(cls, path: str | Path) -> SanitizationPolicy:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) - {"field_patterns", "text_patterns", "redact_all_text"}:
            raise ValueError("Política de sanitização inválida.")
        for key in ("field_patterns", "text_patterns"):
            if not isinstance(data.get(key, []), list) or any(not isinstance(v, str) for v in data.get(key, [])):
                raise ValueError(f"{key} deve ser uma lista de expressões regulares.")
            for pattern in data.get(key, []):
                re.compile(pattern, re.IGNORECASE)
        if not isinstance(data.get("redact_all_text", False), bool):
            raise ValueError("redact_all_text deve ser booleano.")
        return cls(tuple(data.get("field_patterns", [])), tuple(data.get("text_patterns", [])),
                   data.get("redact_all_text", False))

    def __call__(self, value: str, context: SapTextContext) -> str | None:
        fields = " ".join(filter(None, (context.node_id, context.node_type, context.node_name)))
        if self.redact_all_text or any(re.search(p, fields, re.IGNORECASE) for p in self.field_patterns):
            return "[REDACTED]" if value else None
        for pattern in self.text_patterns:
            value = re.sub(pattern, "[REDACTED]", value, flags=re.IGNORECASE)
        return default_text_sanitizer(value, context)
