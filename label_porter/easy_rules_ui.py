"""Inline, three-step intent picker. No editable JSON or connection canvas."""
from __future__ import annotations

import copy
import queue
import threading
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, scrolledtext
from . import widgets as ttk

from .easy_rules import analyze_inputs, friendly, make_rule, select_key, key_fields, key_stats, connection_option
from .pair_preview import display
from .pair_preview_ui import PairPreview
from .ui import ScrollPage, card
from .ui_lifecycle import UiEventQueue, remove_traces, release_variables


class EasyRules(ttk.Frame):
    TITLES = ("데이터 선택", "반영할 내용 선택", "변경 전·후 확인")

    def __init__(self, parent, on_change, on_finish, on_exit, source="", results=()):
        super().__init__(parent)
        self.on_change, self.on_finish, self.on_exit = on_change, on_finish, on_exit
        self.stage, self.busy, self.closed = 0, False, False
        self.revision, self.cancel_event = 0, threading.Event()
        self.queue = UiEventQueue()
        self.inputs = self.data = self.preview = self.rule_config = None
        self.options, self.selected = [], set()
        self.source = tk.StringVar(value=source)
        self.results = list(results)
        self.status = tk.StringVar(value="원본과 플랫폼 / 보완 데이터를 선택하세요. 필드명이 달라도 연결할 수 있습니다.")
        self.name = tk.StringVar(value="데이터로 만든 규칙")
        self.key = tk.StringVar()
        self.query = tk.StringVar()
        self.show_all = tk.BooleanVar(value=False)
        self.check_times = tk.BooleanVar(value=True)
        self.skip_empty = tk.BooleanVar(value=True)
        self.summary = tk.StringVar()
        self.connection = tk.StringVar()
        self._build()
        self.traces = [(variable, variable.trace_add("write", callback)) for variable, callback in (
            (self.query, lambda *_: self._render_fields()),
            (self.show_all, lambda *_: self._render_fields()),
            (self.source, lambda *_: self._input_changed()),
        )]
        self.bind("<Destroy>", self._destroyed, add="+")
        self.last_state = None
        self.poll_job = self.after(80, self._poll)
        self._show(0)

    def _build(self):
        self.steps = ttk.Notebook(self, style="Flow.TNotebook")
        self.steps.pack(fill="both", expand=True)
        choose = ScrollPage(self.steps)
        options, preview = ScrollPage(self.steps), ttk.Frame(self.steps)
        for frame, title in zip((choose, options, preview), self.TITLES):
            self.steps.add(frame, text=title)
        self.preview_host = preview
        options = options.body
        group = card(choose.body, "1. 원본 라벨 폴더", "개별 JSON과 통합 JSON을 읽습니다. JSONL / NDJSON도 한 형식씩 사용할 수 있습니다.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x")
        ttk.Button(row, text="폴더 선택", command=self._choose_source).pack(side="right", padx=(8, 0))
        ttk.Entry(row, textvariable=self.source).pack(fill="x", expand=True)
        group = card(choose.body, "2. 플랫폼 / 보완 데이터", "수정 결과 파일을 선택하세요. 연결 기준을 추천하며, 원본과 필드명이 달라도 직접 선택해 연결할 수 있습니다.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x")
        actions = ttk.Frame(row, style="Card.TFrame"); actions.pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="파일 추가", command=self._choose_results).pack(fill="x")
        ttk.Button(actions, text="선택 제거", command=self._remove_results).pack(fill="x", pady=6)
        ttk.Button(actions, text="데이터 새로 읽기", command=self.refresh).pack(fill="x")
        self.result_list = tk.Listbox(row, height=4, selectmode="extended", relief="flat")
        self.result_list.pack(fill="x", expand=True)
        self._render_results()
        card(choose.body, "선택한 항목만 반영합니다", "연결 기준과 수정 항목을 추천합니다. 목록은 보완본 전체로 교체하고, 숫자와 전사는 그대로 가져옵니다. 선택하지 않은 항목은 원본을 유지합니다.")

        ttk.Label(options, textvariable=self.summary, style="Heading.TLabel").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(options); row.pack(fill="x")
        ttk.Label(row, text="추천 연결 기준").pack(side="left", padx=(0, 10))
        ttk.Button(row, text="직접 선택", command=self._toggle_keys, style="Compact.TButton").pack(side="right", padx=(8, 0))
        self.key_box = ttk.Combobox(row, textvariable=self.key, state="readonly")
        self.key_box.pack(fill="x", expand=True)
        self.key_box.bind("<<ComboboxSelected>>", lambda _: self._select_key())
        self.key_manual = ttk.Frame(options)
        self.source_key_box, self.result_key_box = self._two_choices(self.key_manual, "원본의 연결 기준", "플랫폼 / 보완의 연결 기준")
        for box in (self.source_key_box, self.result_key_box):
            box.bind("<<ComboboxSelected>>", lambda _: self._key_edited())
        ttk.Label(self.key_manual, text="목록에는 필드별 예시가 표시됩니다. 실제로 같은 값인 레코드의 수는 ‘연결 확인’으로 확인하세요.",
                  style="Muted.TLabel", wraplength=750).pack(fill="x", pady=(4, 0))
        ttk.Button(self.key_manual, text="연결 확인", command=self._manual_key, style="Compact.TButton").pack(anchor="e", pady=(5, 0))
        self.connection_label = ttk.Label(options, textvariable=self.connection, style="Muted.TLabel", wraplength=760)
        self.connection_label.pack(fill="x", pady=(6, 10))
        row = ttk.Frame(options); row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="규칙 이름").pack(side="left", padx=(0, 10))
        ttk.Entry(row, textvariable=self.name).pack(fill="x", expand=True)
        row = ttk.Frame(options); row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="항목 찾기").pack(side="left", padx=(0, 10))
        ttk.Entry(row, textvariable=self.query).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(row, text="다른 항목도 보기", variable=self.show_all).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="추천 선택", command=self._recommend, style="Compact.TButton").pack(side="left", padx=6)
        ttk.Button(row, text="선택 해제", command=self._clear, style="Compact.TButton").pack(side="left")
        table = ttk.Frame(options); table.pack(fill="both", expand=True)
        self.fields = ttk.Treeview(table, columns=("check", "name", "from", "difference", "policy"), show="headings", height=6)
        for col, title, width in (("check", "반영", 45), ("name", "원본 대상", 160), ("from", "가져올 보완 항목", 250),
                                  ("difference", "표본 변경", 85), ("policy", "반영 방식", 150)):
            self.fields.heading(col, text=title)
            self.fields.column(col, width=width, minwidth=45, stretch=col != "check")
        scroll = ttk.Scrollbar(table, command=self.fields.yview)
        scroll.pack(side="right", fill="y")
        self.fields.configure(yscrollcommand=scroll.set)
        self.fields.pack(fill="both", expand=True)
        self.fields.bind("<Button-1>", self._click)
        self.fields.bind("<space>", self._toggle)
        self.fields.bind("<<TreeviewSelect>>", self._detail)
        self.manual_actions = ttk.Frame(options); self.manual_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(self.manual_actions, text="다른 필드 연결", command=self._open_connection, style="Compact.TButton").pack(side="left")
        ttk.Button(self.manual_actions, text="선택한 연결 수정", command=lambda: self._open_connection(edit=True), style="Compact.TButton").pack(side="left", padx=6)
        self.field_manual = ttk.Frame(options)
        self.target_box, self.incoming_box = self._two_choices(self.field_manual, "수정할 원본 항목", "가져올 플랫폼 / 보완 항목")
        for box in (self.target_box, self.incoming_box):
            box.bind("<<ComboboxSelected>>", lambda _: self._manual_examples())
        self.manual_note = tk.StringVar()
        ttk.Label(self.field_manual, textvariable=self.manual_note, wraplength=750, style="Muted.TLabel").pack(fill="x", pady=5)
        row = ttk.Frame(self.field_manual); row.pack(fill="x")
        ttk.Checkbutton(row, text="보완 값이 비어 있으면 원본 유지", variable=self.skip_empty).pack(side="left")
        ttk.Button(row, text="이 연결 적용", command=self._apply_connection, style="Compact.TButton").pack(side="right")
        ttk.Button(row, text="닫기", command=self.field_manual.pack_forget, style="Compact.TButton").pack(side="right", padx=6)
        self.detail = scrolledtext.ScrolledText(options, height=4, wrap="word", font=("맑은 고딕", 9), state="disabled", relief="flat")
        self.detail.pack(fill="x", pady=(8, 4))
        self.time_check = ttk.Checkbutton(options, text="어절 시간의 기본 오류 확인 · 음수 / 시작 > 종료", variable=self.check_times)
        self.time_check.pack(anchor="w")
        self.policy_note = ttk.Label(options, text="선택한 목록은 보완본 전체로 교체합니다. 빈 값 처리는 각 연결의 설정을 따릅니다. 선택하지 않은 항목은 원본 유지입니다.",
                                    style="Muted.TLabel", wraplength=760)
        self.policy_note.pack(fill="x", pady=(5, 0))
        self.stop = ttk.Button(self, text="분석 중단", command=self.cancel)

    @staticmethod
    def _two_choices(parent, left, right):
        row = ttk.Frame(parent); row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="fields"); row.columnconfigure(1, weight=1, uniform="fields")
        boxes = []
        for column, title in enumerate((left, right)):
            ttk.Label(row, text=title).grid(row=0, column=column, sticky="w", pady=(5, 3))
            box = ttk.Combobox(row, state="readonly", width=20)
            box.grid(row=1, column=column, sticky="ew", padx=(0, 8) if column == 0 else (0, 0))
            boxes.append(box)
        return boxes

    @staticmethod
    def _fill_choices(box, fields):
        box.paths = list(fields)
        box.configure(values=[f"{p}  ·  {str(fields[p].examples[0])[:65].replace(chr(10), ' ')}"
                              if fields[p].examples else p for p in box.paths])
        box.set("")

    @staticmethod
    def _choice(box):
        index = box.current()
        return box.paths[index] if 0 <= index < len(getattr(box, "paths", [])) else None

    @staticmethod
    def _choose_path(box, path):
        if path in box.paths: box.current(box.paths.index(path))

    def _toggle_keys(self):
        if self.key_manual.winfo_manager(): self.key_manual.pack_forget()
        else: self.key_manual.pack(fill="x", before=self.connection_label)

    def _key_edited(self):
        self.cancel()
        self.data = None; self.options = []; self.selected.clear()
        self.key.set(""); self.field_manual.pack_forget()
        self.connection.set("원본과 보완의 연결 기준을 선택한 뒤 ‘연결 확인’을 누르세요. 값이 같은 레코드만 연결합니다.")
        self._render_fields()

    def _manual_key(self):
        a, b = self._choice(self.source_key_box), self._choice(self.result_key_box)
        if not a or not b or self.inputs is None:
            self.status.set("원본과 보완의 연결 기준을 각각 선택하세요."); self.on_change(); return
        self._connect_key(a, b)

    def _open_connection(self, edit=False):
        if self.busy or self.data is None:
            self.status.set("연결 기준을 먼저 확인하세요."); self.on_change(); return
        self._fill_choices(self.target_box, self.inputs.source_fields)
        self._fill_choices(self.incoming_box, self.inputs.result_fields)
        self.skip_empty.set(True)
        if edit and self.fields.selection():
            item = self.options[int(self.fields.selection()[0])]
            self._choose_path(self.target_box, item["path"])
            self._choose_path(self.incoming_box, item.get("from", item["path"]))
            self.skip_empty.set(item.get("skip_empty", False))
        self.field_manual.pack(fill="x", after=self.manual_actions, pady=5)
        self._manual_examples()

    def _manual_examples(self):
        parts = []
        for title, box, fields in (("원본", self.target_box, self.inputs.source_fields),
                                   ("보완", self.incoming_box, self.inputs.result_fields)):
            path = self._choice(box)
            if path:
                info = fields[path]
                parts.append(f"{title} {path} · {info.kind} · 예시 {display(info.examples[0], 160) if info.examples else '없음'}")
        self.manual_note.set("필드별 예시 · 서로 다른 레코드일 수 있습니다. 적용 후 실제 한 쌍을 확인하세요.\n" + "\n".join(parts)
                             if parts else "항목을 고르면 경로와 값 예시를 보여줍니다. 적용 후 실제로 연결된 한 쌍을 확인하세요.")

    def _apply_connection(self):
        if self.busy or self.data is None: return
        try:
            item = connection_option(self.inputs, self.data, self._choice(self.target_box),
                                     self._choice(self.incoming_box), self.skip_empty.get())
            item["selected"] = True
            self.options = [old for old in self.options if old["path"] != item["path"]] + [item]
            self.selected.add(item["path"])
            self.query.set("")
            self.field_manual.pack_forget(); self._render_fields()
            self.fields.selection_set(str(len(self.options) - 1)); self.fields.see(str(len(self.options) - 1))
            self._detail()
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(str(exc)); self.on_change()

    def _show(self, stage):
        self.stage = stage
        self.steps.select(stage)
        self.on_change()

    @property
    def can_next(self):
        if self.busy: return False
        if self.stage == 0: return bool(self.source.get().strip() and self.results)
        if self.stage == 1: return self.data is not None and bool(self.selected)
        return self.preview is not None and self.preview.cache is not None

    @property
    def next_title(self):
        return ("데이터 분석 →", "변경 전·후 확인 →", "규칙 만들고 계속 →")[self.stage]

    def _choose_source(self):
        path = filedialog.askdirectory(parent=self.winfo_toplevel(), title="원본 라벨 폴더")
        if path: self.source.set(path); self.on_change()

    def _choose_results(self):
        paths = filedialog.askopenfilenames(parent=self.winfo_toplevel(), title="보완 데이터 선택",
                                            filetypes=[("JSON 데이터", "*.json *.jsonl *.ndjson")])
        self.results = list(dict.fromkeys([*self.results, *paths]))
        self._input_changed()
        self._render_results()

    def _remove_results(self):
        for index in reversed(self.result_list.curselection()): del self.results[index]
        self._input_changed()
        self._render_results()

    def _render_results(self):
        self.result_list.delete(0, "end")
        for path in self.results: self.result_list.insert("end", path)
        self.on_change()

    def _work(self, function, kind):
        if self.closed: return
        self.busy = True
        self.revision += 1
        revision = self.revision
        cancel = self.cancel_event = threading.Event()
        self.stop.pack(side="bottom", anchor="w", pady=(6, 0))
        self.on_change()
        events = self.queue
        def send(kind, value): events.put((revision, kind, value))
        def worker():
            try:
                value = function(cancel.is_set, lambda message: send("progress", message))
                if not cancel.is_set(): send(kind, value)
            except Exception as exc:
                if not cancel.is_set(): send("error", str(exc))
        threading.Thread(target=worker, daemon=True, name="easy-rules").start()

    def _input_changed(self):
        if self.busy: self.cancel()
        self.on_change()

    def refresh(self):
        if self.busy: return
        if self.preview:
            self.preview.close(); self.preview.destroy(); self.preview = None
        self.inputs = self.data = None
        self.options = []; self.selected.clear()
        self._show(0)
        self.next()

    def next(self):
        if not self.can_next: return
        if self.stage == 0:
            source, results = self.source.get(), list(self.results)
            if self.inputs and getattr(self, 'input_selection', None) == (source, tuple(results)):
                self.status.set("읽어 둔 데이터와 선택한 설정을 유지합니다. 외부 파일 변경은 ‘데이터 새로 읽기’로 반영하세요.")
                self._show(1); return
            self.status.set("파일을 읽고 연결 기준을 찾고 있습니다…")
            self.pending_selection = (source, tuple(results))
            self._work(lambda cancel, progress: analyze_inputs(source, results, progress, cancel), "inputs")
        elif self.stage == 1:
            try:
                self.rule_config = make_rule(self.data, self.options, self.selected, self.name.get(), self.check_times.get())
                if self.preview: self.preview.close(); self.preview.destroy()
                self.preview = PairPreview(self.preview_host, self.rule_config, self.inputs.source, self.inputs.result_files,
                                           configured=True, preloaded=replace(self.data, rule_config=copy.deepcopy(self.rule_config)))
                self.preview.load_button.configure(text="데이터 새로 분석", command=self.refresh)
                self.preview.pack(fill="both", expand=True)
                self.status.set("검색과 페이지 이동으로 실제 데이터를 확인하세요. 규칙을 만든 뒤 전체 검증을 진행합니다.")
                self._show(2)
            except (ValueError, TypeError, KeyError) as exc: self.status.set(str(exc)); self.on_change()
        else:
            self.on_finish(self.rule_config, self.inputs, self.preview.cache)

    def back(self):
        if self.busy: return
        if self.stage == 0: self.on_exit()
        else:
            self.status.set("선택을 바꾸면 변경 전·후 예시를 다시 계산합니다.")
            self._show(self.stage - 1)

    def _select_key(self):
        index = self.key_box.current()
        if index < 0 or self.inputs is None: return
        chosen = self.inputs.candidates[index]
        self._connect_key(chosen["path"], chosen["result_path"])

    def _connect_key(self, source_path, result_path):
        self.cancel()
        self.data = None; self.options = []; self.selected = set(); self._render_fields()
        self.field_manual.pack_forget()
        self._choose_path(self.source_key_box, source_path)
        self._choose_path(self.result_key_box, result_path)
        self.connection.set(f"원본 {source_path} ↔ 보완 {result_path} · 전체 입력의 연결 수를 확인합니다…")
        self.status.set("연결된 데이터에서 수정할 항목을 준비하고 있습니다…")
        inputs = self.inputs
        def prepare(cancel, progress):
            data, options = select_key(inputs, source_path, cancel, result_path)
            def checkpoint():
                if cancel(): raise ValueError("분석을 중단했습니다.")
            return data, options, key_stats(inputs, source_path, result_path, checkpoint)
        self._work(prepare, "fields")

    def _render_fields(self):
        if not hasattr(self, "fields"): return
        selected_row = self.fields.selection()
        self.fields.delete(*self.fields.get_children())
        query = self.query.get().strip().casefold()
        for index, item in enumerate(self.options):
            if query and query not in (item["label"] + item["path"] + item.get("from", "")).casefold(): continue
            if not query and not self.show_all.get() and any(option["selected"] for option in self.options):
                if not item["selected"] and item["path"] not in self.selected and item["path"] != "/PronunciationErrorTags":
                    continue
            policy = ("목록 전체 반영" if "list" in item["types"] else "보완값 그대로") if item["path"] in self.selected else "원본 유지"
            if item["path"] in self.selected and item.get("skip_empty"): policy = "빈 값은 원본 유지"
            self.fields.insert("", "end", iid=str(index), values=(
                "☑" if item["path"] in self.selected else "☐", item["label"],
                friendly(item.get("from", item["path"])),
                f"{item['changed']} / {item['count']}", policy))
        if selected_row and self.fields.exists(selected_row[0]): self.fields.selection_set(selected_row)
        elif self.fields.get_children(): self.fields.selection_set(self.fields.get_children()[0])
        else:
            self.detail.configure(state="normal"); self.detail.delete("1.0", "end"); self.detail.configure(state="disabled")
        if self.stage == 1 and self.data is not None and not self.busy:
            self.status.set(f"선택한 {len(self.selected)}개 항목만 보완값으로 반영합니다. 나머지는 원본 유지 · 외부 파일 변경은 ‘데이터 새로 읽기’")
        if any(item["path"] == "/Segmentation/eojeols" for item in self.options):
            self.time_check.pack(anchor="w", before=self.policy_note)
            self.time_check.configure(state="normal" if "/Segmentation/eojeols" in self.selected else "disabled")
        else: self.time_check.pack_forget()
        self.on_change()

    def _recommend(self):
        if self.busy: return
        self.selected = {item["path"] for item in self.options if item["selected"]}
        self._render_fields()

    def _clear(self):
        if self.busy: return
        self.selected.clear(); self._render_fields()

    def _click(self, event):
        row = self.fields.identify_row(event.y)
        if row and self.fields.identify_column(event.x) == "#1":
            self.fields.selection_set(row); self._toggle()
            return "break"

    def _toggle(self, _=None):
        if self.busy or not self.fields.selection(): return "break"
        item = self.options[int(self.fields.selection()[0])]
        if item["path"] in self.selected: self.selected.remove(item["path"])
        else: self.selected.add(item["path"])
        self._render_fields(); self._detail()
        return "break"

    def _detail(self, _=None):
        if not self.fields.selection(): return
        item = self.options[int(self.fields.selection()[0])]
        text = f"원본 {item['path']} ← 보완 {item.get('from', item['path'])} · 연결된 앞 {item['sample_count']}쌍 중 {item['count']}쌍 확인\n원본 예시: {display(item['before'], 500)}\n보완 예시: {display(item['after'], 500)}"
        self.detail.configure(state="normal"); self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text); self.detail.configure(state="disabled")

    def cancel(self):
        self.cancel_event.set(); self.revision += 1
        self.busy = False; self.stop.pack_forget()
        self.status.set("분석을 중단했습니다. 다시 진행할 수 있습니다.")
        self.on_change()

    def _poll(self):
        if self.closed: return
        try:
            while True:
                revision, kind, value = self.queue.get_nowait()
                if revision != self.revision: continue
                if kind == "progress": self.status.set(value)
                else:
                    self.busy = False; self.stop.pack_forget()
                    if kind == "inputs":
                        self.inputs = value
                        self.input_selection = self.pending_selection
                        self.data = None; self.options = []; self.selected.clear()
                        self.field_manual.pack_forget()
                        self.summary.set(f"원본 {len(value.originals):,}건 · 보완 {len(value.incoming):,}건")
                        self._fill_choices(self.source_key_box, key_fields(value.source_fields))
                        self._fill_choices(self.result_key_box, key_fields(value.result_fields))
                        self.key_box.configure(values=[f"{c['path']} ↔ {c['result_path']} · {c['matched']:,}쌍" for c in value.candidates])
                        self.connection.set("추천이 없어도 양쪽 항목을 직접 선택할 수 있습니다. 값이 같은 레코드만 연결되며 행 순서로 짝짓지 않습니다.")
                        self.status.set("연결 기준을 선택하세요. 수정 항목은 연결된 앞 200쌍까지 비교해 추천합니다.")
                        self._show(1)
                        if value.candidates and value.candidates[0]["recommended"]:
                            self.key_box.current(0); self._select_key()
                        else:
                            self.key.set("")
                            self.key_manual.pack(fill="x", before=self.connection_label)
                            self._render_fields()
                    elif kind == "fields":
                        self.data, self.options, chosen = value
                        self.selected = {item["path"] for item in self.options if item["selected"]}
                        self.connection.set(f"원본 {self.data.rule_config['source']['key']} ↔ 보완 {self.data.rule_config['result']['key']}\n전체 입력 기준 · 연결 {chosen['matched']:,}쌍 · 연결 안 됨: 원본 {len(self.inputs.originals)-chosen['matched']:,} / 보완 {len(self.inputs.incoming)-chosen['matched']:,}건 · 중복: 원본 {chosen['source_duplicates']:,} / 보완 {chosen['result_duplicates']:,}건")
                        self._render_fields()
                        if not self.options:
                            self.status.set("연결 기준을 확인했습니다. ‘다른 필드 연결’에서 원본 대상과 보완 값을 선택하세요.")
                            self._open_connection()
                    else: self.status.set("확인 필요: " + value)
                self.on_change()
        except queue.Empty: pass
        state = (self.stage, self.busy, self.can_next, self.status.get())
        if state != self.last_state:
            self.last_state = state
            self.on_change()  # Preview loading completes on its own queue.
        self.poll_job = self.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is self: self.close()

    def close(self):
        if self.closed: return
        self.closed = True; self.cancel_event.set(); self.revision += 1
        remove_traces(self.traces)
        self.queue.close()
        self.after_cancel(self.poll_job)
        self.poll_job = None
        if self.preview: self.preview.close()
        self.inputs = self.data = None
        self.options.clear(); self.selected.clear()
        self.on_change = self.on_finish = self.on_exit = None
        self.busy = False
        release_variables(self)
