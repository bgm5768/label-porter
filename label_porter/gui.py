"""Guided desktop workspace. Porting and file operations live in workflow.py."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, scrolledtext, ttk

from . import __version__
from .config import load_config, validate_config
from .rule_review import RuleReview, profile_connections
from .reports import STATUS, ISSUES
from .ui import INK, MUTED, ScrollPage, card, setup_style
from .workflow import Workflow, resource_directory, AI_GUIDE_FILENAME


class App(Workflow):
    def __init__(self, root):
        self.root = root
        root.title(f"라벨 포터 {__version__} · 원본을 보존하는 라벨 반영")
        width, height = min(1320, root.winfo_screenwidth()-60), min(900, root.winfo_screenheight()-100)
        root.geometry(f"{width}x{height}")
        root.minsize(min(1040, width), min(680, height))
        setup_style(root)
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = self.rule_ready = False
        self.preview_fingerprint = self.active_fingerprint = None
        self.review_step = 3
        self.connection_confirmed = False
        self.mapping = None
        self.last_run = self.last_summary = None
        self.result_paths = []
        self.current_step, self.route = 0, "workflow"
        self.help_return = None
        self.source, self.output = tk.StringVar(), tk.StringVar()
        self.split_output = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="규칙 JSON 파일을 불러오면 입력·저장 위치 선택으로 이동합니다.")
        self.rule_file = tk.StringVar(value="아직 불러온 파일이 없습니다.")
        self.rule_summary = tk.StringVar(value="아직 프로젝트 규칙이 없습니다.")
        self.rule_title = tk.StringVar(value="규칙을 준비해 주세요")
        self.run_summary, self.profile_name, self.description = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.input_hint = tk.StringVar(value="실제로 처리할 전체 원본 폴더와 결과 파일을 지정하세요.")
        self.result_count = tk.StringVar(value="선택한 결과 파일 없음")
        self.profiles = {c["name"]: (p, c) for p in sorted((resource_directory()/"profiles").glob("*.json")) for c in [load_config(p)]}
        self.settings_path = Path(os.environ.get("APPDATA", str(Path.home()))) / "LabelPorter" / "settings.json"
        self.ai_guide_text = (resource_directory()/AI_GUIDE_FILENAME).read_text(encoding="utf-8")
        self.help_text = (resource_directory()/"사용설명.md").read_text(encoding="utf-8")
        self.capabilities_text = (resource_directory()/"후처리_지원범위.md").read_text(encoding="utf-8")
        self._config_text = ""
        self._build()
        self._load_settings()
        for var in (self.source, self.output, self.split_output):
            var.trace_add("write", lambda *_: self._invalidate_preview())
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._show_step(0)
        self.poll_job = root.after(100, self._poll)

    def _build(self):
        self.toolbar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(16, 8))
        self.toolbar.pack(side="top", fill="x")
        self.viewer_button = ttk.Button(self.toolbar, text="JSON 뷰어", command=self._json_viewer, style="Toolbar.TButton")
        self.viewer_button.pack(side="left")
        ttk.Separator(self.root).pack(side="top", fill="x")
        workspace = ttk.Frame(self.root)
        workspace.pack(fill="both", expand=True)
        menu_font = font.Font(self.root, family="맑은 고딕", size=11, weight="bold")
        sidebar = tk.Frame(workspace, bg="#edf0f4", width=max(200, menu_font.measure("4   검증·복사본 저장")+46))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="라벨 포터", bg="#edf0f4", fg=INK, font=("맑은 고딕", 19, "bold")).pack(anchor="w", padx=22, pady=(28, 4))
        tk.Label(sidebar, text="규칙 → 데이터 → 확인 → 저장", bg="#edf0f4", fg=MUTED, font=("맑은 고딕", 9)).pack(anchor="w", padx=22, pady=(0, 30))
        self.nav_buttons = []
        for i, title in enumerate(("1   규칙 불러오기", "2   입력·저장 위치", "3   필드 연결 확인", "4   검증·복사본 저장")):
            b = tk.Button(sidebar, text=title, anchor="w", padx=12, pady=16, bd=0,
                          bg="#edf0f4", fg=MUTED, activebackground="#e0e5ec", activeforeground=INK,
                          font=("맑은 고딕", 11, "bold"), command=lambda i=i: self._navigate(i))
            b.pack(fill="x", padx=10, pady=3)
            self.nav_buttons.append(b)
        tk.Label(sidebar, text=f"원본 보존 · 누락 항목 유지\n\nv{__version__}", justify="left", bg="#edf0f4", fg=MUTED, font=("맑은 고딕", 9)).pack(side="bottom", anchor="w", padx=22, pady=24)
        self.help_button = tk.Button(sidebar, text="사용 방법", anchor="w", padx=12, pady=12, bd=0,
                                     bg="#edf0f4", fg=MUTED, activebackground="#e0e5ec", activeforeground=INK,
                                     font=("맑은 고딕", 11), command=lambda: self._show_tools(1))
        self.help_button.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        shell = ttk.Frame(workspace, padding=(28, 24, 28, 16))
        shell.pack(side="left", fill="both", expand=True)
        self.page_title, self.page_caption = tk.StringVar(), tk.StringVar()
        ttk.Label(shell, textvariable=self.page_title, style="Title.TLabel").pack(anchor="w")
        caption = ttk.Label(shell, textvariable=self.page_caption, style="Muted.TLabel", wraplength=800)
        caption.pack(fill="x", pady=(6, 18))
        shell.bind("<Configure>", lambda e: caption.configure(wraplength=max(300, e.width-48)))
        # Allocate navigation first so it stays visible at small window sizes and high DPI.
        self.footer = ttk.Frame(shell, padding=(0, 12, 0, 0))
        self.footer.pack(side="bottom", fill="x")
        ttk.Separator(self.footer).pack(fill="x", pady=(0, 10))
        self.step_indicator = tk.StringVar()
        status_row = ttk.Frame(self.footer); status_row.pack(fill="x", pady=(0, 10))
        ttk.Label(status_row, textvariable=self.step_indicator, style="Muted.TLabel").pack(side="right", padx=(12, 0))
        status = ttk.Label(status_row, textvariable=self.status, wraplength=700, style="Muted.TLabel")
        status.pack(side="left", fill="x", expand=True)
        self.footer.bind("<Configure>", lambda e: status.configure(wraplength=max(280, e.width-100)))
        actions = ttk.Frame(self.footer)
        self.footer_actions = actions
        actions.pack(fill="x")
        self.navigation = ttk.Frame(actions)
        self.navigation.pack(side="right")
        self.back_button = ttk.Button(self.navigation, command=self._back, style="Nav.TButton", width=20)
        self.back_button.grid(row=0, column=0, padx=(0, 8))
        self.primary_button = ttk.Button(self.navigation, style="NavPrimary.TButton", command=self._primary, width=20)
        self.primary_button.grid(row=0, column=1)
        self.navigation.columnconfigure(1, minsize=self.primary_button.winfo_reqwidth())
        self.cancel_button = ttk.Button(actions, text="작업 중단", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self.content = ttk.Frame(shell)
        self.content.pack(fill="both", expand=True)
        self.steps = ttk.Notebook(self.content, style="Flow.TNotebook")
        self.steps.pack(fill="both", expand=True)
        self.step_pages = [ScrollPage(self.steps) for _ in range(4)]
        for i, page in enumerate(self.step_pages):
            self.steps.add(page, text=str(i+1), state="normal" if i == 0 else "disabled")
        project, inputs, _, review = [p.body for p in self.step_pages]
        group = card(project, "프로젝트 규칙", "AI가 만든 규칙 JSON 파일을 불러오세요. 다음 단계에서 실제 처리할 데이터와 저장 위치를 선택합니다.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x", pady=(2, 12))
        self.import_rule_button = ttk.Button(row, text="규칙 파일 불러오기…", command=self._load_profile, style="Primary.TButton")
        self.import_rule_button.pack(side="left")
        ttk.Label(group, textvariable=self.rule_file, style="CardMuted.TLabel", wraplength=700).pack(fill="x")
        group = card(project, "불러온 규칙")
        ttk.Label(group, textvariable=self.rule_title, style="CardTitle.TLabel").pack(anchor="w", pady=(8, 8))
        ttk.Label(group, textvariable=self.rule_summary, style="CardMuted.TLabel", wraplength=720).pack(fill="x")
        self.save_rule_button = ttk.Button(group, text="규칙 내보내기", command=self._save_profile, state="disabled")
        self.save_rule_button.pack(anchor="w", pady=(14, 0))
        group = card(project, "이후 진행 순서")
        for title, explanation in (("02  데이터 선택", "원본 폴더 · 플랫폼/보완 파일 · 저장 위치를 한 번 선택합니다."),
                                   ("03  연결과 프리뷰 확인", "같은 KEY의 데이터 예시를 자동으로 찾아 연결과 실제 값을 보여줍니다."),
                                   ("04  전체 검증과 저장", "전체 처리 결과서를 확인한 뒤 원본을 보존하는 복사본을 저장합니다.")):
            ttk.Label(group, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(10, 3))
            ttk.Label(group, text=explanation, style="CardMuted.TLabel", wraplength=700).pack(fill="x")
        ttk.Label(project, text="규칙 작성이 필요하면 왼쪽 아래 ‘사용 방법 → AI 규칙 작성 가이드’를 이용하세요. 불러온 규칙은 읽기 전용으로 확인합니다.", style="Muted.TLabel", wraplength=720).pack(fill="x", pady=4)
        ttk.Label(inputs, textvariable=self.input_hint, wraplength=760, foreground="#245eb7").pack(fill="x", pady=(0, 14))
        group = card(inputs, "1. 보완 전 원본 라벨 폴더", "여러 개별 JSON 또는 여러 건을 담은 통합 JSON 모두 가능합니다. 하위 폴더도 포함합니다.")
        self._path_row(group, self.source, self._choose_source)
        group = card(inputs, "2. 플랫폼 결과 또는 별도 보완 데이터", "같은 프로젝트의 보완·검수 결과 JSON / CSV를 선택하세요.")
        ttk.Label(group, textvariable=self.result_count, style="CardMuted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x")
        tools = ttk.Frame(row, style="Card.TFrame"); tools.pack(side="right", padx=(10, 0))
        ttk.Button(tools, text="파일 추가", command=self._choose_results).pack(fill="x")
        ttk.Button(tools, text="선택 제거", command=self._remove_results).pack(fill="x", pady=(4, 0))
        ttk.Button(tools, text="JSON 항목 수 보기", command=self._json_viewer).pack(fill="x", pady=(4, 0))
        self.result_list = tk.Listbox(row, height=4, selectmode="extended", relief="flat", highlightthickness=1,
                                     highlightbackground="#dce3ee", background="#f8faff", foreground=INK, selectbackground="#dbeafe", selectforeground=INK)
        scroll = ttk.Scrollbar(row, command=self.result_list.yview); scroll.pack(side="right", fill="y")
        self.result_list.configure(yscrollcommand=scroll.set); self.result_list.pack(fill="x", expand=True)
        group = card(inputs, "3. 새 결과를 저장할 폴더", "반영 결과는 날짜 / 001, 002… 폴더에 저장합니다. 원본 폴더 밖을 선택하세요.")
        self._path_row(group, self.output, self._choose_output)
        ttk.Checkbutton(inputs, text="정상 데이터도 변경 / 미변경으로 분리 · 검토·짝 없음·조건 제외·파일 오류는 분류별 examine에 저장", variable=self.split_output).pack(anchor="w", pady=(0, 12))
        group = card(review, "이번 작업", "전체 데이터 검증을 마쳐야 복사본을 저장할 수 있습니다.")
        ttk.Label(group, textvariable=self.run_summary, style="CardMuted.TLabel", wraplength=760).pack(fill="x")
        self.metric_values = {}
        metrics = ttk.Frame(review); metrics.pack(fill="x", pady=(0, 14))
        for i, (key, title) in enumerate((("source", "원본 레코드"), ("result", "보완 레코드"), ("completed", "처리 완료 / 통과"), ("fields", "변경 필드"), ("changed", "변경"), ("same", "변경 없음"), ("missing", "짝이 없는 데이터"), ("review", "검토 / 오류"))):
            metrics.columnconfigure(i % 4, weight=1, uniform="metric")
            box = ttk.Frame(metrics, style="Card.TFrame", padding=12)
            box.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 7), pady=(0, 7))
            ttk.Label(box, text=title, style="CardMuted.TLabel").pack(anchor="w")
            var = self.metric_values[key] = tk.StringVar(value="—")
            ttk.Label(box, textvariable=var, style="CardTitle.TLabel", font=("맑은 고딕", 21, "bold")).pack(anchor="w", pady=(8, 0))
        self.review_note = tk.StringVar(value="검증 전입니다. ‘전체 데이터 검증’을 눌러 주세요.")
        ttk.Label(review, textvariable=self.review_note, wraplength=760).pack(fill="x", pady=(0, 10))
        self.progress = ttk.Progressbar(review, mode="determinate"); self.progress.pack(fill="x", pady=(0, 12))
        self.status_table = self._table(review, (("구분", 100), ("처리 결과", 420), ("건수", 90)), 5)
        links = ttk.Frame(review); links.pack(fill="x", pady=(0, 12))
        self.preview_button = ttk.Button(links, text="전체 검증 다시 하기", command=lambda: self._start(True)); self.preview_button.pack(side="left")
        self.report_button = ttk.Button(self.footer_actions, text="처리 결과서 열기", command=self._open_report, state="disabled")
        self.folder_button = ttk.Button(self.footer_actions, text="결과 폴더", command=self._open_folder, state="disabled")
        # Execution code can disable this action, while the footer provides the current label.
        self.run_button = self.primary_button
        ttk.Label(review, text="진행 기록", style="Muted.TLabel").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(review, wrap="word", height=5, font=("맑은 고딕", 9), state="disabled", relief="flat", foreground=MUTED)
        self.log.pack(fill="x", pady=(6, 0))
        self.mapping_host = ttk.Frame(self.content)
        self.help_host = ttk.Frame(self.content)
        self.tabs = ttk.Notebook(self.help_host); self.tabs.pack(fill="both", expand=True)
        ai, help_page, capabilities = [ttk.Frame(self.tabs, padding=12) for _ in range(3)]
        self.tabs.add(ai, text="AI 규칙 작성 가이드"); self.tabs.add(help_page, text="사용 방법")
        self.tabs.add(capabilities, text="후처리 지원 범위")
        row = ttk.Frame(ai); row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="가이드 복사", command=self._copy_ai_guide).pack(side="left")
        ttk.Button(row, text="MD 파일 저장", command=self._save_ai_guide).pack(side="left", padx=8)
        self.ai_guide_box = self._document_view(ai, self.ai_guide_text)
        ttk.Button(help_page, text="처리 가능한 업무와 제한 보기 →", command=lambda: self.tabs.select(2)).pack(anchor="w", pady=(0, 10))
        self.help_box = self._document_view(help_page, self.help_text)
        self.capabilities_box = self._document_view(capabilities, self.capabilities_text)

    @staticmethod
    def _table(parent, columns, height):
        table = ttk.Treeview(parent, columns=list(range(len(columns))), show="headings", height=height)
        for i, (title, width) in enumerate(columns):
            table.heading(i, text=title); table.column(i, width=width, minwidth=60)
        table.pack(fill="x", pady=12)
        return table

    def _json_viewer(self):
        from .json_viewer import JsonViewer
        selected = self.result_list.curselection()
        initial = self.result_paths[selected[0] if selected else 0] if self.result_paths else None
        JsonViewer(self.root, initial)

    @staticmethod
    def _path_row(parent, variable, command):
        row = ttk.Frame(parent, style="Card.TFrame"); row.pack(fill="x")
        ttk.Button(row, text="폴더 선택", command=command).pack(side="right", padx=(10, 0))
        ttk.Entry(row, textvariable=variable).pack(fill="x", expand=True)

    def _close_mapping(self):
        if self.mapping and not self.mapping.closed:
            self.mapping._close(notify=False)
        self.mapping = None

    def _show_step(self, index):
        if index == 2:
            self._wizard()
            return
        self.route, self.current_step = "workflow", index
        self.help_host.pack_forget(); self.help_return = None
        self.mapping_host.pack_forget()
        self.steps.pack(fill="both", expand=True); self.steps.tab(index, state="normal"); self.steps.select(index)
        self.footer.pack(side="bottom", fill="x", before=self.content)
        self.page_title.set(("프로젝트 규칙 불러오기", "입력 데이터와 저장 위치", "필드 연결 확인", "전체 검증과 복사본 저장")[index])
        self.page_caption.set(("규칙 파일을 불러오면 데이터 선택 단계로 이어집니다.",
                               "여기서 선택한 데이터를 다음 단계의 프리뷰와 전체 처리에 함께 사용합니다.", "",
                               "전체 데이터를 검증하고 처리 결과서를 확인한 뒤 복사본을 저장하세요.")[index])
        self._sync_actions()

    def _sync_actions(self):
        helping = self.route == "help"
        self.back_button.configure(text="← 작업으로 돌아가기" if helping else "← 이전 단계",
                                   state="disabled" if self.running or (self.current_step == 0 and not helping) else "normal")
        self.cancel_button.configure(state="normal" if self.running and not self.cancel_event.is_set() else "disabled")
        for button in (self.cancel_button, self.report_button, self.folder_button): button.pack_forget()
        for i, button in enumerate(self.nav_buttons):
            active = i == self.current_step and not helping
            button.configure(state="disabled" if self.running or (i > 0 and not self.rule_ready) else "normal",
                             bg="#dfe5ed" if active else "#edf0f4", fg=INK if active else MUTED)
        self.help_button.configure(state="disabled" if self.running else "normal", bg="#dfe5ed" if helping else "#edf0f4", fg=INK if helping else MUTED)
        self.step_indicator.set("도움말" if helping else f"{self.current_step + 1} / 4 단계")
        if helping:
            self.primary_button.grid_remove()
            return
        self.primary_button.grid()
        if self.running: self.cancel_button.pack(side="left", padx=(0, 8))
        if self.current_step == self.review_step:
            self.report_button.pack(side="left", padx=(0, 8)); self.folder_button.pack(side="left")
        title = "다음 단계 →" if self.current_step < self.review_step else "복사본 저장" if self.preview_fingerprint else "전체 데이터 검증"
        if self.current_step == self.review_step and self._saved(): title = "결과 폴더 열기"
        self.primary_button.configure(text=title, state="normal" if not self.running and self.rule_ready else "disabled")

    def _saved(self):
        return self.last_summary and not self.last_summary.get("preview") and self.last_summary["status"].startswith("completed")

    def _navigate(self, index):
        if self.running: return
        if self.route == "help": self._return_from_help()
        if index == self.current_step and (index != self.review_step or self.connection_confirmed): return
        if index == 0: self._show_step(0)
        elif index == 1: self._advance_inputs()
        elif index == 2: self._wizard()
        elif self.route == "mapping" and self.mapping:
            self.mapping.apply_button.invoke()
        else: self._advance_review()

    def _primary(self):
        if self.running or self.route == "help": return
        if self.current_step == 0: self._advance_inputs()
        elif self.current_step == 1: self._wizard()
        elif self.current_step == 2 and self.mapping: self.mapping._apply()
        elif self._saved(): self._open_folder()
        else: self._start(self.preview_fingerprint is None)

    def _back(self):
        if self.running: return
        if self.route == "help": self._return_from_help()
        else: self._show_step(max(0, self.current_step - 1))

    def _show_tools(self, tab=1):
        if self.running: return
        if self.route != "help":
            self.help_return = (self.route, self.page_title.get(), self.page_caption.get(), self.status.get())
        self.steps.pack_forget(); self.mapping_host.pack_forget()
        self.help_host.pack(fill="both", expand=True)
        self.route = "help"
        self.tabs.select(tab)
        self.page_title.set("사용 방법")
        self.page_caption.set("진행 순서와 AI 규칙 작성 가이드를 확인하세요. 돌아가면 보던 단계에서 계속할 수 있습니다.")
        self.status.set("작업의 입력값과 프리뷰 상태를 유지하고 있습니다.")
        self._sync_actions()

    def _return_from_help(self):
        if self.route != "help" or not self.help_return: return
        self.help_host.pack_forget()
        self.route, title, caption, status = self.help_return
        self.help_return = None
        (self.mapping_host if self.route == "mapping" else self.steps).pack(fill="both", expand=True)
        self.page_title.set(title); self.page_caption.set(caption); self.status.set(status)
        self._sync_actions()

    def _set_config(self, config):
        config = validate_config(config)
        self._config_text = json.dumps(config, ensure_ascii=False, indent=2)
        self.profile_name.set(config["name"]); self.description.set(config.get("description", ""))
        self._invalidate_rule(); self._summarize_rule(config)
        self.save_rule_button.configure(state="normal")
        self._sync_actions()
        if self.route == "workflow":
            self._show_step(0)

    def _get_config(self):
        if not self._config_text: raise ValueError("프로젝트 규칙을 먼저 만들거나 불러오세요.")
        return validate_config(json.loads(self._config_text))

    def _summarize_rule(self, config):
        self.rule_title.set(config["name"])
        checks = sum(len(config.get(k, [])) for k in ("include_when", "checks", "assertions"))
        connections = profile_connections(config)
        self.rule_summary.set(f"수정 필드 {sum(r['kind']=='field' for r in connections)}개 · 목록 {sum(r['kind'] in ('items', 'rebuild_items') for r in connections)}개 · 레코드 조건 / 검증 {checks}개\n원본 대상: {config['source'].get('glob', '**/*.json')}\n데이터를 선택한 뒤 3단계에서 필드 연결과 실제 예시를 확인합니다.")

    def _invalidate_preview(self):
        if self.mapping and getattr(self, "mapping_inputs", None) != self._mapping_inputs():
            self._close_mapping()
        self.connection_confirmed = False
        self.preview_fingerprint = self.last_summary = None
        for var in self.metric_values.values(): var.set("—")
        self.status_table.delete(*self.status_table.get_children())
        self.review_note.set("현재 설정으로 전체 데이터를 검증해 주세요.")
        self.report_button.configure(state="disabled"); self.folder_button.configure(state="disabled")
        self._sync_actions()

    def _invalidate_rule(self):
        self.rule_ready = bool(self._config_text); self._invalidate_preview()
        self.steps.tab(1, state="normal" if self.rule_ready else "disabled")
        self.steps.tab(2, state="disabled"); self.steps.tab(3, state="disabled")

    def _advance_inputs(self):
        if self.running: return
        try:
            self._summarize_rule(self._get_config()); self.rule_ready = True
            self.steps.tab(1, state="normal"); self._show_step(1)
            self.status.set("규칙을 불러왔습니다. 원본 폴더·보완 파일·저장 위치를 선택하세요."); self._save_settings()
        except (ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 확인: {exc}")

    def _advance_review(self):
        if self.running: return
        if not self.connection_confirmed:
            self._wizard(); return
        try:
            self._check_inputs()
            pattern = self._get_config()["source"].get("glob", "**/*.json")
            if not any(p.is_file() for p in Path(self.source.get()).glob(pattern)):
                raise ValueError(f"원본 폴더에서 대상 패턴 ‘{pattern}’에 맞는 파일을 찾지 못했습니다.")
            self.run_summary.set(f"규칙  {self._get_config()['name']}\n원본  {self.source.get()}\n결과  {len(self.result_paths):,}개 파일\n저장  {self.output.get()}\n분리  {'변경 / 미변경 / examine' if self.split_output.get() else '전체 복사본 + examine'}")
            self.steps.tab(3, state="normal"); self._show_step(3)
            self.status.set("전체 데이터 검증 후 집계와 보고서를 확인하세요."); self._save_settings()
        except (ValueError, OSError) as exc: self.status.set(f"입력 확인: {exc}")

    def _load_profile(self):
        if self.running: return
        path = filedialog.askopenfilename(title="AI / 저장된 프로젝트 규칙", filetypes=[("규칙 JSON", "*.json")], parent=self.root)
        if path:
            try:
                config = load_config(Path(path))
                self._close_mapping()
                self.source.set(""); self.output.set(""); self.result_paths = []; self._render_results()
                self._set_config(config)
                self.rule_file.set(str(Path(path)))
                self._advance_inputs()
            except (OSError, ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 불러오기: {exc}")

    def _save_profile(self):
        try:
            config = self._get_config()
            path = filedialog.asksaveasfilename(title="규칙 내보내기", defaultextension=".json", initialfile="project_profile.json", filetypes=[("JSON", "*.json")], parent=self.root)
            if path:
                Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); self.status.set(f"규칙 저장 완료: {path}")
        except (OSError, ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 저장: {exc}")

    def _render_results(self):
        super()._render_results(); self.result_count.set(f"{len(self.result_paths):,}개 결과 파일 선택")

    def _wizard(self):
        if self.running: return
        reuse = self.mapping and not self.mapping.closed and self.mapping_inputs == self._mapping_inputs()
        try:
            config = self._get_config()
            self._check_inputs()
            if not reuse and not any(p.is_file() for p in Path(self.source.get()).glob(config["source"].get("glob", "**/*.json"))):
                raise ValueError("규칙의 파일 패턴에 맞는 원본 JSON이 없습니다.")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if self.rule_ready: self._show_step(1)
            self.status.set(f"입력 확인: {exc}"); return
        self._save_settings()
        self.current_step, self.route = 2, "mapping"
        self.steps.tab(2, state="normal"); self.steps.select(2); self.steps.pack_forget()
        self.mapping_host.pack(fill="both", expand=True)
        self.page_title.set("필드 연결과 실제 값 확인")
        self.page_caption.set("전체 입력을 검색하고 레코드를 선택하여 연결값과 적용 후 JSON을 확인하세요.")
        self.status.set("프리뷰는 세션에 유지됩니다. 외부에서 파일을 수정했다면 ‘데이터 새로 읽기’를 누르세요. 실제 저장 전 전체 검증을 진행합니다.")
        self._sync_actions()
        if not reuse:
            self._close_mapping()
            self.mapping_inputs = self._mapping_inputs()
            self.mapping = RuleReview(self.mapping_host, config, self._finish_mapping,
                                      lambda: self._show_step(1), source_hint=self.source.get(),
                                      result_hints=self.result_paths, configured=True, external_navigation=True)

    def _mapping_inputs(self):
        return (self._config_text, self.source.get(), tuple(self.result_paths))

    def _finish_mapping(self):
        self.connection_confirmed = True
        self._advance_review()

    def _fingerprint(self):
        base = super()._fingerprint()
        source = Path(self.source.get()).resolve()
        stats = [(p.relative_to(source).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(source.rglob("*")) if p.is_file()]
        return json.dumps([base, stats], ensure_ascii=False)

    def _start(self, preview):
        if self.running: return
        if not preview:
            try:
                if self.preview_fingerprint != self._fingerprint():
                    self._invalidate_preview(); self.status.set("규칙 또는 입력 파일이 바뀌었습니다. 전체 데이터 검증을 다시 실행하세요."); return
            except (OSError, ValueError) as exc: self.status.set(str(exc)); return
        self.last_summary = None
        if not self.connection_confirmed:
            self._wizard(); return
        super()._start(preview)
        self._sync_actions()

    def _show_summary(self, summary):
        counts = summary.get("source_status_counts", {})
        values = {"changed": counts.get("planned_update", 0)+counts.get("updated", 0), "same": counts.get("unchanged", 0),
                  "missing": summary.get("unmatched_records", counts.get("no_result", 0) + summary.get("result_issue_counts", {}).get("orphan_result", 0)),
                  "review": summary.get("issue_rows", sum(counts.get(k, 0) for k in ISSUES)),
                  "source": summary.get("source_records", 0), "result": summary.get("result_records", 0),
                  "completed": summary.get("completed_records", counts.get("updated",0)+counts.get("planned_update",0)+counts.get("unchanged",0)),
                  "fields": summary.get("changed_fields", 0)}
        for key, value in values.items(): self.metric_values[key].set(f"{value:,}")
        self.status_table.delete(*self.status_table.get_children())
        for scope, key in (("원본 레코드", "source_status_counts"), ("결과 항목", "result_issue_counts"), ("원본 파일", "source_file_issue_counts")):
            for status, count in summary.get(key, {}).items(): self.status_table.insert("", "end", values=(scope, STATUS.get(status, status), f"{count:,}"))
        text = f"원본 {summary.get('source_records', 0):,}건 · 결과 {summary.get('result_records', 0):,}건"
        p = summary.get("partition_records", {})
        if p: text += f"\n분리 {'예정' if summary['preview'] else '결과'}: 변경 {p.get('changed',0):,}건 / 미변경 {p.get('unchanged',0):,}건 / examine {p.get('examine',0):,}건"
        text += "\nexamine에서 짝 없음·검토/오류·조건 제외·파일 오류를 폴더별로 확인하세요. 처리 결과서에서 모든 분류를 검색할 수 있습니다."
        if summary.get("advisory_rows"):
            text += f"\n어절 등 반영 {summary['advisory_rows']:,}건에서 연결 불확실 태그 {summary.get('held_references',0):,}개를 별도 보관했습니다."
        if summary["status"] in ("failed", "cancelled"): text += "\n작업이 완료되지 않았습니다. 부분 결과를 완성본으로 사용하지 마세요."
        elif summary["status"] == "completed_with_issues": text += "\n전체 반영을 보류한 항목은 원본을 보존합니다. 별도 보관 태그와 보류 사유는 상세 보고서에서 확인하세요."
        self.review_note.set(text)

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self.status.set(payload["message"])
                    self.progress.configure(value=100*payload["done"]/payload["total"] if payload["total"] else 0)
                    self._append_log(payload["message"]); continue
                self.running = False; self.preview_button.configure(state="normal")

                for i in range(4): self.steps.tab(i, state="normal")
                if kind == "error":
                    self.status.set("실행하지 못했습니다: "+payload); self.review_note.set("실행 실패 · 입력과 규칙을 확인하고 다시 검증하세요."); self._append_log(payload)
                else:
                    self.last_run = payload.directory; summary = self.last_summary = payload.summary
                    self.report_button.configure(state="normal"); self.folder_button.configure(state="normal")
                    if summary.get("preview") and summary["status"].startswith("completed"): self.preview_fingerprint = self.active_fingerprint
                    self.progress.configure(value=100 if summary["status"].startswith("completed") else 0)
                    self._show_summary(summary)
                    self._append_log(f"보고서: {payload.directory/'reports'/'report.html'}")
                    self.status.set({"completed": "검증 완료. 집계 확인 후 ‘복사본 저장’을 누르세요." if summary["preview"] else "복사본 저장 완료. 결과 폴더를 확인하세요.", "completed_with_issues": "완료 · 검토 항목이 있습니다. 보고서에서 사유를 확인하세요.", "failed": "실행 실패 · 보고서의 오류를 확인하세요.", "cancelled": "중단했습니다. 원본은 변경하지 않았습니다."}[summary["status"]])
                    if summary.get("error"): self._append_log(summary["error"])
                self._sync_actions()
        except queue.Empty: pass
        self.poll_job = self.root.after(100, self._poll)

    def _load_settings(self):
        try:
            s = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.source.set(s.get("source", "")); self.output.set(s.get("output", "")); self.split_output.set(s.get("split_output", True))
            self.result_paths = [p for p in s.get("results", []) if Path(p).is_file()]; self._render_results()
            if s.get("config"): self._set_config(s["config"])
            elif s.get("profile") in self.profiles: self._set_config(self.profiles[s["profile"]][1])
            self.rule_file.set(s.get("rule_file", "최근 저장한 프로젝트 규칙"))
            if self._config_text: self.status.set("최근 작업의 규칙과 입력 위치를 복원했습니다. 확인 후 계속하세요.")
        except (OSError, ValueError, TypeError, KeyError): pass

    def _save_settings(self):
        try:
            s = {"source": self.source.get(), "output": self.output.get(), "results": self.result_paths, "split_output": self.split_output.get(),
                 "rule_file": self.rule_file.get(), "config": self._get_config() if self._config_text else None}
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix(".tmp"); temp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8"); os.replace(temp, self.settings_path)
        except (OSError, ValueError, TypeError): pass

    def _close(self):
        if self.running: self.status.set("‘작업 중단’을 누르고 보고서 저장이 끝나면 닫을 수 있습니다."); return
        if self.mapping and not self.mapping.closed: self.mapping._close()
        self._save_settings(); self.root.after_cancel(self.poll_job); self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception: pass
    root = tk.Tk(); App(root); root.mainloop()


if __name__ == "__main__": main()
