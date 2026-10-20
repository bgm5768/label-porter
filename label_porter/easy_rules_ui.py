"""Inline, three-step intent picker. No editable JSON or connection canvas."""
from __future__ import annotations

import copy
import queue
import threading
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

from .easy_rules import analyze_inputs, friendly, make_profile, select_key
from .pair_preview import display
from .pair_preview_ui import PairPreview
from .ui import ScrollPage, card


class EasyRules(ttk.Frame):
    TITLES = ("데이터 선택", "반영할 내용 선택", "변경 전·후 확인")

    def __init__(self, parent, on_change, on_finish, on_exit, source="", results=()):
        super().__init__(parent)
        self.on_change, self.on_finish, self.on_exit = on_change, on_finish, on_exit
        self.stage, self.busy, self.closed = 0, False, False
        self.revision, self.cancel_event = 0, threading.Event()
        self.queue = queue.Queue()
        self.inputs = self.data = self.preview = self.profile = None
        self.options, self.selected = [], set()
        self.source = tk.StringVar(value=source)
        self.results = list(results)
        self.status = tk.StringVar(value="같은 필드 구조의 원본과 보완 데이터를 선택하세요. 파일은 수정하지 않습니다.")
        self.name = tk.StringVar(value="데이터로 만든 규칙")
        self.key = tk.StringVar()
        self.query = tk.StringVar()
        self.show_all = tk.BooleanVar(value=False)
        self.check_times = tk.BooleanVar(value=True)
        self.summary = tk.StringVar()
        self.connection = tk.StringVar()
        self._build()
        self.query.trace_add("write", lambda *_: self._render_fields())
        self.show_all.trace_add("write", lambda *_: self._render_fields())
        self.source.trace_add("write", lambda *_: self._input_changed())
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
        group = card(choose.body, "2. 보완 데이터", "원본과 같은 필드 구조의 최종 수정본을 선택하세요. 플랫폼 전용 구조나 필드명이 다르면 기존 AI 규칙을 사용합니다.")
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
        ttk.Label(row, text="같은 데이터를 찾는 기준").pack(side="left", padx=(0, 10))
        self.key_box = ttk.Combobox(row, textvariable=self.key, state="readonly")
        self.key_box.pack(fill="x", expand=True)
        self.key_box.bind("<<ComboboxSelected>>", lambda _: self._select_key())
        ttk.Label(options, textvariable=self.connection, style="Muted.TLabel", wraplength=760).pack(fill="x", pady=(6, 10))
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
        self.fields = ttk.Treeview(table, columns=("check", "name", "difference", "policy"), show="headings", height=7)
        for col, title, width in (("check", "반영", 50), ("name", "수정할 내용", 280),
                                  ("difference", "표본 중 변경", 100), ("policy", "반영 방식", 170)):
            self.fields.heading(col, text=title)
            self.fields.column(col, width=width, minwidth=45, stretch=col != "check")
        scroll = ttk.Scrollbar(table, command=self.fields.yview)
        scroll.pack(side="right", fill="y")
        self.fields.configure(yscrollcommand=scroll.set)
        self.fields.pack(fill="both", expand=True)
        self.fields.bind("<Button-1>", self._click)
        self.fields.bind("<space>", self._toggle)
        self.fields.bind("<<TreeviewSelect>>", self._detail)
        self.detail = scrolledtext.ScrolledText(options, height=4, wrap="word", font=("맑은 고딕", 9), state="disabled", relief="flat")
        self.detail.pack(fill="x", pady=(8, 4))
        self.time_check = ttk.Checkbutton(options, text="어절 시간의 기본 오류 확인 · 음수 / 시작 > 종료", variable=self.check_times)
        self.time_check.pack(anchor="w")
        self.policy_note = ttk.Label(options, text="목록은 순서·ID·추가·삭제·빈 목록까지 보완본 그대로 반영합니다. 태그 등 선택하지 않은 값은 유지됩니다.",
                                    style="Muted.TLabel", wraplength=760)
        self.policy_note.pack(fill="x", pady=(5, 0))
        self.stop = ttk.Button(self, text="분석 중단", command=self.cancel)

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
        self.busy = True
        self.revision += 1
        revision = self.revision
        cancel = self.cancel_event = threading.Event()
        self.stop.pack(side="bottom", anchor="w", pady=(6, 0))
        self.on_change()
        def send(kind, value): self.queue.put((revision, kind, value))
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
            if self.inputs and str(Path(source).expanduser().resolve()) == self.inputs.source and [str(Path(p).resolve()) for p in results] == self.inputs.result_files:
                self.status.set("읽어 둔 데이터와 선택한 설정을 유지합니다. 외부 파일 변경은 ‘데이터 새로 읽기’로 반영하세요.")
                self._show(1); return
            self.status.set("파일을 읽고 연결 기준을 찾고 있습니다…")
            self._work(lambda cancel, progress: analyze_inputs(source, results, progress, cancel), "inputs")
        elif self.stage == 1:
            try:
                self.profile = make_profile(self.data, self.options, self.selected, self.name.get(), self.check_times.get())
                if self.preview: self.preview.close(); self.preview.destroy()
                self.preview = PairPreview(self.preview_host, self.profile, self.inputs.source, self.inputs.result_files,
                                           configured=True, preloaded=replace(self.data, profile=copy.deepcopy(self.profile)))
                self.preview.load_button.configure(text="데이터 새로 분석", command=self.refresh)
                self.preview.pack(fill="both", expand=True)
                self.status.set("검색과 페이지 이동으로 실제 데이터를 확인하세요. 규칙을 만든 뒤 전체 검증을 진행합니다.")
                self._show(2)
            except (ValueError, TypeError, KeyError) as exc: self.status.set(str(exc)); self.on_change()
        else:
            self.on_finish(self.profile, self.inputs, self.preview.cache)

    def back(self):
        if self.busy: return
        if self.stage == 0: self.on_exit()
        else:
            self.status.set("선택을 바꾸면 변경 전·후 예시를 다시 계산합니다.")
            self._show(self.stage - 1)

    def _select_key(self):
        index = self.key_box.current()
        if index < 0 or self.inputs is None: return
        self.cancel()
        self.data = None; self.options = []; self.selected = set(); self._render_fields()
        chosen = self.inputs.candidates[index]
        self.connection.set(f"전체 입력 기준 · 연결 {chosen['matched']:,}쌍 · 연결 안 됨: 원본 {len(self.inputs.originals)-chosen['matched']:,} / 보완 {len(self.inputs.incoming)-chosen['matched']:,}건\n중복: 원본 {chosen['source_duplicates']:,} / 보완 {chosen['result_duplicates']:,}건 · 항목 추천은 앞 200쌍 표본")
        self.status.set("연결된 데이터에서 수정할 항목을 준비하고 있습니다…")
        inputs = self.inputs
        self._work(lambda cancel, progress: select_key(inputs, chosen["path"], cancel), "fields")

    def _render_fields(self):
        if not hasattr(self, "fields"): return
        selected_row = self.fields.selection()
        self.fields.delete(*self.fields.get_children())
        query = self.query.get().strip().casefold()
        for index, item in enumerate(self.options):
            if query and query not in (item["label"] + item["path"]).casefold(): continue
            if not query and not self.show_all.get() and any(option["selected"] for option in self.options):
                if not item["selected"] and item["path"] not in self.selected and item["path"] != "/PronunciationErrorTags":
                    continue
            policy = ("목록 전체 반영" if "list" in item["types"] else "보완값 그대로") if item["path"] in self.selected else "원본 유지"
            self.fields.insert("", "end", iid=str(index), values=(
                "☑" if item["path"] in self.selected else "☐", item["label"],
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
        text = f"{item['label']}  ·  {item['path']} · 표본 {item['sample_count']}쌍 중 공통 항목 {item['count']}쌍\n원본 예시: {display(item['before'], 500)}\n보완 예시: {display(item['after'], 500)}"
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
                        self.summary.set(f"원본 {len(value.originals):,}건 · 보완 {len(value.incoming):,}건")
                        self.key_box.configure(values=[f"{friendly(c['path'])}  ({c['path']}) · {c['matched']:,}쌍" for c in value.candidates])
                        self.status.set("연결 기준을 선택하세요. 수정 항목은 연결된 앞 200쌍까지 비교해 추천합니다.")
                        self._show(1)
                        if value.candidates[0]["recommended"]:
                            self.key_box.current(0); self._select_key()
                        else:
                            self.key.set(""); self.data = None; self.options = []; self.selected.clear(); self._render_fields()
                    elif kind == "fields":
                        self.data, self.options = value
                        self.selected = {item["path"] for item in self.options if item["selected"]}
                        self.status.set("체크한 항목만 보완값으로 반영합니다. 목록 선택 시 추가·삭제도 함께 반영합니다.")
                        self._render_fields()
                    else: self.status.set("확인 필요: " + value)
                self.on_change()
        except queue.Empty: pass
        state = (self.stage, self.busy, self.can_next, self.status.get())
        if state != self.last_state:
            self.last_state = state
            self.on_change()  # Preview loading completes on its own queue.
        self.poll_job = self.after(100, self._poll)

    def close(self):
        if self.closed: return
        self.closed = True; self.cancel_event.set(); self.revision += 1
        self.after_cancel(self.poll_job)
        if self.preview: self.preview.close()
        self.inputs = self.data = None
