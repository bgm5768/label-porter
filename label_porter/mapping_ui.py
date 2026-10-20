"""Native visual wiring board for annotation fields."""
from __future__ import annotations

import copy
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, scrolledtext, ttk

from .formats import DataError
from .config import load_config, validate_config
from .mapping import (FIELD_LIMIT, SAMPLE_LIMIT, TRANSFORMS, Scope, compact,
                      compile_profile, import_profile, load_samples, preview_profile)
from .ui import ScrollPage

COLORS = {"key": "#3878e8", "value": "#059b83", "guard": "#c27d15", "array": "#8658d9"}
KINDS = {"key": "같은 건 찾기", "value": "바꿀 값", "guard": "이전값 대조", "array": "목록 안쪽 연결"}
TYPES = {"string": "문자", "integer": "정수", "number": "숫자", "boolean": "참/거짓", "array": "배열", "mixed": "혼합", "null": "비어 있음"}


class MappingWindow:
    def __init__(self, parent, on_apply, source_hint="", result_hint="", source_root="", source_pointer="", result_pointer="", initial_profile=None, embedded=False, on_cancel=None):
        self.embedded, self.on_cancel = embedded, on_cancel
        self.applied = False
        self.window = ttk.Frame(parent) if embedded else tk.Toplevel(parent)
        if embedded:
            self.window.pack(fill="both", expand=True)
        else:
            self.window.title("필드 연결 · 프로젝트 규칙 만들기")
            screen_width, screen_height = parent.winfo_screenwidth(), parent.winfo_screenheight()
            self.window.geometry(f"{min(1320, screen_width - 70)}x{min(930, screen_height - 100)}")
            self.window.minsize(min(1040, screen_width - 70), min(740, screen_height - 100))
            self.window.transient(parent)
            self.window.grab_set()
        self.on_apply = on_apply
        self.source_root = source_root
        self.initial_pointers = (source_pointer, result_pointer)
        self.profile = copy.deepcopy(initial_profile)
        self.source_selection = copy.deepcopy(source_hint)
        self.result_selection = copy.deepcopy(result_hint)
        self.source_path = tk.StringVar(value=self._selection_label(source_hint))
        self.result_path = tk.StringVar(value=self._selection_label(result_hint))
        self.source_container = tk.StringVar()
        self.result_container = tk.StringVar()
        self.name = tk.StringVar(value="새 프로젝트 · 선 연결")
        self.mode = tk.StringVar(value="key")
        self.query = tk.StringVar()
        self.details = tk.BooleanVar(value=False)
        self.card_font = font.Font(self.window, family="맑은 고딕", size=10, weight="bold")
        self.sample_font = font.Font(self.window, family="맑은 고딕", size=9)
        self.status = tk.StringVar(value="원본 라벨 폴더 또는 여러 JSON 파일과 작업 결과를 선택하세요.")
        self.sample_info = tk.StringVar(value="규칙을 만드는 예시 파일이며, 원본을 수정하지 않습니다.")
        self.step_hint = tk.StringVar(value="예시 파일을 읽으면 같은 건을 찾는 기준부터 안내합니다.")
        self.breadcrumb = tk.StringVar(value="전체 데이터")
        self.source_transform = tk.StringVar(value="그대로")
        self.result_transform = tk.StringVar(value="그대로")
        self.nonempty = tk.BooleanVar(value=True)
        self.allow_missing = tk.BooleanVar(value=False)
        self.condition_path = tk.StringVar(value="항상 적용")
        self.condition_value = tk.StringVar()
        self.scope = self.root_scope = None
        self.history = []
        self.selected = None
        self.guard_owner = None
        self.pending = None
        self.source_sample = self.result_sample = None
        self.hits = []
        self.busy = False
        self.dirty = True
        self.closed = False
        self.last_checked = None
        self.queue = queue.Queue()
        self._build()
        if not embedded:
            self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.poll_job = self.window.after(80, self._poll)
        self.load_job = None
        self.query.trace_add("write", lambda *_: self._redraw())
        self.source_path.trace_add("write", lambda *_: self._mark_dirty())
        self.result_path.trace_add("write", lambda *_: self._mark_dirty())
        self.name.trace_add("write", lambda *_: self._invalidate())
        if self.profile:
            self._restore_profile(self.profile)
        if source_hint and result_hint:
            self.load_job = self.window.after(150, self.load_files)

    def _build(self):
        shell = ttk.Frame(self.window, padding=0 if self.embedded else 16)
        shell.pack(fill="both", expand=True)
        if not self.embedded:
            ttk.Label(shell, text="프로젝트 규칙 만들기", font=("맑은 고딕", 21, "bold")).pack(anchor="w", pady=(0, 10))
        footer = ttk.Frame(shell, padding=(0, 10, 0, 0))
        footer.pack(side="bottom", fill="x")
        status_label = ttk.Label(footer, textvariable=self.status, foreground="#405573", wraplength=800)
        status_label.pack(fill="x", pady=(0, 8))
        footer.bind("<Configure>", lambda e: status_label.configure(wraplength=max(300, e.width)))
        self.footer_actions = ttk.Frame(footer)
        self.footer_actions.pack(fill="x")
        ttk.Button(self.footer_actions, text="개요로 돌아가기", command=self._close).pack(side="left")
        self.next_button = ttk.Button(self.footer_actions, text="자료 읽고 연결하기 →", command=self.load_files, style="Primary.TButton")
        self.next_button.pack(side="right")
        self.apply_button = ttk.Button(self.footer_actions, text="규칙 확정 · 입력 확인 →", command=self._apply, style="Primary.TButton", state="disabled")
        self.pages = ttk.Notebook(shell)
        self.pages.pack(fill="both", expand=True)
        sample_page = ScrollPage(self.pages, padding=12)
        samples = sample_page.body
        self.board_page = board_page = ttk.Frame(self.pages, padding=10)
        review_page = ScrollPage(self.pages, padding=12)
        review = review_page.body
        for frame, title in ((sample_page, "① 대조할 자료"), (board_page, "② 필드 연결"), (review_page, "③ 확인·확정")):
            self.pages.add(frame, text=title)
        self.pages.bind("<<NotebookTabChanged>>", self._page_changed)
        ttk.Label(samples, text="대조할 원본 라벨과 작업 결과를 선택하세요", font=("맑은 고딕", 16, "bold")).pack(anchor="w")
        ttk.Label(samples, text="폴더 전체, 여러 JSON, 한 개의 통합 JSON을 모두 사용할 수 있습니다.\n규칙 확정 후 입력 단계로 가져와 전체 처리 범위를 확인합니다.", wraplength=720).pack(anchor="w", pady=(8, 14))
        for label, variable, side in (("보완 전 원본 라벨 · 폴더 / 여러 파일", self.source_path, "source"), ("보완·검수 후 작업 결과 · 폴더 / 여러 파일", self.result_path, "result")):
            group = ttk.LabelFrame(samples, text=label, padding=14)
            group.pack(fill="x", pady=8)
            ttk.Entry(group, textvariable=variable).pack(fill="x", pady=(0, 8))
            actions = ttk.Frame(group); actions.pack(fill="x")
            ttk.Button(actions, text="폴더 선택", command=lambda side=side: self._choose_folder(side)).pack(side="left")
            ttk.Button(actions, text="JSON 여러 파일", command=lambda side=side: self._choose(side)).pack(side="left", padx=8)
            ttk.Button(actions, text="선택 목록", command=lambda side=side: self._show_selection(side)).pack(side="right")
        location = ttk.LabelFrame(samples, text="파일 안에서 한 건씩 들어 있는 위치", padding=12)
        location.pack(fill="x", pady=14)
        ttk.Label(location, text="자료를 읽으면 자동 제안합니다. 파일마다 한 건이면 ‘파일 전체’, 내부 목록에 여러 건이 있으면 해당 목록을 선택합니다.", wraplength=700).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(location)
        row.pack(fill="x")
        ttk.Label(row, text="원본").pack(side="left")
        self.source_combo = ttk.Combobox(row, textvariable=self.source_container, state="readonly", width=24)
        self.source_combo.pack(side="left", padx=(8, 20))
        ttk.Label(row, text="결과").pack(side="left")
        self.result_combo = ttk.Combobox(row, textvariable=self.result_container, state="readonly", width=24)
        self.result_combo.pack(side="left", padx=8)
        for combo in (self.source_combo, self.result_combo):
            combo.bind("<<ComboboxSelected>>", self._container_changed)
        ttk.Label(samples, textvariable=self.sample_info, foreground="#62728a", wraplength=960).pack(anchor="w", pady=8)
        row = ttk.Frame(samples)
        row.pack(fill="x", pady=14)
        ttk.Button(row, text="AI / 저장된 규칙 JSON 불러오기", command=self._load_rule).pack(side="left")
        self.load_button = self.next_button

        heading = ttk.Frame(board_page)
        heading.pack(fill="x")
        ttk.Label(heading, text="규칙 이름").pack(side="left")
        ttk.Entry(heading, textvariable=self.name, width=25).pack(side="left", padx=8)
        ttk.Button(heading, text="자료 변경", command=lambda: self.pages.select(0)).pack(side="right")
        hint = ttk.Label(board_page, textvariable=self.step_hint, font=("맑은 고딕", 10, "bold"), foreground="#285886", wraplength=760)
        hint.pack(fill="x", pady=(10, 5))
        board_page.bind("<Configure>", lambda e: hint.configure(wraplength=max(300, e.width-24)))
        trail = ttk.Frame(board_page)
        trail.pack(fill="x", pady=(0, 8))
        self.back_button = ttk.Button(trail, text="← 상위 목록", command=self._go_back, state="disabled")
        self.back_button.pack(side="left")
        ttk.Label(trail, textvariable=self.breadcrumb, wraplength=220).pack(side="left", padx=10)
        ttk.Button(trail, text="기준 변경", command=self._change_key).pack(side="right")
        ttk.Button(trail, text="고급 규칙 원문", command=self._show_raw).pack(side="right", padx=8)

        completed = ttk.LabelFrame(board_page, text="완료한 연결", padding=6)
        self.completed_frame = completed
        self.connection_list_open = tk.BooleanVar(value=False)
        self.wire_list = ttk.Treeview(completed, columns=("kind", "source", "result"), show="headings", height=2, selectmode="browse")
        for key, title, width in (("kind", "역할", 130), ("source", "원본에서 찾거나 바꿀 곳", 370), ("result", "작업 결과에서 가져올 값", 370)):
            self.wire_list.heading(key, text=title)
            self.wire_list.column(key, width=width, minwidth=80)
        scrollbar = ttk.Scrollbar(completed, orient="vertical", command=self.wire_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.wire_list.configure(yscrollcommand=scrollbar.set)
        self.wire_list.pack(fill="x")
        self.wire_list.bind("<<TreeviewSelect>>", self._select_from_list)

        search = ttk.Frame(board_page)
        search.pack(fill="x", pady=(0, 6))
        ttk.Label(search, text="추가할 필드 찾기").pack(side="left")
        ttk.Entry(search, textvariable=self.query, width=23).pack(side="left", padx=8)
        ttk.Button(search, text="검색 지우기", command=lambda: self.query.set("")).pack(side="left")
        ttk.Checkbutton(search, text="연결 목록 보기", variable=self.connection_list_open,
                        command=lambda: completed.pack(fill="x", before=search, pady=(0, 8)) if self.connection_list_open.get() else completed.pack_forget()).pack(side="right")
        board = ttk.Frame(board_page)
        board.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(board, background="#f4f6fb", highlightthickness=0, height=260)
        scroll = ttk.Scrollbar(board, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self._redraw())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
        self.canvas.bind("<Delete>", lambda event: self._delete_wire())
        self.canvas.bind("<Escape>", lambda event: self._cancel_pending())

        actions = ttk.Frame(board_page)
        actions.pack(fill="x", pady=8)
        self.array_button = ttk.Button(actions, text="목록 안쪽 연결 →", command=self._enter_array, state="disabled")
        self.array_button.pack(side="left")
        ttk.Button(actions, text="선택한 연결 삭제", command=self._delete_wire).pack(side="left", padx=8)
        ttk.Checkbutton(actions, text="선택한 연결의 세부 설정", variable=self.details, command=self._toggle_details).pack(side="left")
        self.board_canvas_frame = board
        self.inspector = inspector = ScrollPage(board_page)
        opts = ttk.LabelFrame(inspector.body, text="선택한 연결의 설정 · 변경 후 ‘설정 적용’을 누르세요", padding=12)
        opts.grid(row=0, column=0, sticky="ew")
        inspector.body.columnconfigure(0, weight=1)
        line = ttk.Frame(opts)
        line.pack(fill="x")
        ttk.Label(line, text="원본 기준값 변환").pack(side="left")
        self.source_transform_combo = ttk.Combobox(line, textvariable=self.source_transform, values=["규칙 설정 유지", *TRANSFORMS], width=15, state="disabled")
        self.source_transform_combo.pack(side="left", padx=6)
        ttk.Label(line, text="결과값 변환").pack(side="left")
        self.result_transform_combo = ttk.Combobox(line, textvariable=self.result_transform, values=["규칙 설정 유지", *TRANSFORMS], width=15, state="disabled")
        self.result_transform_combo.pack(side="left", padx=6)
        flags = ttk.Frame(opts); flags.pack(fill="x", pady=10)
        ttk.Checkbutton(flags, text="빈 수정값은 보류", variable=self.nonempty).pack(anchor="w")
        ttk.Checkbutton(flags, text="빠진 목록 항목을 유지하고 나머지 반영", variable=self.allow_missing).pack(anchor="w", pady=5)
        cond = ttk.Frame(opts)
        cond.pack(fill="x", pady=(8, 0))
        ttk.Label(cond, text="이 결과 필드가").pack(side="left")
        self.condition_combo = ttk.Combobox(cond, textvariable=self.condition_path, width=24, state="readonly")
        self.condition_combo.pack(side="left", padx=6)
        ttk.Entry(cond, textvariable=self.condition_value, width=14).pack(side="left")
        ttk.Label(cond, text="일 때만 반영").pack(side="left", padx=6)
        option_actions = ttk.Frame(opts); option_actions.pack(fill="x", pady=(12, 0))
        ttk.Button(option_actions, text="설정 적용", command=self._save_options, style="Primary.TButton").pack(side="right")
        ttk.Button(option_actions, text="플랫폼에 가져갔던 이전값도 대조", command=self._add_guard).pack(side="left")
        self.option_frames = (opts,)
        self._toggle_details()
        # Keep actions above the expandable canvas so they can never be pushed off-screen.
        board.pack_forget()
        board.pack(fill="both", expand=True)

        ttk.Label(review, text="원본이 어떻게 바뀌는지 확인하세요", font=("맑은 고딕", 16, "bold")).pack(anchor="w")
        ttk.Label(review, text="자료가 있으면 연결키와 최대 120건의 변경 전후를 확인합니다.\n예시 검사는 참고용입니다. 실제 저장 전에는 전체 데이터 검증을 반드시 거칩니다.", wraplength=720).pack(anchor="w", pady=8)
        self.test_button = ttk.Button(review, text="변경 전후 검사하기", command=self._test, state="disabled")
        self.test_button.pack(anchor="w", pady=8)
        self.preview_box = scrolledtext.ScrolledText(review, wrap="word", font=("맑은 고딕", 10), height=18, state="disabled")
        self.preview_box.pack(fill="both", expand=True)
        bottom = ttk.Frame(review)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Button(bottom, text="← 연결 수정으로", command=lambda: self.pages.select(1)).pack(side="left")

    def _page_changed(self, event=None):
        if self.closed:
            return
        index = self.pages.index(self.pages.select())
        self.next_button.pack_forget()
        self.apply_button.pack_forget()
        if index < 2:
            self.next_button.configure(text="자료 읽고 연결하기 →" if index == 0 else "연결 확인하고 계속 →",
                                       command=self.load_files if index == 0 else self._review_step)
            self.next_button.pack(side="right")
        else:
            self.apply_button.pack(side="right")
            self._update_apply()

    def _update_apply(self):
        try:
            self._config()
            enabled = not self.busy
        except (ValueError, TypeError, KeyError):
            enabled = False
        self.apply_button.configure(state="normal" if enabled else "disabled")

    @staticmethod
    def _selection_label(selection):
        if isinstance(selection, (list, tuple)):
            return f"{len(selection):,}개 파일 선택 · {Path(selection[0]).name}" if selection else ""
        return str(selection or "")

    def _selection(self, side):
        stored = self.source_selection if side == "source" else self.result_selection
        text = (self.source_path if side == "source" else self.result_path).get().strip()
        return copy.deepcopy(stored) if text == self._selection_label(stored) else text

    def _set_selection(self, side, selection):
        if side == "source":
            self.source_selection = copy.deepcopy(selection)
            self.source_path.set(self._selection_label(selection))
        else:
            self.result_selection = copy.deepcopy(selection)
            self.result_path.set(self._selection_label(selection))

    def _choose(self, side):
        paths = filedialog.askopenfilenames(title="원본 라벨 JSON 여러 개 선택" if side == "source" else "작업 결과 JSON 여러 개 선택", parent=self.window,
                                           filetypes=[("JSON 파일", "*.json *.jsonl *.ndjson")])
        if paths:
            self._set_selection(side, list(dict.fromkeys(paths)))

    def _choose_folder(self, side):
        folder = filedialog.askdirectory(title="원본 라벨 JSON 폴더 선택" if side == "source" else "작업 결과 JSON 폴더 선택", parent=self.window)
        if folder:
            self._set_selection(side, folder)

    def _show_selection(self, side):
        selection = self._selection(side)
        sample = self.source_sample if side == "source" else self.result_sample
        entries = list(selection) if isinstance(selection, (list, tuple)) else [selection]
        if sample and getattr(sample, "selection", None) == selection:
            entries = [str(file.path) for file in sample.files]
        dialog = tk.Toplevel(self.window)
        dialog.title("선택한 원본 라벨" if side == "source" else "선택한 작업 결과")
        dialog.geometry("900x520")
        dialog.transient(self.window.winfo_toplevel())
        note = f"{len(entries):,}개 선택 · 폴더는 파일 읽기를 실행한 뒤 내부 목록을 확인할 수 있습니다."
        if sample and getattr(sample, "selection", None) == selection:
            note += f"\n상대 경로의 기준 폴더: {sample.root}"
        ttk.Label(dialog, text=note, wraplength=850).pack(padx=12, pady=12)
        box = scrolledtext.ScrolledText(dialog, wrap="none", font=("맑은 고딕", 10))
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        box.insert("1.0", "\n".join(str(entry) for entry in entries))
        box.configure(state="disabled")

    def _toggle_details(self):
        for frame in self.option_frames:
            frame.grid() if self.details.get() else frame.grid_remove()
        if hasattr(self, "inspector"):
            self.board_canvas_frame.pack_forget()
            self.inspector.pack_forget()
            (self.inspector if self.details.get() else self.board_canvas_frame).pack(fill="both", expand=True)

    @staticmethod
    def _fit(text, text_font, width, keep_end=False):
        if text_font.measure(text) <= width:
            return text
        while text and text_font.measure("…" + text if keep_end else text + "…") > width:
            text = text[1:] if keep_end else text[:-1]
        return "…" + text if keep_end else text + "…"

    def _mark_dirty(self):
        self.dirty = True
        self._invalidate()
        self.test_button.configure(state="disabled")
        self.step_hint.set("예시 파일이 바뀌었습니다. 1단계의 ‘파일 읽고 연결 화면으로’를 눌러 주세요.")

    def _invalidate(self):
        self.last_checked = None
        self._update_apply()

    def _async(self, work, callback):
        if self.busy:
            return
        self.busy = True
        self.load_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.source_combo.configure(state="disabled")
        self.result_combo.configure(state="disabled")
        def task():
            try:
                self.queue.put((callback, work(), None))
            except Exception as exc:
                self.queue.put((callback, None, str(exc)))
        threading.Thread(target=task, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        try:
            callback, value, error = self.queue.get_nowait()
            while callback == "loading":
                self.status.set(value)
                callback, value, error = self.queue.get_nowait()
            self.busy = False
            self.load_button.configure(state="normal")
            self.source_combo.configure(state="readonly")
            self.result_combo.configure(state="readonly")
            if error:
                self.status.set(error)
            else:
                try:
                    callback(value)
                except Exception as exc:
                    self.status.set(str(exc))
            self.test_button.configure(state="normal" if self.scope and not self.dirty else "disabled")
            self._update_apply()
        except queue.Empty:
            pass
        self.poll_job = self.window.after(80, self._poll)

    def load_files(self):
        if self.busy:
            return
        source, result = self._selection("source"), self._selection("result")
        if not source or not result:
            self.status.set("원본 라벨과 결과의 폴더 또는 JSON 파일들을 선택하세요.")
            return
        if self.root_scope:
            try:
                self.profile = self._config()
                self.initial_pointers = (self._pointer(self.source_container), self._pointer(self.result_container))
            except (ValueError, TypeError, KeyError):
                pass
        self._mark_dirty()
        self.status.set("파일을 읽고 연결 후보를 찾고 있습니다…")
        def loaded(pair):
            if (source, result) != (self._selection("source"), self._selection("result")):
                self.status.set("선택 파일이 바뀌었습니다. 다시 샘플 비교하기를 누르세요.")
                return
            self.source_sample, self.result_sample = pair
            for sample, combo, variable, preferred in zip(pair, (self.source_combo, self.result_combo),
                                                         (self.source_container, self.result_container), self.initial_pointers):
                values = ["파일 전체" if path == "" else path for path in sample.containers]
                if self.profile and preferred and preferred not in values:
                    values.append(preferred)
                combo.configure(values=values)
                if not self.profile and preferred == "":
                    preferred = next((path for path in sample.containers if path in ("/records", "/data", "/items")), "")
                variable.set(preferred or "파일 전체")
                if variable.get() not in values:
                    variable.set(values[0])
            self.dirty = False
            self._reset_scope()
        source_pattern = self.profile["source"].get("glob", "**/*.json") if self.profile else None
        def work():
            original = load_samples(source, self.source_root, lambda message: self.queue.put(("loading", "원본: " + message, None)),
                                    lambda: self.closed, source_pattern)
            results = load_samples(result, progress=lambda message: self.queue.put(("loading", "결과: " + message, None)), cancel=lambda: self.closed)
            return original, results
        self._async(work, loaded)

    def _pointer(self, variable):
        return "" if variable.get() in ("최상위", "파일 전체") else variable.get()

    def _container_changed(self, event=None):
        self.initial_pointers = (self._pointer(self.source_container), self._pointer(self.result_container))
        self._reset_scope()

    def _restore_profile(self, config):
        self.profile = validate_config(copy.deepcopy(config))
        self.initial_pointers = (config["source"].get("records_path", ""), config["result"].get("records_path", ""))
        self.source_container.set(self.initial_pointers[0] or "파일 전체")
        self.result_container.set(self.initial_pointers[1] or "파일 전체")
        self.name.set(config["name"])
        self.root_scope = import_profile(config)
        self.scope = self.root_scope
        self.history = []
        self.selected = self.pending = None
        self.mode.set("value")
        self._invalidate()
        self._refresh()
        self.pages.select(1)
        self.status.set("저장된 연결을 불러왔습니다. 예시 파일을 넣으면 실제 값과 변경 전후도 확인할 수 있습니다. 기존 조건·변환·검증은 보존됩니다.")

    def _load_rule(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.window, title="AI / 저장된 규칙 JSON 불러오기", filetypes=[("규칙 JSON", "*.json")])
        if path:
            try:
                self._restore_profile(load_config(Path(path)))
                if self.source_sample and self.result_sample and not self.dirty:
                    self._reset_scope()
            except (ValueError, OSError) as exc:
                self.status.set(f"규칙을 불러오지 못했습니다: {exc}")

    def _reset_scope(self):
        if not self.source_sample or not self.result_sample or self.busy:
            return
        try:
            sp, rp = self._pointer(self.source_container), self._pointer(self.result_container)
            self.root_scope = (import_profile(self.profile, self.source_sample, self.result_sample, sp, rp) if self.profile else
                               Scope(self.source_sample.rows(sp), self.result_sample.rows(rp), self.source_sample.meta))
            self.scope = self.root_scope
            self.history = []
            self.pending = self.selected = None
            self.mode.set("value" if any(w.kind == "key" for w in self.scope.wires) else "key")
            self._invalidate()
            self.sample_info.set(f"원본 {self.source_sample.file_count:,}개 파일 / {len(self.scope.source_rows):,}건 · 결과 {self.result_sample.file_count:,}개 파일 / {len(self.scope.result_rows):,}건 읽음")
            self.status.set(self.sample_info.get() + f" · 필드는 최대 {SAMPLE_LIMIT}건에서 추출합니다.")
            self.test_button.configure(state="normal")
            self._refresh()
            self.pages.select(1)
        except (ValueError, TypeError, KeyError) as exc:
            self.dirty = True
            self.root_scope = import_profile(self.profile) if self.profile else None
            self.scope = self.root_scope
            self.history = []
            self.selected = self.pending = None
            self._invalidate()
            self.test_button.configure(state="disabled")
            self.status.set(str(exc))
            self._refresh() if self.scope else self._redraw()

    def _mode_changed(self):
        self.pending = None
        self._refresh() if self.scope else self._redraw()

    def _change_key(self):
        if self.busy or not self.scope:
            return
        self.mode.set("key")
        self.pending = None
        self._refresh()
        self.status.set("새 기준을 양쪽에서 연결하면 파란 선만 교체됩니다. 바꿀 값 연결은 유지됩니다. Esc로 취소할 수 있습니다.")

    def _add_guard(self):
        if not self.selected or self.selected.kind != "value" or self.busy:
            self.status.set("먼저 위 목록에서 ‘바꿀 값’ 연결을 선택하세요.")
            return
        self.guard_owner = self.selected
        self.details.set(False)
        self._toggle_details()
        self.mode.set("guard")
        self.pending = ("source", self.selected.source)
        self._refresh()
        self.status.set("오른쪽에서 플랫폼에 가져갔던 ‘수정 전 값’을 선택하세요. 최종 수정값은 초록 선으로 유지됩니다.")

    def _show_raw(self):
        try:
            config = self._config()
        except (ValueError, TypeError, KeyError):
            config = self.profile
        if config is None:
            self.status.set("같은 건을 찾는 기준과 바꿀 값을 먼저 연결하세요.")
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("고급 규칙 원문 · 조건·변환·검증 확인")
        dialog.geometry("860x620")
        dialog.transient(self.window.winfo_toplevel())
        box = scrolledtext.ScrolledText(dialog, wrap="none", font=("Consolas", 10))
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", json.dumps(config, ensure_ascii=False, indent=2))
        box.configure(state="disabled")
        ttk.Label(dialog, text="복합 조건·시간/참조 검증 등은 원문 그대로 유지됩니다. 직접 수정은 메인 화면의 ‘규칙 JSON’에서 합니다.").pack(padx=12, pady=8)

    def _review_step(self):
        self.pages.select(2)
        try:
            self._config()
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(f"연결을 먼저 완성해 주세요: {exc}")
            self.preview_box.configure(state="normal")
            self.preview_box.delete("1.0", "end")
            self.preview_box.insert("1.0", "② 필드 연결에서 같은 건을 찾는 기준과 반영할 수정값을 연결해 주세요.\n\n" + str(exc))
            self.preview_box.configure(state="disabled")
            self._update_apply()
            return
        if self.source_sample and self.result_sample and not self.dirty:
            self._test()
        else:
            self.preview_box.configure(state="normal")
            self.preview_box.delete("1.0", "end")
            self.preview_box.insert("1.0", "규칙의 연결 구조를 확인했습니다.\n\n자료를 선택하면 변경 전후 예시를 확인할 수 있습니다.\nAI 규칙 또는 저장된 규칙은 그대로 확정하고 전체 데이터로 검증할 수도 있습니다.\n\n아래 ‘규칙 확정 · 입력 확인’을 누르면 다음 단계로 이동합니다.\n규칙 확정은 데이터 저장이 아닙니다.")
            self.preview_box.configure(state="disabled")
            self.status.set("규칙을 확정하면 입력 확인으로 이동합니다. 실제 저장 전 전체 데이터를 검증합니다.")
        self._update_apply()

    def _visible(self):
        if not self.scope:
            return [], []
        # Completed connections are always present and ordered by creation.
        # Searches affect only unconnected candidates, never existing wires.
        lists = []
        query = self.query.get().casefold().strip()
        for side, fields in (("source", self.scope.source_fields), ("result", self.scope.result_fields)):
            connected = list(dict.fromkeys(getattr(wire, side) for wire in self.scope.wires))
            visible = [fields[path] for path in connected if path in fields]
            for path, item in fields.items():
                if path in connected:
                    continue
                if query and query not in (item.label + str(item.examples)).casefold():
                    continue
                visible.append(item)
            lists.append(visible)
        return tuple(lists)

    def _redraw(self):
        if self.closed:
            return
        self.canvas.delete("all")
        self.hits = []
        width = max(560, self.canvas.winfo_width())
        left, right = self._visible()
        gap = max(180, width * 0.17)
        card_width = (width - gap - 64) / 2
        x_left, x_right = 20, width - 20 - card_width
        positions = {}
        self.canvas.create_text(x_left, 18, anchor="w", text=f"원본 · 수정할 자리  {len(left)}", font=("맑은 고딕", 12, "bold"), fill="#233550")
        self.canvas.create_text(x_right, 18, anchor="w", text=f"작업 결과 · 가져올 값  {len(right)}", font=("맑은 고딕", 12, "bold"), fill="#233550")
        for side, fields, x in (("source", left, x_left), ("result", right, x_right)):
            for index, info in enumerate(fields):
                y = 44 + index * 74
                positions[(side, info.path)] = (x + card_width if side == "source" else x, y + 31)
        if self.scope:
            for index, wire in enumerate(self.scope.wires):
                a, b = positions.get(("source", wire.source)), positions.get(("result", wire.result))
                if a and b:
                    color = COLORS[wire.kind]
                    middle = (a[0] + b[0]) / 2
                    self.canvas.create_line(a[0], a[1], middle, a[1], middle, b[1], b[0], b[1],
                                            smooth=True, splinesteps=28, fill=color, width=4 if wire is self.selected else 2,
                                            dash=(5, 4) if wire.kind == "guard" else (), arrow="first" if wire.kind == "value" else "none",
                                            tags=(f"wire:{index}",))
                    self.canvas.create_text(middle, (a[1] + b[1]) / 2 - 9, text=KINDS[wire.kind], fill=color,
                                            font=("맑은 고딕", 9), tags=(f"wire:{index}",))
        for side, fields, x in (("source", left, x_left), ("result", right, x_right)):
            for index, info in enumerate(fields):
                y = 44 + index * 74
                selected = self.pending == (side, info.path)
                connected = self.scope and any((wire.source if side == "source" else wire.result) == info.path for wire in self.scope.wires)
                outline = COLORS[self.mode.get()] if selected else ("#b6d9d1" if connected else "#dce3ee")
                self.canvas.create_rectangle(x, y, x + card_width, y + 62, fill="#eef8ff" if selected else "#ffffff", outline=outline, width=2 if selected else 1)
                label = self._fit(info.label, self.card_font, card_width - 26, keep_end=True)
                self.canvas.create_text(x + 13, y + 16, text=label, anchor="w", fill="#233550", font=self.card_font)
                sample = TYPES.get(info.kind, info.kind) + " · " + compact(info.examples[0] if info.examples else "샘플 없음")
                sample = self._fit(sample, self.sample_font, card_width - 26)
                self.canvas.create_text(x + 13, y + 40, text=sample, anchor="w", fill="#728097", font=self.sample_font)
                port_x, port_y = positions[(side, info.path)]
                self.canvas.create_oval(port_x - 6, port_y - 6, port_x + 6, port_y + 6, fill=outline if selected else "#7790b1", outline="white", width=2)
                self.hits.append((side, info.path, (x - 8, y, x + card_width + 8, y + 62)))
        if not self.scope:
            self.canvas.create_text(width / 2, 140, text="원본 라벨과 작업 결과를 읽으면\n양쪽 필드를 연결할 수 있습니다.", justify="center", font=("맑은 고딕", 15), fill="#8694a8")
        elif not left or not right:
            self.canvas.create_text(width / 2, 140, text="검색어를 지우면 전체 필드가 보입니다.", justify="center", font=("맑은 고딕", 12), fill="#8694a8")
        self.positions = positions
        self.canvas.configure(scrollregion=(0, 0, width, max(300, 54 + max(len(left), len(right)) * 74)))

    def _hit(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        return next(((side, path) for side, path, (a, b, c, d) in self.hits if a <= x <= c and b <= y <= d), None)

    def _click(self, event):
        if self.busy:
            return
        self.canvas.focus_set()
        hit = self._hit(event)
        if hit:
            self._pick(hit)
            return
        for item in reversed(self.canvas.find_overlapping(self.canvas.canvasx(event.x) - 6, self.canvas.canvasy(event.y) - 6,
                                                         self.canvas.canvasx(event.x) + 6, self.canvas.canvasy(event.y) + 6)):
            tags = self.canvas.gettags(item)
            tag = next((tag for tag in tags if tag.startswith("wire:")), None)
            if tag:
                self.selected = self.scope.wires[int(tag.split(":")[1])]
                self.pending = None
                self._refresh()
                return
        self._cancel_pending()

    def _pick(self, hit):
        if self.dirty or not self.source_sample or not self.result_sample:
            self.status.set("연결을 추가하려면 ‘예시 파일 선택 / 변경’에서 실제 파일을 먼저 읽어 주세요.")
            return
        if self.pending and self.pending[0] != hit[0]:
            source = hit[1] if hit[0] == "source" else self.pending[1]
            result = hit[1] if hit[0] == "result" else self.pending[1]
            try:
                self.selected = self.scope.connect(source, result, self.mode.get(), self.guard_owner if self.mode.get() == "guard" else None)
                self.pending = None
                self.guard_owner = None
                self.mode.set("value")
                self._invalidate()
                self.status.set("목록을 연결했습니다. ‘선택한 목록의 안쪽 항목 연결’에서 각 항목을 이어 주세요." if self.selected.kind == "array" else "연결을 추가했습니다. 다음 바꿀 값을 연결하세요. 기존 연결은 위 목록과 그림에 유지됩니다.")
                self._refresh()
            except (ValueError, TypeError, KeyError) as exc:
                self.status.set(str(exc))
        else:
            self.pending = hit
            info = (self.scope.source_fields if hit[0] == "source" else self.scope.result_fields)[hit[1]]
            self.status.set(f"{info.label} · {TYPES.get(info.kind,info.kind)} · 예: {compact(info.examples, 170)} → 반대쪽 필드를 클릭하거나 드래그하세요.")
            self._redraw()

    def _drag(self, event):
        if self.pending and not self.busy:
            start = self.positions.get(self.pending)
            if start:
                self.canvas.delete("draft")
                self.canvas.create_line(*start, self.canvas.canvasx(event.x), self.canvas.canvasy(event.y),
                                        fill=COLORS[self.mode.get()], width=2, dash=(5, 3), tags="draft")

    def _release(self, event):
        self.canvas.delete("draft")
        if self.pending and not self.busy and not self.dirty:
            hit = self._hit(event)
            if hit and hit[0] != self.pending[0]:
                self._pick(hit)

    def _cancel_pending(self):
        self.pending = None
        self.mode.set("value" if self.scope and any(w.kind == "key" for w in self.scope.wires) else "key")
        self._refresh() if self.scope else self._redraw()

    def _refresh(self):
        if not self.scope:
            return
        self.breadcrumb.set("전체 데이터" + (" / " + " / ".join(wire.source for _, wire in self.history) if self.history else ""))
        self.back_button.configure(state="normal" if self.history else "disabled")
        self.wire_list.delete(*self.wire_list.get_children())
        for index, wire in enumerate(self.scope.wires):
            role = KINDS[wire.kind]
            if wire.raw_rule and wire.raw_rule.get("when"):
                role += " · 조건 있음"
            self.wire_list.insert("", "end", iid=str(index), values=(role, self.scope.source_fields[wire.source].label, self.scope.result_fields[wire.result].label))
        paths = ["항상 적용", "규칙의 조건 유지"] + [info.path for info in self.scope.result_fields.values() if info.kind not in ("array", "mixed", "null") and not info.path.startswith("@expression:")]
        self.condition_combo.configure(values=paths)
        if self.selected in self.scope.wires:
            self.wire_list.selection_set(str(self.scope.wires.index(self.selected)))
            self.source_transform.set(self.selected.source_transform)
            self.result_transform.set(self.selected.result_transform)
            self.nonempty.set(self.selected.nonempty)
            self.allow_missing.set(self.selected.allow_missing)
            self.condition_path.set(self.selected.condition_path or "항상 적용")
            value = self.selected.condition_value
            self.condition_value.set(value if isinstance(value, str) else (json.dumps(value) if value is not None else ""))
        else:
            self.selected = None
        kind = self.selected.kind if self.selected else None
        retained = bool(self.selected and (self.selected.raw_rule is not None or self.selected.original_keys))
        self.source_transform_combo.configure(values=(["규칙 설정 유지"] if retained else []) + list(TRANSFORMS))
        self.result_transform_combo.configure(values=(["규칙 설정 유지"] if retained else []) + list(TRANSFORMS))
        self.source_transform_combo.configure(state="readonly" if kind == "key" else "disabled")
        self.result_transform_combo.configure(state="readonly" if kind in ("key", "value", "guard") else "disabled")
        self.array_button.configure(state="normal" if kind == "array" else "disabled")
        count = sum(w.kind in ("value", "array") for w in self.scope.wires)
        self.step_hint.set(
            "먼저 같은 건을 찾을 값을 양쪽에서 선택하세요. 예: 고유번호 또는 원본 JSON 파일 이름" if self.mode.get() == "key" else
            "수정 전 값 대조: 오른쪽에서 플랫폼에 가져갔던 이전 값을 선택하세요." if self.mode.get() == "guard" else
            f"같은 건을 찾는 기준이 설정되었습니다. 이제 바꿀 값을 연결하세요. 현재 {count}개 연결됨")
        if not self.source_sample or not self.result_sample or self.dirty:
            self.step_hint.set("규칙의 연결 구조를 보고 있습니다. ‘예시 파일 선택 / 변경’에서 파일을 넣으면 실제 값과 함께 수정할 수 있습니다.")
        self._redraw()

    def _select_from_list(self, event=None):
        selected = self.wire_list.selection()
        if not self.scope or not selected:
            return
        index = int(selected[0])
        if index < len(self.scope.wires) and self.selected is not self.scope.wires[index]:
            self.selected = self.scope.wires[index]
            self.pending = None
            self._refresh()

    def _delete_wire(self):
        if self.selected and not self.busy:
            target = self.selected
            self.scope.wires[:] = [wire for wire in self.scope.wires if wire is not target and wire.owner is not target]
            self.selected = self.pending = None
            self.mode.set("value" if any(w.kind == "key" for w in self.scope.wires) else "key")
            self._invalidate()
            self._refresh()
            self.status.set("선택한 연결을 삭제했습니다. 나머지 연결은 유지됩니다.")

    def _enter_array(self):
        if not self.busy and self.selected and self.selected.kind == "array":
            self.history.append((self.scope, self.selected))
            self.scope = self.selected.child
            self.selected = self.pending = None
            self.mode.set("value" if any(w.kind == "key" for w in self.scope.wires) else "key")
            self.query.set("")
            self.canvas.yview_moveto(0)
            self._refresh()
            self.status.set("목록 안쪽입니다. 각 항목을 찾을 기준과 바꿀 값을 연결하세요. 상위 연결은 그대로 유지됩니다.")

    def _go_back(self):
        if self.history and not self.busy:
            self.scope, self.selected = self.history.pop()
            self.pending = None
            self.mode.set("value" if any(w.kind == "key" for w in self.scope.wires) else "key")
            self.query.set("")
            self.canvas.yview_moveto(0)
            self._refresh()

    def _save_options(self):
        if not self.selected or self.busy:
            return
        try:
            path = self.condition_path.get()
            value = None
            if path == "규칙의 조건 유지" and self.selected.raw_rule:
                value = self.selected.condition_value
            elif path not in ("항상 적용", "규칙의 조건 유지") and self.selected.kind in ("value", "array"):
                info = self.scope.result_fields[path]
                value = self.condition_value.get()
                if info.kind != "string":
                    value = json.loads(value)
                    if isinstance(value, (dict, list)) or value is None:
                        raise DataError("조건에는 문자·숫자·true/false 값을 입력하세요.")
            else:
                path = ""
            self.selected.source_transform = self.source_transform.get() if self.selected.kind == "key" else "그대로"
            if self.selected.kind != "array":
                self.selected.result_transform = self.result_transform.get()
            self.selected.nonempty = self.nonempty.get()
            self.selected.allow_missing = self.allow_missing.get()
            self.selected.condition_path = "" if path == "항상 적용" else path
            self.selected.condition_value = value
            self._invalidate()
            self.status.set("설정을 적용했습니다. ‘다음: 변경 전후 확인’으로 이동하세요.")
            self._refresh()
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(f"연결 설정 확인: {exc}")

    def _config(self):
        if not self.root_scope:
            raise DataError("예시 파일 또는 저장된 규칙을 먼저 불러오세요.")
        return compile_profile(self.root_scope, self.source_sample, self.result_sample,
                               self._pointer(self.source_container), self._pointer(self.result_container), self.name.get())

    def _test(self):
        if self.busy:
            return
        if self.dirty or not self.source_sample or not self.result_sample:
            self.status.set("실제 변경 전후를 보려면 1단계에서 원본 라벨과 작업 결과의 폴더 또는 파일들을 읽어 주세요.")
            return
        try:
            config = self._config()
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            return
        self.pages.select(2)
        self.status.set("같은 건끼리 연결하고 변경 전후를 검사하고 있습니다…")
        def complete(report):
            count = report["counts"]
            good = count.get("updated", 0) + count.get("unchanged", 0) + count.get("excluded", 0)
            self.last_checked = json.dumps(config, sort_keys=True, ensure_ascii=False) if good else None
            self._update_apply()
            self.status.set(f"예시 {report['sampled']}건 · 변경 {count.get('updated',0)} · 동일 {count.get('unchanged',0)} · 조건 제외 {count.get('excluded',0)} · 결과 없음 {count.get('no_result',0)} · 중복 {count.get('duplicate',0)} · 검토 {count.get('review_required',0)}")
            coverage = report["all_source_key_counts"]
            lines = [self.status.get(), f"원본 {self.source_sample.file_count:,}개 파일 / {report['source_loaded']:,}건 · 결과 {self.result_sample.file_count:,}개 파일 / {report['result_loaded']:,}건",
                     f"전체 원본 연결: 매칭 {coverage.get('matched',0):,} · 결과 없음 {coverage.get('no_result',0):,} · 중복 관련 {coverage.get('duplicate',0):,} · 키 오류 {coverage.get('invalid_source',0):,}",
                     f"전체 결과의 중복 키 관련 행: {report['duplicate_result_rows']:,}건",
                     f"전체 연결키 중복을 검사하며 변경 전후 검사는 원본 최대 {SAMPLE_LIMIT}건입니다.",
                     f"결과의 연결 기준값 해석 오류: {report['invalid_result_keys']}건", ""]
            if not good:
                lines.append("성공적으로 연결된 예시가 없습니다. 연결 기준과 실제 필드를 확인하세요.")
            for example in report["examples"]:
                if example.get("source_file"):
                    lines.append("원본 파일: " + example["source_file"])
                lines.append(f"같은 건 찾기: {example['id']}")
                for change in example["changes"]:
                    lines.extend([change["path"], "  이전: " + compact(change["before"], 500), "  이후: " + compact(change["after"], 500)])
                lines.append("")
            lines.extend("검토: " + error for error in report["errors"])
            lines.append("전체 원본 폴더와 모든 결과 파일은 규칙 사용 후 메인 화면의 검증 미리보기로 검사합니다.")
            self.preview_box.configure(state="normal")
            self.preview_box.delete("1.0", "end")
            self.preview_box.insert("1.0", "\n".join(lines))
            self.preview_box.configure(state="disabled")
        self._async(lambda: preview_profile(config, self.root_scope.source_rows, self.root_scope.result_rows, self.source_sample.meta), complete)

    def _apply(self):
        try:
            if self.busy:
                raise DataError("자료 읽기 또는 예시 검사가 끝날 때까지 기다려 주세요.")
            config = self._config()
            def selection(sample):
                if sample is None or self.dirty:
                    return ""
                return getattr(sample, "selection", sample.path)
            self.on_apply(config, selection(self.source_sample), selection(self.result_sample))
            self.applied = True
            self._close()
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(str(exc))

    def _close(self):
        if self.closed:
            return
        self.closed = True
        for job in (self.poll_job, self.load_job):
            if job:
                self.window.after_cancel(job)
        self.window.destroy()
        if not self.applied and self.on_cancel:
            self.on_cancel()
