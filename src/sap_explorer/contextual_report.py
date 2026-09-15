"""Roteiro contextual por execução, derivado do diário persistido, sem COM."""
from __future__ import annotations

import json
import keyword
from pathlib import Path
from typing import Any

from .fingerprint import normalize_component_id
from .models import new_id, utc_now
from .repository import SapRepository, write_json

SAP_API = "https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/770.06/en-US/sap_gui_scripting_api.pdf"


def _rows(repository: SapRepository, query: str, run_id: str) -> list[dict[str, Any]]:
    return [{key.removesuffix("_json"): json.loads(value) if key.endswith("_json") and value is not None else value
             for key, value in dict(row).items()}
            for row in repository.connection.execute(query, (run_id,))]


def _nodes(state: dict[str, Any]):
    pending = list(reversed((state.get("ui_tree") or {}).get("roots", [])))
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.get("children", [])))


def screen_context(observation: dict[str, Any] | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    state = observation["state"]
    return {"observation_id": observation["id"],
            **{key: state.get(key) for key in ("transaction", "program", "screen_number",
               "active_window_id", "active_window_type", "active_window_text")},
            "structural_hash": observation["structural_hash"],
            "is_popup": state.get("active_window_type") in {"GuiModalWindow", "GuiMessageWindow"},
            "basis": "last_verified_observation"}


def _literal(value: Any) -> bool:
    if value is None or type(value) in {bool, int}:
        return True
    if isinstance(value, float):
        import math
        return math.isfinite(value)
    if isinstance(value, str):
        return not any(marker in value for marker in ("[REDACTED]", "[TRUNCATED]", "[UNAVAILABLE]"))
    if isinstance(value, list):
        return all(_literal(item) for item in value)
    return False


def command_preview(event: dict[str, Any]) -> dict[str, Any]:
    """Traduz apenas M/SP íntegros; linhas de um Change formam uma cadeia SAP."""
    commands = event.get("commands", [])
    component = normalize_component_id(event.get("component_id"))
    problem = None
    if event.get("event_type") != "Change" or not commands or not component:
        problem = "Evento sem comandos reproduzíveis."
    expression = f"session.findById({component!r})"
    for index, command in enumerate(commands):
        member, kind = command.get("member_name"), command.get("command_type")
        params, quality = command.get("parameters", []), command.get("quality", {})
        if (not member or not member.isascii() or not member.isidentifier()
                or member.startswith("_") or keyword.iskeyword(member)):
            problem = "Membro desconhecido ou inválido."
        if quality.get("parameters_preserved") is not True or not _literal(params):
            problem = "Parâmetros redigidos, perdidos ou com fidelidade não comprovada."
        if kind == "M":
            expression += f".{member}({', '.join(repr(p) for p in params)})"
        elif kind == "SP" and len(params) == 1 and index == len(commands) - 1:
            expression += f".{member} = {params[0]!r}"
        else:
            problem = "Tipo/encadeamento de comando não suportado; consultar CommandArray."
    return {"python": None if problem else expression, "review_required": True, "reason": problem}


def expected_result(observation: dict[str, Any] | None) -> dict[str, Any]:
    if observation is None:
        return {"basis": "unavailable", "review_required": True, "status_bar": None, "popup": None}
    state = observation["state"]
    popup = None
    if state.get("active_window_type") in {"GuiModalWindow", "GuiMessageWindow"}:
        popup = {"window_id": state.get("active_window_id"), "title": state.get("active_window_text"),
                 "texts": [{"id": n.get("id"), "type": n.get("type"), "text": n.get("text")}
                           for n in _nodes(state) if n.get("text")]}
    return {"basis": "observed_after_batch", "review_required": True,
            "functional_success": "unconfirmed", "screen": screen_context(observation),
            "status_bar": state.get("status_bar"), "popup": popup,
            "matching_guidance": "Revisar tipo, classe e número da mensagem; parametrizar valores variáveis. "
                                 "Uma mensagem anterior pode permanecer na barra de status."}


def build_contextual_report(repository: SapRepository, run_id: str) -> dict[str, Any]:
    runs = _rows(repository, "SELECT * FROM runs WHERE id=?", run_id)
    if not runs:
        raise ValueError("Gravação não encontrada.")
    observations = _rows(repository, "SELECT * FROM observations WHERE run_id=? ORDER BY rowid", run_id)
    events = _rows(repository, "SELECT * FROM events WHERE run_id=? ORDER BY rowid", run_id)
    history = _rows(repository, "SELECT * FROM transition_observations WHERE run_id=? ORDER BY rowid", run_id)
    batches = _rows(repository, "SELECT * FROM request_batches WHERE run_id=? ORDER BY rowid", run_id)
    timeline = _rows(repository, "SELECT * FROM capture_timeline WHERE run_id=? ORDER BY sequence", run_id)
    metadata = _rows(repository, "SELECT * FROM run_metadata WHERE run_id=?", run_id)
    observation_metadata = _rows(repository, """SELECT m.* FROM observation_metadata m
        JOIN observations o ON o.id=m.observation_id WHERE o.run_id=? ORDER BY o.rowid""", run_id)
    marks = _rows(repository, "SELECT * FROM case_marks WHERE run_id=? ORDER BY rowid", run_id)
    cases = _rows(repository, "SELECT c.* FROM cases c JOIN case_runs cr ON cr.case_id=c.id WHERE cr.run_id=?", run_id)
    by_observation = {item["id"]: item for item in observations}
    by_event = {item["id"]: item for item in events}
    by_batch = {item["transition_observation"]: item for item in batches}
    steps, assigned = [], set()
    for item in history:
        before = by_observation.get(item["before_observation"])
        after = by_observation.get(item["after_observation"])
        actions = []
        for event_id in item["action_ids"]:
            record = by_event[event_id]
            payload = record["event"]
            component_id = normalize_component_id(payload.get("component_id"))
            node = next((n for n in _nodes(before["state"]) if component_id and
                         normalize_component_id(n.get("id")) == component_id), None) if before else None
            actions.append({"event_id": event_id, "event": payload, "script_preview": command_preview(payload),
                            "locator": {"id": component_id, "type": payload.get("component_type"),
                                        "name": payload.get("component_name")},
                            "control_context": node, "context_basis": "last_verified_observation"})
            assigned.add(event_id)
        context = screen_context(before)
        title = "Tela de origem não observada" if context is None else (
            f"{'Popup' if context['is_popup'] else 'Tela'}: {context['active_window_text'] or context['active_window_id']} "
            f"({context['transaction']} / {context['screen_number']})")
        steps.append({"number": len(steps) + 1, "transition_id": item["id"], "title": title,
                      "before": context, "after": screen_context(after), "actions": actions,
                      "capture_status": item["status"], "capture_reason": item["reason"],
                      "batch": by_batch.get(item["id"]), "expected_result": expected_result(after),
                      "annotations": [m for m in marks if m.get("observation_id") == item["before_observation"]],
                      "exact_before_known": False})
    unassigned = [e for e in events if e["is_action"] and e["id"] not in assigned]
    warnings = []
    if not (runs[0].get("capabilities") or {}).get("change_events_expected"):
        warnings.append("Eventos Change indisponíveis ou não confirmados nesta execução.")
    if not any(e["event_type"] == "Change" for e in events):
        warnings.append("Nenhum comando Change foi recebido.")
    if not observations:
        warnings.append("Nenhuma tela estável foi capturada.")
    if any(s["capture_status"] != "complete" for s in steps):
        warnings.append("Há etapas com tela anterior ou posterior desconhecida.")
    if unassigned:
        warnings.append("Há ações sem transição correlacionada; consultar unassigned_actions.")
    if any(e["event_type"] == "CaptureError" for e in events):
        warnings.append("O SAP entregou eventos que não puderam ser normalizados.")
    for observation in observations:
        tree = observation["state"].get("ui_tree") or {}
        if not tree or tree.get("warnings") or any(n.get("truncated") or
                any(value != "captured" for value in n.get("quality", {}).values()) for n in _nodes(observation["state"])):
            warnings.append(f"Revisar limites/qualidade dos controles na observação {observation['id']}.")
    return {"schema": "sap_contextual_recording", "schema_version": 1, "generated_at": utc_now(),
            "run": runs[0], "case": cases[0] if cases else None, "capture_metadata": metadata,
            "summary": {"steps": len(steps), "observations": len(observations), "events": len(events),
                        "commands": sum(len(e["event"].get("commands", [])) for e in events)},
            "general_steps": [{"number": s["number"], "title": s["title"],
                               "action_count": len(s["actions"]), "capture_status": s["capture_status"]} for s in steps],
            "steps": steps, "observations": observations, "observation_metadata": observation_metadata,
            "events": events, "timeline": timeline,
            "annotations": marks, "unassigned_actions": unassigned, "warnings": warnings,
            "generation_guidance": [
                "Usar timeline.sequence como ordem de captura; timestamps não substituem essa ordem.",
                "Preservar a cadeia de comandos de cada Change e os parâmetros tipados/raw_commands.",
                "Validar transação, programa, tela e janela antes de agir; esperar Busy=False com timeout.",
                "Revisar valores pré-preenchidos e estados de controles nas observações, mesmo sem Change.",
                "Resolver linhas de tabelas por valores/chaves de negócio quando disponíveis.",
                "Observação anterior não equivale ao instante exato de cada ação do lote.",
                "Mensagens observadas são candidatas a resultado esperado, nunca confirmação automática de sucesso.",
                "Preencher lacunas/redações e validar popups; não executar previews sem revisão.",
                "Captura depende dos eventos do SAP; diálogos nativos e scripts externos podem deixar lacunas."],
            "references": [SAP_API, "https://docs.python.org/3/library/tkinter.html#threading-model"]}


def render_contextual_markdown(report: dict[str, Any]) -> str:
    def literal(value: Any) -> str:
        # Fence size follows the data, so captured SAP text cannot close it.
        text = json.dumps(value, ensure_ascii=False, indent=2)
        import re
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=3))
        return f"{fence}json\n{text}\n{fence}"

    lines = ["# Gravação contextual SAP", "", f"Execução: {report['run']['id']}", "",
             "Mensagens observadas precisam de revisão para se tornarem resultados esperados do script.", "",
             "## Resumo", "", literal(report["summary"]), "", "## Passos gerais", ""]
    for step in report["steps"]:
        lines.append(f"{step['number']}. {step['title'].replace(chr(10), ' ')} — {len(step['actions'])} evento(s) de ação.")
    lines.extend(["", "## Etapas detalhadas", ""])
    for step in report["steps"]:
        lines.extend([f"### Etapa {step['number']}", "", literal({k: v for k, v in step.items() if k != "actions"}), "",
                      "#### Ações na ordem recebida", "", literal(step["actions"]), ""])
    lines.extend(["## Anotações do operador", "", literal(report["annotations"]), "",
                  "## Lacunas e qualidade", "", literal(report["warnings"]), "",
                  "## Orientações para gerar o script", ""])
    lines.extend(f"- {text}" for text in report["generation_guidance"])
    lines.extend(["", "O JSON acompanhante contém todas as observações, árvores, eventos e a ordem global.", "",
                  f"Referência: [SAP GUI Scripting API]({SAP_API})", ""])
    return "\n".join(lines)


def write_contextual_report(repository: SapRepository, run_id: str, directory: str | Path) -> dict[str, str]:
    report = build_contextual_report(repository, run_id)
    folder = Path(directory).resolve()
    json_path, md_path = folder / f"recording_{run_id}.json", folder / f"recording_{run_id}.md"
    write_json(json_path, report)
    temporary = md_path.with_name(md_path.name + "." + new_id() + ".tmp")
    try:
        temporary.write_text(render_contextual_markdown(report), encoding="utf-8")
        temporary.replace(md_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"json": str(json_path), "markdown": str(md_path)}
