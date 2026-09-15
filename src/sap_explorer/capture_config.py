"""Limites explícitos para a captura, serializáveis por execução."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CaptureOptions:
    max_depth: int = 24
    max_nodes: int = 2000
    max_grid_rows: int = 20
    max_grid_columns: int = 20
    max_entries: int = 100
    max_control_reads: int = 2000
    enrichment_seconds: float = 0.5
    control_scopes: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("max_depth", "max_nodes", "max_grid_rows", "max_grid_columns",
                     "max_entries", "max_control_reads"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} deve ser inteiro positivo.")
        if not math.isfinite(self.enrichment_seconds) or self.enrichment_seconds <= 0:
            raise ValueError("enrichment_seconds deve ser positivo e finito.")
        if any(not s.startswith("wnd[") for s in self.control_scopes):
            raise ValueError("Escopos devem começar com wnd[n], sem prefixo de sessão.")
