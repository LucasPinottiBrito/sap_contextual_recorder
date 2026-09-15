"""Frequências observadas: uma ação pode possuir vários resultados."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .repository import SapRepository, write_json


def analyze(repository: SapRepository, *, rare_threshold: float = 0.05) -> dict[str, Any]:
    data = repository.export()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    from .repository import encode
    for transition in data["transitions"]:
        groups[(transition["from_screen"], encode(transition["action"]))].append(transition)
    paths = []
    for (state, _), transitions in groups.items():
        total = sum(t["count"] for t in transitions)
        paths.append({"from_screen": state, "actions": transitions[0]["action"], "count": total,
                      "outcomes": [{"to_screen": t["to_screen"], "count": t["count"],
                                    "frequency": t["count"] / total,
                                    "rare": t["count"] / total < rare_threshold}
                                   for t in sorted(transitions, key=lambda t: -t["count"])]})
    outgoing = {path["from_screen"] for path in paths}
    popups = [s["structural_hash"] for s in data["states"]
              if s["definition"]["screen"].get("has_additional_windows")
              or s["definition"]["screen"].get("active_window_type") == "GuiModalWindow"]
    errors = [o["id"] for o in data["observations"]
              if (o["state"].get("status_bar") or {}).get("message_type") in {"E", "A"}]
    partial = [o["id"] for o in data["observations"] if (o["state"].get("ui_tree") or {}).get("warnings")]
    command_quality = []
    for row in data["actions"]:
        for index, command in enumerate(row["event"].get("commands", [])):
            quality = command.get("quality", {})
            if quality.get("parameters_preserved") is not True:
                command_quality.append({"event_id": row["id"], "command_index": index,
                                        "quality": quality or {"status": "legacy_unknown"}})
    return {"command_quality_issues": command_quality,
            "cases": data.get("cases", []), "request_batches": data.get("request_batches", []),
            "stats": data["stats"], "states": data["states"],
            "paths": sorted(paths, key=lambda p: -p["count"]),
            "rare_threshold": rare_threshold, "popups": popups,
            "states_without_exit": [s["structural_hash"] for s in data["states"] if s["structural_hash"] not in outgoing],
            "multiple_outcomes": [p for p in paths if len(p["outcomes"]) > 1],
            "rare_paths": [{"from_screen": p["from_screen"], "actions": p["actions"],
                            "total": p["count"], **o} for p in paths for o in p["outcomes"] if o["rare"]],
            "error_observations": errors, "observations_with_warnings": partial,
            "unattributed_transitions": sum(t["count"] for t in data["transitions"] if not t["action"])}


def write_report(repository: SapRepository, directory: str | Path) -> dict[str, Any]:
    report = analyze(repository)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "exploration_report.json", report)
    lines = ["# Relatório da exploração SAP", "", "## Contagens", ""]
    lines.extend(f"- {name}: {count}" for name, count in report["stats"].items())
    lines += ["", "## Estados descobertos", ""]
    for state in report["states"]:
        screen = state["definition"]["screen"]
        lines.append(f"- `{state['structural_hash']}`: {screen['transaction']} / {screen['program']} / "
                     f"{screen['screen_number']} / {screen['active_window_id']} — {state['count']} observações.")
    lines += ["", "## Transições e caminhos mais comuns", ""]
    for path in report["paths"]:
        label = "; ".join(f"{a['component_id']}: " + ", ".join(str(c['member_name']) for c in a['commands'])
                          for a in path["actions"]) or "ação não informada pelo SAP"
        lines += [f"- Estado `{path['from_screen']}`; ação: {label} ({path['count']} ocorrências)."]
        lines += [f"  - → `{o['to_screen']}`: {o['count']}/{path['count']} ({o['frequency']:.1%})." for o in path["outcomes"]]
    lines += ["", "## Caminhos raros (<5% dos resultados da mesma ação)", ""]
    lines += [f"- `{p['from_screen']}` → `{p['to_screen']}`: {p['count']}/{p['total']} ({p['frequency']:.1%})."
              for p in report["rare_paths"]] or ["Nenhum observado."]
    for heading, key in (("Estados sem saída observada", "states_without_exit"), ("Popups detectados", "popups")):
        lines += ["", f"## {heading}", ""] + ([f"- `{s}`" for s in report[key]] or ["Nenhum observado."])
    lines += ["", "## Ações com múltiplos resultados", ""]
    lines += [f"- `{p['from_screen']}`: {len(p['outcomes'])} resultados para a sequência de ações registrada."
              for p in report["multiple_outcomes"]] or ["Nenhuma observada."]
    lines += ["", "## Pontos de atenção", "",
              f"- Observações com mensagem SAP E/A: {len(report['error_observations'])}.",
              f"- Observações com avisos de leitura: {len(report['observations_with_warnings'])}.",
              f"- Transições sem ação informada: {report['unattributed_transitions']}.",
              f"- Comandos com perda ou qualidade desconhecida: {len(report['command_quality_issues'])}.",
              "- Estados de controles, amostras e omissões estão em control_state e quality no export.",
              "- request_batches preserva intervalos observados; não afirma o instante anterior a cada comando.",
              "- cases contém resultados informados pelo operador; CTRL+C não confirma sucesso de negócio.",
              "- Transições incompletas permanecem no histórico, fora das frequências do mapa.",
              "- As frequências descrevem a amostra observada; não garantem o resultado de uma execução futura.",
              "- Uma observação anterior é a última tela verificada; não é uma leitura por tecla ou por linha de script."]
    (directory / "exploration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
