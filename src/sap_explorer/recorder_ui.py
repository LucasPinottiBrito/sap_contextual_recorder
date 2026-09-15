"""Janela Tkinter; o acesso ao SAP e ao banco ocorre em outra thread."""
from __future__ import annotations

import json
from queue import Empty
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
import webbrowser
from pathlib import Path

from .recording_service import RecordingWorker


class RecorderWindow:
    def __init__(self, root: tk.Tk, worker: RecordingWorker):
        self.root, self.worker = root, worker
        self.busy = False
        self.recording = False
        self.closing = False
        self.failed = False
        self.paths: dict[str, str] = {}
        self.observation_id = None
        self.rows: dict[str, dict] = {}
        self.event_count = self.command_count = self.screen_count = 0
        root.title("SAP Explorer · Gravador contextual")
        root.geometry("1180x820")
        root.minsize(900, 680)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        ttk.Label(frame, text="Gravador contextual SAP", font=("Segoe UI", 18, "bold")).grid(sticky="w")
        ttk.Label(frame, text="Selecione a sessão, inicie a gravação e trabalhe no SAP. Ao finalizar, o roteiro será salvo automaticamente.",
                  wraplength=1060).grid(row=1, sticky="w", pady=(4, 10))

        session_frame = ttk.LabelFrame(frame, text="1. Selecione a sessão SAP", padding=8)
        session_frame.grid(row=2, sticky="ew")
        session_frame.columnconfigure(0, weight=1)
        columns = ("connection", "session", "description", "system", "client", "user", "transaction", "screen")
        self.sessions = ttk.Treeview(session_frame, columns=columns, show="headings", height=3, selectmode="browse")
        for column, title, width in zip(columns,
                ("Conexão", "Sessão", "Descrição", "Sistema", "Cliente", "Usuário", "Transação", "Tela"),
                (65, 55, 180, 80, 60, 100, 90, 75)):
            self.sessions.heading(column, text=title)
            self.sessions.column(column, width=width, minwidth=45)
        self.sessions.grid(row=0, column=0, sticky="ew")
        self.sessions.bind("<<TreeviewSelect>>", lambda _: self.update_buttons())
        self.refresh_button = ttk.Button(session_frame, text="Atualizar sessões", command=self.discover)
        self.refresh_button.grid(row=0, column=1, padx=(10, 0))

        controls = ttk.Frame(frame)
        controls.grid(row=3, sticky="ew", pady=10)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Nome da gravação").grid(row=0, column=0, padx=(0, 8))
        self.name = tk.StringVar(value="Meu processo SAP")
        self.name_entry = ttk.Entry(controls, textvariable=self.name)
        self.name_entry.grid(row=0, column=1, sticky="ew")
        self.start_button = ttk.Button(controls, text="Iniciar gravação", command=self.start)
        self.start_button.grid(row=0, column=2, padx=8)
        self.stop_button = ttk.Button(controls, text="Finalizar e gerar arquivos", command=self.stop)
        self.stop_button.grid(row=0, column=3)
        self.status = tk.StringVar(value="Buscando sessões SAP…")
        ttk.Label(frame, textvariable=self.status, wraplength=1060).grid(row=4, sticky="ew", pady=(0, 8))

        panes = ttk.Panedwindow(frame, orient=tk.HORIZONTAL)
        panes.grid(row=5, sticky="nsew")
        live = ttk.LabelFrame(panes, text="2. Ocorrências e ações", padding=6)
        live.columnconfigure(0, weight=1)
        live.rowconfigure(1, weight=1)
        self.counts = tk.StringVar(value="0 telas · 0 eventos · 0 comandos")
        ttk.Label(live, textvariable=self.counts).grid(row=0, sticky="w", pady=(0, 6))
        self.timeline = ttk.Treeview(live, columns=("time", "kind", "summary"), show="headings", selectmode="browse")
        for col, title, width in (("time", "Hora", 95), ("kind", "Ocorrência", 105), ("summary", "Descrição", 360)):
            self.timeline.heading(col, text=title)
            self.timeline.column(col, width=width, minwidth=60)
        self.timeline.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(live, orient="vertical", command=self.timeline.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.timeline.configure(yscrollcommand=scrollbar.set)
        self.timeline.bind("<<TreeviewSelect>>", self.show_detail)
        detail_frame = ttk.LabelFrame(panes, text="Tela atual e detalhes da ocorrência", padding=6)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        self.screen = tk.StringVar(value="Aguardando gravação.")
        ttk.Label(detail_frame, textvariable=self.screen, wraplength=350, justify="left").grid(sticky="ew", pady=(0, 8))
        self.details = ScrolledText(detail_frame, width=40, height=12, wrap="word", state="disabled")
        self.details.grid(row=1, sticky="nsew")
        panes.add(live, weight=3)
        panes.add(detail_frame, weight=2)

        notes = ttk.LabelFrame(frame, text="Anotar etapa e resultado esperado na tela exibida", padding=8)
        notes.grid(row=6, sticky="ew", pady=10)
        notes.columnconfigure(1, weight=1)
        self.label, self.expected = tk.StringVar(), tk.StringVar()
        ttk.Label(notes, text="Etapa").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(notes, textvariable=self.label).grid(row=0, column=1, sticky="ew")
        ttk.Label(notes, text="Resultado esperado").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(notes, textvariable=self.expected).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.note_button = ttk.Button(notes, text="Registrar anotação", command=self.annotate)
        self.note_button.grid(row=0, column=2, rowspan=2, padx=(10, 0))

        footer = ttk.Frame(frame)
        footer.grid(row=7, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.output = tk.StringVar(value=f"Destino: {Path(worker.settings['directory']).resolve()}")
        ttk.Label(footer, textvariable=self.output, wraplength=850).grid(sticky="w")
        self.open_button = ttk.Button(footer, text="Abrir roteiro", command=self.open_report)
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        self.json_button = ttk.Button(footer, text="Abrir JSON", command=lambda: self.open_report("json"))
        self.json_button.grid(row=0, column=2, padx=(8, 0))
        self.update_buttons()
        worker.thread.start()
        self.discover()
        root.after(100, self.poll_messages)

    def update_buttons(self):
        available = not self.busy and not self.closing and not self.failed
        self.start_button.configure(state="normal" if available and self.sessions.selection() else "disabled")
        self.refresh_button.configure(state="normal" if available else "disabled")
        self.stop_button.configure(state="normal" if self.recording and not self.worker.stop.is_set() else "disabled")
        self.note_button.configure(state="normal" if self.recording and self.observation_id and not self.worker.stop.is_set() else "disabled")
        self.name_entry.configure(state="normal" if available else "disabled")
        for button in (self.open_button, self.json_button):
            button.configure(state="normal" if self.paths else "disabled")

    def discover(self):
        self.busy = True
        self.sessions.delete(*self.sessions.get_children())
        self.status.set("Buscando sessões SAP…")
        self.worker.commands.put(("discover", None))
        self.update_buttons()

    def start(self):
        selection = self.sessions.selection()
        if not selection or not self.name.get().strip():
            self.status.set("Selecione uma sessão e informe o nome da gravação.")
            return
        self.paths = {}
        self.rows.clear()
        self.timeline.delete(*self.timeline.get_children())
        self.event_count = self.command_count = self.screen_count = 0
        self.observation_id = None
        self.screen.set("Aguardando primeira tela estável.")
        self.set_detail({})
        self.worker.stop.clear()
        self.busy = True
        self.recording = True
        self.status.set("Conectando o gravador à sessão selecionada…")
        self.worker.commands.put(("record", (int(selection[0]), self.name.get().strip())))
        self.update_buttons()

    def stop(self):
        self.worker.stop.set()
        self.status.set("Finalizando: recebendo eventos pendentes e salvando o roteiro…")
        self.update_buttons()

    def annotate(self):
        if not self.label.get().strip() or not self.observation_id:
            self.status.set("Informe a etapa após a primeira captura de tela.")
            return
        self.worker.notes.put((self.label.get().strip(), self.expected.get(), self.observation_id))
        self.label.set("")
        self.expected.set("")

    def append(self, kind, summary, payload):
        stamp = payload.get("timestamp", "") if isinstance(payload, dict) else ""
        row = self.timeline.insert("", "end", values=(stamp[11:23], kind, summary))
        self.rows[row] = payload
        # Only presentation is bounded; SQLite retains the entire timeline.
        children = self.timeline.get_children()
        if len(children) > 1500:
            oldest = children[0]
            self.timeline.delete(oldest)
            self.rows.pop(oldest, None)
        self.timeline.see(row)

    def show_detail(self, _=None):
        selected = self.timeline.selection()
        if selected:
            self.set_detail(self.rows[selected[0]])

    def set_detail(self, payload):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", json.dumps(payload, ensure_ascii=False, indent=2))
        self.details.configure(state="disabled")

    def poll_messages(self):
        for _ in range(150):
            try:
                kind, payload = self.worker.messages.get_nowait()
            except Empty:
                break
            self.handle_message(kind, payload)
        if self.closing and not self.worker.thread.is_alive():
            self.worker.thread.join(timeout=0)
            self.root.destroy()
            return
        self.root.after(100, self.poll_messages)

    def handle_message(self, kind, payload):
        if kind == "sessions":
            for index, session in enumerate(payload):
                self.sessions.insert("", "end", iid=str(index), values=tuple("—" if session.get(key) is None else session[key] for key in (
                    "connection_index", "session_index", "connection_description", "system_name", "client", "user", "transaction", "screen_number")))
            self.status.set(f"{len(payload)} sessão(ões) encontrada(s). Selecione uma para gravar.")
        elif kind == "idle":
            self.busy = False
            self.recording = False
        elif kind == "run":
            self.append("Gravação", f"Execução {payload['id']}", payload)
        elif kind == "capabilities":
            self.capabilities = payload
            self.append("Captura", "Eventos Change disponíveis" if payload["change_events_expected"] else payload["recording_warning"], payload)
        elif kind == "started":
            warning = getattr(self, "capabilities", {}).get("recording_warning")
            self.status.set("Gravando. Use o SAP normalmente. " + (warning or "As ações aparecem quando o SAP entrega os eventos."))
        elif kind == "event":
            self.event_count += 1
            self.command_count += len(payload.get("commands", []))
            summary = payload.get("component_id") or ""
            for command in payload.get("commands", []):
                summary += f" | {command['member_name']} {json.dumps(command['parameters'], ensure_ascii=False)}"
            if not summary:
                summary = json.dumps(payload.get("details", {}), ensure_ascii=False)
            self.append(payload["event_type"], summary[:600], payload)
        elif kind == "screen":
            state = payload["state"]
            self.observation_id = payload["id"]
            self.screen_count += 1
            title = f"{state.get('transaction')} · {state.get('program')} · {state.get('screen_number')}\n{state.get('active_window_id')} · {state.get('active_window_text')}"
            status = state.get("status_bar") or {}
            title += f"\nMensagem [{status.get('message_type') or '—'}]: {status.get('text') or '—'}"
            self.screen.set(title[:900])
            popup = state.get("active_window_type") in {"GuiModalWindow", "GuiMessageWindow"}
            self.append("Popup" if popup else "Tela", title.replace("\n", " | ")[:600], payload)
        elif kind == "transition":
            self.append("Transição", f"{payload['actions']} ação(ões) · {payload['status']} · {payload['reason']}", payload)
        elif kind == "note":
            self.append("Anotação", payload, {"message": payload})
        elif kind == "files":
            self.paths = payload
            self.output.set(f"JSON: {payload['json']}\nRoteiro: {payload['markdown']}")
            self.status.set("Arquivos gerados. Revise as mensagens esperadas e as lacunas indicadas no roteiro.")
        elif kind in {"error", "fatal"}:
            self.failed = kind == "fatal" or self.worker.shutdown.is_set()
            if self.failed:
                self.busy = self.recording = False
            self.status.set(payload)
            self.append("Erro", payload, {"error": payload})
            # Keep paths visible and allow recovery after an export/start error.
            if self.closing:
                self.closing = False
        self.counts.set(f"{self.screen_count} telas · {self.event_count} eventos · {self.command_count} comandos")
        self.update_buttons()

    def open_report(self, kind="markdown"):
        if kind in self.paths:
            webbrowser.open(Path(self.paths[kind]).as_uri())

    def close(self):
        self.closing = True
        self.worker.close()
        self.status.set("Encerrando com segurança e gerando os arquivos. Aguarde…")
        self.update_buttons()


def launch_recorder(**settings) -> int:
    root = tk.Tk()
    RecorderWindow(root, RecordingWorker(**settings))
    root.mainloop()
    return 0
