"""A read-only paired-data preview. File I/O and evaluation stay off the Tk thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

from .pair_preview import (SEARCH_FIELDS, catalog_preview, display, fingerprint, load_preview,
                           match_preview, search_catalog)
from .ui import BG, INK, MUTED


class PairPreview(ttk.Frame):
    def __init__(self, parent, profile, source_hint="", result_hints=(), *, configured=False):
        super().__init__(parent)
        self.configured, self.example_index = configured, 0
        self.auto_job = None
        self.profile, self.closed, self.busy = profile, False, False
        self.cache = self.pair = None
        self.filtered = []
        self.page = 1
        self.selected_index = None
        self.search_revision = self.selection_revision = 0
        self.search_cancel = threading.Event()
        self.search_job = None
        self.pair_cache = OrderedDict()
        self.selection_lock = threading.Lock()
        self.query = tk.StringVar()
        self.scope = tk.StringVar(value="전체")
        self.connection_state = tk.StringVar(value="전체 상태")
        self.page_size = tk.StringVar(value="25")
        self.page_number = tk.StringVar(value="1")
        self.list_status = tk.StringVar(value="데이터를 불러오면 전체 목록을 표시합니다.")
        self.data_summary = tk.StringVar(value="세션에 보관 · 단계 이동 시 유지")
        self.generation = 0
        self.cancel_event = threading.Event()
        self.queue = queue.Queue()
        hint = Path(source_hint) if source_hint else None
        self.source = tk.StringVar(value=str(hint) if hint and (configured or hint.is_file()) else "")
        self.root_path = tk.StringVar(value=str(hint if hint.is_dir() else hint.parent) if hint else "")
        self.results = list(result_hints)
        self.result_label = tk.StringVar(value=self._result_label())
        self.number = tk.StringVar(value="1")
        self.status = tk.StringVar(value="원본 JSON과 보완 결과를 선택하면 규칙의 KEY가 같은 한 쌍을 보여줍니다.")
        self.controls = []
        self._build()
        for variable in (self.query, self.scope, self.connection_state):
            variable.trace_add("write", lambda *_: self._schedule_search())
        self.source.trace_add("write", lambda *_: self._invalidate())
        self.root_path.trace_add("write", lambda *_: self._invalidate())
        self.number.trace_add("write", lambda *_: self._invalidate(cache=False))
        self.poll_job = self.after(80, self._poll)
        if configured:
            self.auto_job = self.after_idle(self._autoload)

    def _autoload(self):
        self.auto_job = None
        if not self.closed: self.load()

    def _next_example(self):
        if self.filtered:
            position = self.filtered.index(self.selected_index) if self.selected_index in self.filtered else -1
            self._select_index(self.filtered[(position + 1) % len(self.filtered)])

    def _build(self):
        inputs = self.inputs_panel = ttk.Frame(self)
        if not self.configured: inputs.pack(fill="x")
        inputs.columnconfigure(1, weight=1)
        for row, label, variable, command in (
            (0, "원본 폴더" if self.configured else "원본 JSON", self.source, self._choose_source),
            (1, "플랫폼 / 보완 결과", self.result_label, self._choose_results),
        ):
            ttk.Label(inputs, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(inputs, textvariable=variable, state="readonly").grid(row=row, column=1, sticky="ew", pady=3)
            if not self.configured:
                button = ttk.Button(inputs, text="파일 선택…", command=command)
                button.grid(row=row, column=2, padx=(8, 0), pady=3)
                self.controls.append(button)
        source_key = self.profile["source"]["key"]
        if not self.configured and isinstance(source_key, dict) and source_key.get("meta") == "relative_path":
            ttk.Label(inputs, text="원본 기준 폴더").grid(row=2, column=0, sticky="w")
            ttk.Entry(inputs, textvariable=self.root_path, state="readonly").grid(row=2, column=1, sticky="ew")
            button = ttk.Button(inputs, text="폴더 선택…", command=self._choose_root)
            button.grid(row=2, column=2, padx=(8, 0)); self.controls.append(button)
        actions = self.actions_panel = ttk.Frame(self); actions.pack(fill="x", pady=6)
        if not self.configured:
            ttk.Label(actions, text="원본 항목").pack(side="left")
        self.spin = ttk.Spinbox(actions, from_=1, to=1, textvariable=self.number, width=7)
        if not self.configured: self.spin.pack(side="left", padx=8)
        self.controls.append(self.spin)
        if self.configured:
            ttk.Label(actions, textvariable=self.data_summary, foreground=MUTED).pack(side="left", padx=(0, 12))
            self.paths_button = ttk.Button(actions, text="입력 위치 보기", command=self._toggle_paths, style="Compact.TButton")
            self.paths_button.pack(side="left", padx=(0, 8))
        self.load_button = ttk.Button(actions, text="데이터 새로 읽기" if self.configured else "KEY로 한 쌍 불러오기",
                                      command=lambda: self.load(refresh=self.configured), style="Compact.TButton")
        self.load_button.pack(side="left"); self.controls.append(self.load_button)
        self.cancel_button = ttk.Button(actions, text="중단", command=self.cancel, state="disabled", style="Compact.TButton")
        self.cancel_button.pack(side="left", padx=8)
        if self.configured:
            ttk.Button(actions, text="선택 데이터 보기 →", command=self._open_selected, style="Compact.TButton").pack(side="right")
        note = ttk.Label(self, textvariable=self.status, foreground=MUTED, wraplength=780)
        note.pack(fill="x", pady=(0, 6))
        self.bind("<Configure>", lambda e: note.configure(wraplength=max(280, e.width)))
        self.views = ttk.Notebook(self); self.views.pack(fill="both", expand=True)
        if self.configured:
            self._build_browser(self.views)
        graph, raw = ttk.Frame(self.views), ttk.Frame(self.views)
        self.views.add(graph, text="실제 값 연결")
        self.views.add(raw, text="JSON 한 쌍 · 처리 후")
        self.canvas = tk.Canvas(graph, background=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(graph, command=self.canvas.yview); scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set); self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda *_: self._draw())
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-1>", self._select_row)
        vertical = ttk.Panedwindow(raw, orient="vertical"); vertical.pack(fill="both", expand=True)
        panes = ttk.Panedwindow(vertical, orient="horizontal"); vertical.add(panes, weight=1)
        self.json_boxes = []
        for label in ("원본 라벨 JSON · 읽기 전용", "플랫폼 / 보완 JSON · 읽기 전용"):
            panel = ttk.Frame(panes, padding=5); panes.add(panel, weight=1)
            ttk.Label(panel, text=label).pack(anchor="w", pady=5)
            box = scrolledtext.ScrolledText(panel, wrap="word", font=("Consolas", 10), width=35, height=6, state="disabled")
            box.pack(fill="both", expand=True); self.json_boxes.append(box)
        output_panel = ttk.Frame(vertical, padding=5); vertical.add(output_panel, weight=1)
        self.output_note = tk.StringVar(value="규칙 적용 후 저장될 JSON · 예시")
        ttk.Label(output_panel, textvariable=self.output_note, wraplength=720).pack(anchor="w", pady=5)
        self.output_box = scrolledtext.ScrolledText(output_panel, wrap="word", font=("Consolas", 10),
                                                  width=70, height=6, state="disabled")
        self.output_box.pack(fill="both", expand=True)
        self._draw()

    def _build_browser(self, body):
        panel = ttk.Frame(body, padding=(0, 6, 0, 0)); body.add(panel, text="전체 데이터 목록")
        search = ttk.Frame(panel); search.pack(fill="x", pady=(0, 5))
        ttk.Label(search, text="검색").pack(side="left", padx=(0, 6))
        scope = ttk.Combobox(search, textvariable=self.scope, values=list(SEARCH_FIELDS), state="readonly", width=13, style="Compact.TCombobox")
        scope.pack(side="left", padx=(0, 6))
        self.search_entry = ttk.Entry(search, textvariable=self.query, style="Compact.TEntry")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _: self._search())
        ttk.Button(search, text="초기화", command=self._reset_search, style="Compact.TButton").pack(side="left", padx=6)
        ttk.Combobox(search, textvariable=self.connection_state, width=14, state="readonly", style="Compact.TCombobox",
                     values=["전체 상태", "연결됨", "원본만 있음", "보완만 있음", "KEY 중복", "KEY 해석 불가"]).pack(side="left")
        table = ttk.Frame(panel)
        self.table = ttk.Treeview(table, columns=("state", "key", "source", "result"), show="headings", height=4, selectmode="browse")
        for column, title, width in (("state", "연결 상태", 105), ("key", "KEY", 160),
                                      ("source", "원본 파일 / 위치", 240), ("result", "보완 파일 / 위치", 240)):
            self.table.heading(column, text=title); self.table.column(column, width=width, minwidth=60)
        scroll = ttk.Scrollbar(table, command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set); self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self._on_select)
        self.table.bind("<Double-1>", self._open_selected)
        self.table.bind("<Return>", self._open_selected)
        pager = ttk.Frame(panel); pager.pack(side="bottom", fill="x", pady=5)
        table.pack(fill="both", expand=True)
        ttk.Label(pager, textvariable=self.list_status, foreground=MUTED).pack(side="left")
        self.first_page = ttk.Button(pager, text="처음", width=5, command=lambda: self._go_page(1), style="Compact.TButton")
        self.previous_page = ttk.Button(pager, text="이전", width=5, command=lambda: self._go_page(self.page - 1), style="Compact.TButton")
        self.next_page = ttk.Button(pager, text="다음", width=5, command=lambda: self._go_page(self.page + 1), style="Compact.TButton")
        self.last_page = ttk.Button(pager, text="끝", width=5, command=lambda: self._go_page(self._page_count()), style="Compact.TButton")
        size = ttk.Combobox(pager, textvariable=self.page_size, values=("25", "50", "100"), state="readonly", width=4, style="Compact.TCombobox")
        size.pack(side="right"); size.bind("<<ComboboxSelected>>", lambda _: self._go_page(1))
        ttk.Label(pager, text="페이지당").pack(side="right", padx=(8, 4))
        self.last_page.pack(side="right"); self.next_page.pack(side="right", padx=4)
        self.page_entry = ttk.Entry(pager, textvariable=self.page_number, width=5, justify="center", style="Compact.TEntry")
        self.page_entry.pack(side="right"); self.page_entry.bind("<Return>", self._jump_page)
        self.previous_page.pack(side="right", padx=4); self.first_page.pack(side="right")

    def _open_selected(self, _=None):
        if self.table.selection():
            self._on_select()
            self.views.select(1)

    def _reset_search(self):
        self.query.set(""); self.scope.set("전체"); self.connection_state.set("전체 상태")

    def _schedule_search(self):
        self.search_revision += 1
        self.search_cancel.set()
        if self.search_job is not None: self.after_cancel(self.search_job)
        self.search_job = self.after(250, self._search)

    def _search(self):
        if self.search_job is not None:
            self.after_cancel(self.search_job); self.search_job = None
        if self.closed or not self.cache or self.busy or not self.configured: return
        self.search_cancel.set()
        self.search_cancel = cancel = threading.Event()
        self.search_revision += 1
        revision, generation, data = self.search_revision, self.generation, self.cache
        query, scope, state = self.query.get(), self.scope.get(), self.connection_state.get()
        self.list_status.set("검색 중…")
        def work():
            def checkpoint():
                if cancel.is_set(): raise InterruptedError
            try:
                found = search_catalog(data, query, scope, state, checkpoint)
                if not cancel.is_set(): self.queue.put((generation, "search", (revision, found)))
            except InterruptedError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _page_count(self):
        return max(1, (len(self.filtered) + int(self.page_size.get()) - 1) // int(self.page_size.get()))

    def _jump_page(self, _=None):
        try: self._go_page(int(self.page_number.get()))
        except ValueError: self.page_number.set(str(self.page))

    def _go_page(self, page):
        if not self.cache or self.busy: return
        self.page = max(1, min(page, self._page_count()))
        self._render_page()

    def _render_page(self):
        self.table.delete(*self.table.get_children())
        size = int(self.page_size.get())
        start = (self.page - 1) * size
        indices = self.filtered[start:start + size]
        for index in indices:
            entry = self.cache.catalog[index]
            self.table.insert("", "end", iid=str(index), values=(
                entry["state"], entry["key"], entry["source_location"] or "—", entry["result_location"] or "—"))
        self.page_number.set(str(self.page))
        self.list_status.set(f"검색 {len(self.filtered):,} / 전체 {len(self.cache.catalog):,}건 · {self.page}/{self._page_count()}쪽")
        for button in (self.first_page, self.previous_page):
            button.configure(state="normal" if self.page > 1 else "disabled")
        for button in (self.next_page, self.last_page):
            button.configure(state="normal" if self.page < self._page_count() else "disabled")
        if indices:
            selected = self.selected_index if self.selected_index in indices else next(
                (i for i in indices if self.cache.catalog[i]["state"] == "연결됨"), indices[0])
            self.table.selection_set(str(selected)); self.table.see(str(selected))
            self._select_index(selected)
        else:
            self.selection_revision += 1
            self.selected_index = None
            self._clear_pair()
            self.status.set("검색 결과가 없습니다. 검색어나 연결 상태를 바꿔 주세요.")

    def _on_select(self, _=None):
        selected = self.table.selection()
        if selected and self.cache: self._select_index(int(selected[0]))

    def _clear_pair(self):
        self.pair = None
        for box in self.json_boxes: self._text(box, "")
        self._text(self.output_box, "")
        self.output_note.set("규칙 적용 후 저장될 JSON")
        self._draw()

    def _select_index(self, index):
        if self.selected_index == index: return
        self.selected_index = index
        self.selection_revision += 1
        revision, generation, data = self.selection_revision, self.generation, self.cache
        self._clear_pair()
        if index in self.pair_cache:
            self.pair_cache.move_to_end(index); self.show_pair(self.pair_cache[index]); return
        self.status.set("선택한 레코드의 연결값과 규칙 적용 후 JSON을 확인하고 있습니다…")
        def work():
            try:
                with self.selection_lock:
                    if self.closed or generation != self.generation or revision != self.selection_revision: return
                    pair = catalog_preview(data, index)
                self.queue.put((generation, "selected", (revision, index, pair)))
            except Exception as exc:
                self.queue.put((generation, "selection_error", (revision, str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_paths(self):
        shown = bool(self.inputs_panel.winfo_manager())
        if shown:
            self.inputs_panel.pack_forget()
        else:
            self.inputs_panel.pack(fill="x", before=self.actions_panel)
        self.paths_button.configure(text="입력 위치 숨기기" if not shown else "입력 위치 보기")

    def _result_label(self):
        return str(self.results[0]) if len(self.results) == 1 else f"{len(self.results):,}개 파일 선택" if self.results else ""

    def _choose_source(self):
        path = filedialog.askopenfilename(parent=self, title="예시로 볼 원본 JSON",
                                         initialdir=self.root_path.get() or None,
                                         filetypes=[("JSON", "*.json *.jsonl *.ndjson")])
        if path:
            self.source.set(path)
            if not self.root_path.get() or not Path(path).resolve().is_relative_to(Path(self.root_path.get()).resolve()):
                self.root_path.set(str(Path(path).parent))
            self.number.set("1")

    def _choose_results(self):
        paths = filedialog.askopenfilenames(parent=self, title="플랫폼 최종 결과 또는 보완 데이터",
                                           filetypes=[("JSON / CSV", "*.json *.jsonl *.ndjson *.csv")])
        if paths:
            self.results = list(paths); self.result_label.set(self._result_label()); self._invalidate()

    def _choose_root(self):
        path = filedialog.askdirectory(parent=self, title="상대 경로 KEY의 원본 기준 폴더")
        if path:
            self.root_path.set(path)

    @staticmethod
    def _text(box, text):
        box.configure(state="normal"); box.delete("1.0", "end"); box.insert("1.0", text); box.configure(state="disabled")

    def _set_busy(self, value):
        self.busy = value
        for control in self.controls:
            control.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")

    def _invalidate(self, cache=True):
        self.cancel_event.set(); self.generation += 1
        self.search_cancel.set(); self.search_revision += 1; self.selection_revision += 1
        self.selected_index = None
        self.pair_cache.clear()
        if cache:
            self.cache = None
        if self.configured:
            self.filtered = []
            self.table.delete(*self.table.get_children())
            self.list_status.set("데이터를 불러오면 전체 목록을 표시합니다.")
        self.pair = None
        self._set_busy(False)
        for box in self.json_boxes:
            self._text(box, "")
        self._text(self.output_box, "")
        self.output_note.set("규칙 적용 후 저장될 JSON · 예시")
        self.status.set("선택한 입력에서 같은 KEY의 예시를 자동으로 찾습니다." if self.configured else
                        "‘KEY로 한 쌍 불러오기’를 눌러 선택한 원본의 실제 값을 확인하세요.")
        self._draw()

    def cancel(self):
        self._invalidate(cache=False)
        self.status.set("프리뷰 불러오기를 중단했습니다. 파일은 수정하지 않았습니다.")

    def load(self, refresh=False):
        if self.busy or self.closed:
            return
        if self.configured and self.cache and not refresh:
            return
        if not self.source.get() or not self.results:
            self.status.set("원본 JSON과 플랫폼 결과 또는 보완 데이터 파일을 선택하세요.")
            return
        try:
            index = int(self.number.get()) - 1
            if index < 0:
                raise ValueError
        except ValueError:
            self.status.set("원본 항목 번호는 1 이상의 정수로 입력하세요.")
            return
        self._invalidate(cache=refresh)
        self.cancel_event = cancel = threading.Event()
        generation, cached = self.generation, self.cache
        source, results = self.source.get(), list(self.results)
        root = self.root_path.get() or str(Path(source).parent)
        self.status.set("원본과 보완 결과를 읽고 KEY를 확인하고 있습니다…")
        self._set_busy(True)
        def send(kind, value):
            self.queue.put((generation, kind, value))
        def work():
            try:
                def checkpoint():
                    if cancel.is_set(): raise ValueError("프리뷰 불러오기를 중단했습니다.")
                stamp = fingerprint(source, results, root, self.profile["source"].get("glob", "**/*.json"), checkpoint) if cached else None
                data = cached if cached and cached.fingerprint == stamp else load_preview(
                    self.profile, source, results, root, progress=lambda text: send("progress", text), cancel=cancel.is_set)
                if cancel.is_set():
                    return
                send("loaded", data)
                if self.configured:
                    send("ready", None)
                    return
                pair = match_preview(data, index)
                if not cancel.is_set():
                    send("pair", pair)
            except Exception as exc:
                send("error", str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, kind, value = self.queue.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "progress":
                    self.status.set(value)
                elif kind == "loaded":
                    self.cache = value; self.spin.configure(to=len(value.originals))
                    self.data_summary.set(f"원본 {len(value.originals):,} · 보완 {value.result_total:,}건 · 세션 보관")
                elif kind == "ready":
                    self._set_busy(False)
                    self.status.set(f"원본 {len(self.cache.originals):,}건 · 보완 {self.cache.result_total:,}건을 세션에 보관했습니다.")
                    self._search()
                elif kind == "search":
                    revision, found = value
                    if revision == self.search_revision:
                        self.filtered = found; self.page = 1
                        self._render_page()
                elif kind == "selected":
                    revision, index, pair = value
                    if revision == self.selection_revision:
                        self.pair_cache[index] = pair
                        if len(self.pair_cache) > 16: self.pair_cache.popitem(last=False)
                        self.show_pair(pair)
                elif kind == "selection_error":
                    revision, message = value
                    if revision == self.selection_revision: self.status.set("프리뷰 확인: " + message)
                elif kind == "error":
                    self._set_busy(False); self.status.set("프리뷰 확인: " + value)
                elif kind == "pair":
                    self._set_busy(False); self.show_pair(value)
        except queue.Empty:
            pass
        self.poll_job = self.after(80, self._poll)

    def show_pair(self, pair):
        self.pair = pair
        extra = f" · KEY 해석 불가 {pair['invalid_result_keys']:,}건" if pair["invalid_result_keys"] else ""
        location = "" if self.configured else f"\n{pair['source_location']} ↔ {pair['result_location']}"
        self.status.set(f"KEY {display(pair['key'], 100)} · {pair['message']}{extra}{location}")
        for box, name in zip(self.json_boxes, ("source", "result")):
            self._text(box, display(pair[name], 200000))
        self._text(self.output_box, display(pair["output"], 200000))
        self.output_note.set(("연결 불가 · 원본 유지 (전체 검증에서 확인)" if pair["source"] is not None else
                              "연결할 원본 없음 · 출력 대상 없음") if pair["status"] == "unpaired" else
                             "검토 필요 · 저장 시 원본 유지" if pair["status"] == "review_required" else
                             "조건 제외 · 저장 시 원본 유지" if pair["status"] == "excluded" else
                             "변경 없음 · 저장 시 원본과 동일" if pair["status"] == "unchanged" else
                             "규칙 적용 후 저장될 JSON · 예시 (아직 저장하지 않음)")
        self.canvas.yview_moveto(0); self._draw()
        self._set_busy(False)

    def _draw(self):
        if self.closed:
            return
        from .rule_review import COLORS, LABELS, describe_expression
        canvas = self.canvas; canvas.delete("all")
        width = max(500, canvas.winfo_width())
        if not self.pair:
            canvas.create_text(width / 2, 55, text="규칙의 KEY가 같은 원본과 보완 결과 한 쌍을 불러오세요.\n데이터와 규칙은 수정하지 않습니다.",
                               width=width - 40, fill=MUTED, font=("맑은 고딕", 10))
            canvas.configure(scrollregion=(0, 0, width, 150))
            return
        gap, pad, y = 90, 12, 10
        if not self.pair["rows"]:
            canvas.create_text(width / 2, 45, text=self.pair["message"] + "\n‘JSON 한 쌍 · 처리 후’에서 해당 원문을 확인하세요.",
                               width=width - 40, fill=MUTED, font=("맑은 고딕", 10))
            canvas.configure(scrollregion=(0, 0, width, 120))
            return
        cell = (width - gap - pad * 2) / 2
        right = pad + cell + gap
        def short(text):
            return text if len(text) <= 450 else text[:450] + "… (클릭하여 더 보기)"
        for index, row in enumerate(self.pair["rows"]):
            color, tag = COLORS[row["kind"]], f"pair:{index}"
            title = canvas.create_text(pad, y, text=f"{index + 1:02d}  {LABELS[row['kind']]}",
                                       anchor="nw", fill=color, font=("맑은 고딕", 10, "bold"), width=width - 24, tags=tag)
            y = canvas.bbox(title)[3] + 8
            source_path = describe_expression(row["source_path"], "source") if row["kind"] == "key" else row["source_path"]
            result_path = describe_expression(row["result_expr"])
            if row.get("nested"):
                result_path = "해당 목록 항목 · " + result_path
            left = source_path + "\n\n원본 값\n" + short(row["source"])
            target = result_path + "\n\n보완 값\n" + short(row["result"])
            if row["evaluated"] and row["evaluated"] != row["result"]:
                target += "\n\n변환 후 가져올 값\n" + short(row["evaluated"])
            a = canvas.create_text(pad + 10, y + 10, text=left, anchor="nw", width=cell - 20, fill=INK, font=("맑은 고딕", 10), tags=tag)
            b = canvas.create_text(right + 10, y + 10, text=target, anchor="nw", width=cell - 20, fill=INK, font=("맑은 고딕", 10), tags=tag)
            bottom = max(canvas.bbox(a)[3], canvas.bbox(b)[3]) + 10
            for x in (pad, right):
                rect = canvas.create_rectangle(x, y, x + cell, bottom, fill="white", outline="#dce3ee", tags=tag)
                canvas.tag_lower(rect, a)
            canvas.create_line(pad + cell + 6, (y + bottom) / 2, right - 6, (y + bottom) / 2, fill=color, width=2,
                               arrow="both" if row["kind"] == "key" else "first", tags=tag)
            note = canvas.create_text(pad, bottom + 6, text=row["note"], anchor="nw", width=width - 24,
                                      fill=MUTED, font=("맑은 고딕", 9), tags=tag)
            y = canvas.bbox(note)[3] + 18
        if self.pair["total_connections"] > len(self.pair["rows"]):
            canvas.create_text(pad, y, text=f"연결값은 앞 {len(self.pair['rows'])}개까지 표시합니다. 전체 한 건은 ‘JSON 한 쌍’에서 확인하세요.",
                               anchor="nw", width=width - 24, fill=MUTED)
            y += 60
        canvas.configure(scrollregion=(0, 0, width, y))

    def _select_row(self, event):
        if not self.pair:
            return
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x + 1, y + 1)):
            tag = next((t for t in self.canvas.gettags(item) if t.startswith("pair:")), None)
            if tag:
                row = self.pair["rows"][int(tag.split(":")[1])]
                dialog = tk.Toplevel(self)
                dialog.title("연결된 실제 값 · 읽기 전용"); dialog.geometry("800x600"); dialog.transient(self.winfo_toplevel())
                box = scrolledtext.ScrolledText(dialog, wrap="word", font=("Consolas", 10))
                box.pack(fill="both", expand=True)
                self._text(box, f"{row['note']}\n\n원본 값\n{row['source']}\n\n보완 값\n{row['result']}\n\n변환 후 가져올 값\n{row['evaluated']}")
                break

    def close(self):
        if self.closed:
            return
        self.closed = True; self.cancel_event.set(); self.generation += 1
        self.search_cancel.set()
        if self.search_job is not None:
            self.after_cancel(self.search_job); self.search_job = None
        if self.auto_job is not None:
            self.after_cancel(self.auto_job)
            self.auto_job = None
        self.after_cancel(self.poll_job)
        self.cache = self.pair = None
        self.pair_cache.clear()
