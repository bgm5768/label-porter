"""Guided desktop workspace. Porting and file operations live in workflow.py."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, scrolledtext
from . import widgets as ttk

from . import __version__
from .data_input_ui import DataInputPanel
from .input_files import source_paths
from .config import load_config, validate_config
from .rule_review import RuleReview, rule_connections
from .reports import STATUS, ISSUES
from .ui import INK, MUTED, ScrollPage, card, card_content, setup_style
from .workflow import Workflow, resource_directory, default_output_directory, AI_GUIDE_FILENAME
from .ui_lifecycle import UiEventQueue, remove_traces, release_variables


class App(Workflow):
    def __init__(self, root):
        self.root = root
        root.title(f"라벨 포터 {__version__} · 원본을 보존하는 라벨 반영")
        width, height = ttk.window_size(root, (1320, 900))
        root.geometry(f"{width}x{height}")
        root.minsize(min(1040, width), min(680, height))
        setup_style(root)
        self.queue = UiEventQueue()
        self.cancel_event = threading.Event()
        self.running = self.rule_ready = False
        self.preview_fingerprint = self.active_fingerprint = None
        self.review_step = 3
        self.preparing = False
        self.preview_generation = None
        self.owned_outputs = []
        self.connection_confirmed = False
        self.mapping = None
        self.rule_inspector = None
        self.builder = None
        self.preloaded_preview = None
        self.last_run = self.last_summary = None
        self.source_files = []
        self.result_folder = ""
        self.result_paths = []
        self.current_step, self.route = 0, "workflow"
        self.help_return = None
        self.source, self.output = tk.StringVar(), tk.StringVar(value=str(default_output_directory()))
        self.output_mode = "default"
        self.split_output = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="규칙 파일을 불러오거나 데이터로 규칙 만들기를 선택하세요.")
        self.rule_file = tk.StringVar(value="아직 불러온 파일이 없습니다.")
        self.rule_summary = tk.StringVar(value="아직 프로젝트 규칙이 없습니다.")
        self.rule_title = tk.StringVar(value="규칙을 준비해 주세요")
        self.run_summary, self.rule_name, self.description = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.input_hint = tk.StringVar(value="양쪽 모두 폴더 또는 파일 다중 선택이 가능합니다. 폴더를 선택하면 기존 파일 선택을 대신합니다.")
        self.source_count = tk.StringVar(value="선택한 원본 없음")
        self.result_count = tk.StringVar(value="선택한 결과 파일 없음")
        self.bundled_rules = {c["name"]: (p, c) for p in sorted((resource_directory()/"rules").glob("*.json")) for c in [load_config(p)]}
        self.settings_path = Path(os.environ.get("APPDATA", str(Path.home()))) / "LabelPorter" / "settings.json"
        self.ai_guide_text = (resource_directory()/AI_GUIDE_FILENAME).read_text(encoding="utf-8")
        self.help_text = (resource_directory()/"사용설명.md").read_text(encoding="utf-8")
        self.capabilities_text = (resource_directory()/"후처리_지원범위.md").read_text(encoding="utf-8")
        self.architecture_text = (resource_directory()/"아키텍처_및_규칙엔진.md").read_text(encoding="utf-8")
        self._config_text = ""
        self._build()
        self._load_settings()
        self.traces = [(self.output, self.output.trace_add("write", self._output_changed))]
        self.traces.append((self.output, self.output.trace_add("write", lambda *_: self._invalidate_preview(destination_only=True))))
        for var in (self.source, self.split_output):
            self.traces.append((var, var.trace_add("write", lambda *_: self._invalidate_preview())))
        root.bind("<Destroy>", self._destroyed, add="+")
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._show_step(0)
        self.poll_job = root.after(100, self._poll)

    def _build(self):
        workspace = ttk.Frame(self.root)
        workspace.pack(fill="both", expand=True)
        menu_font = font.Font(self.root, family="맑은 고딕", size=11, weight="bold")
        sidebar = tk.Frame(workspace, bg=ttk.SURFACE, width=max(220, menu_font.measure("4   검증·복사본 저장")+46))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="라벨 포터", bg=ttk.SURFACE, fg=INK, font=("맑은 고딕", 19, "bold")).pack(anchor="w", padx=22, pady=(28, 4))
        tk.Label(sidebar, text="규칙 → 데이터 → 확인 → 저장", bg=ttk.SURFACE, fg=MUTED, font=("맑은 고딕", 9)).pack(anchor="w", padx=22, pady=(0, 30))
        self.nav_buttons = []
        for i, title in enumerate(("1   규칙 준비", "2   입력·저장 위치", "3   필드 연결·프리뷰", "4   검증·복사본 저장")):
            b = ttk.Button(sidebar, text=title, anchor="w",
                          font=("맑은 고딕", 11, "bold"), command=lambda i=i: self._navigate(i))
            b.configure(height=52, border_width=0, hover_color=ttk.SELECTED)
            b.pack(fill="x", padx=10, pady=3)
            self.nav_buttons.append(b)
        tk.Label(sidebar, text=f"원본 보존 · 누락 항목 유지\n\nv{__version__}", justify="left", bg=ttk.SURFACE, fg=MUTED, font=("맑은 고딕", 9)).pack(side="bottom", anchor="w", padx=22, pady=24)
        self.update_button = ttk.Button(sidebar, text="프로그램 업데이트", anchor="w", command=self._show_updates)
        self.update_button.configure(border_width=0)
        self.update_button.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.viewer_button = ttk.Button(sidebar, text="JSON 뷰어", anchor="w",
                                       font=("맑은 고딕", 11), command=self._json_viewer)
        self.viewer_button.configure(border_width=0)
        self.viewer_button.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.help_button = ttk.Button(sidebar, text="사용 방법", anchor="w",
                                     font=("맑은 고딕", 11), command=lambda: self._show_tools(1))
        self.help_button.configure(border_width=0)
        self.help_button.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        def fit_sidebar(_=None):
            if not sidebar.winfo_exists(): return
            # Native captions and CTk controls use different DPI sizing rules.
            # Reserve their actual requested widths, including side padding.
            needed = max(220, *(child.winfo_reqwidth() + 44 for child in sidebar.winfo_children()))
            if int(sidebar.cget('width')) != needed: sidebar.configure(width=needed)
        sidebar.bind('<Configure>', fit_sidebar, add='+')
        for button in (*self.nav_buttons, self.help_button, self.viewer_button):
            button.bind('<Configure>', fit_sidebar, add='+')
        fit_sidebar()
        shell = ttk.Frame(workspace, padding=(24, 18, 24, 14))
        shell.pack(side="left", fill="both", expand=True)
        self.page_title, self.page_caption = tk.StringVar(), tk.StringVar()
        ttk.Label(shell, textvariable=self.page_title, style="Title.TLabel").pack(anchor="w")
        caption = ttk.Label(shell, textvariable=self.page_caption, style="Muted.TLabel", wraplength=800)
        caption.pack(fill="x", pady=(6, 12))
        shell.bind("<Configure>", lambda e: caption.winfo_exists() and caption.configure(wraplength=max(300, e.width-48)))
        # Allocate navigation first so it stays visible at small window sizes and high DPI.
        self.footer = ttk.Frame(shell, padding=(0, 12, 0, 0))
        self.footer.pack(side="bottom", fill="x")
        ttk.Separator(self.footer).pack(fill="x", pady=(0, 10))
        self.step_indicator = tk.StringVar()
        self.preparation_progress = ttk.Progressbar(self.footer, mode="determinate")
        status_row = ttk.Frame(self.footer); status_row.pack(fill="x", pady=(0, 10))
        self.footer_status_row = status_row
        ttk.Label(status_row, textvariable=self.step_indicator, style="Muted.TLabel").pack(side="right", padx=(12, 0))
        status = ttk.Label(status_row, textvariable=self.status, wraplength=700, style="Muted.TLabel")
        status.pack(side="left", fill="x", expand=True)
        self.footer.bind("<Configure>", lambda e: status.winfo_exists() and status.configure(wraplength=max(280, e.width-100)))
        actions = ttk.Frame(self.footer)
        self.footer_actions = actions
        actions.pack(fill="x")
        self.navigation = ttk.Frame(actions)
        self.navigation.pack(side="right")
        actions.bind('<Configure>', self._layout_footer, add='+')
        self.navigation.bind('<Configure>', self._layout_footer, add='+')
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
        group = card(project, "프로젝트 규칙", "저장된 규칙을 불러오거나, 원본과 보완 데이터를 넣고 반영할 내용만 선택해 규칙을 만드세요.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x", pady=(2, 12))
        self.import_rule_button = ttk.Button(row, text="규칙 파일 불러오기…", command=self._load_rule, style="Primary.TButton")
        self.import_rule_button.pack(side="left")
        self.create_rule_button = ttk.Button(row, text="데이터로 규칙 만들기", command=self._show_builder)
        self.create_rule_button.pack(side="left", padx=(10, 0))
        ttk.Label(group, textvariable=self.rule_file, style="CardMuted.TLabel", wraplength=700).pack(fill="x")
        group = card(project, "현재 규칙")
        self.current_rule_card = group
        ttk.Label(group, textvariable=self.rule_title, style="CardTitle.TLabel").pack(anchor="w", pady=(8, 8))
        ttk.Label(group, textvariable=self.rule_summary, style="CardMuted.TLabel", wraplength=720).pack(fill="x")
        self.rule_inspector_host = ttk.Frame(group, height=54)
        self.rule_inspector_host.pack(fill="x", pady=(12, 0))
        self.rule_inspector_host.pack_propagate(False)
        self.rule_inspector_empty = ttk.Label(self.rule_inspector_host, text="규칙을 불러오면 필드 연결과 전체 JSON이 여기에 표시됩니다.", style="Muted.TLabel")
        self.rule_inspector_empty.pack(anchor="w", padx=10, pady=14)
        self.step_pages[0].canvas.bind("<Configure>", self._resize_rule_inspector, add="+")
        self.save_rule_button = ttk.Button(group, text="규칙 내보내기", command=self._save_rule, state="disabled")
        self.save_rule_button.pack(anchor="w", pady=(14, 0))
        ttk.Label(project, text="규칙 작성이 필요하면 왼쪽 아래 ‘사용 방법 → AI 규칙 작성 가이드’를 이용하세요. 불러온 규칙은 읽기 전용으로 확인합니다.", style="Muted.TLabel", wraplength=720).pack(fill="x", pady=4)
        ttk.Label(inputs, textvariable=self.input_hint, wraplength=760, foreground=ttk.ACCENT).pack(fill="x", pady=(0, 14))
        group = card(inputs, "1. 보완 전 원본 라벨 데이터", "JSON / JSONL / NDJSON · 개별 파일과 통합 배열 모두 지원합니다.")
        self.source_panel = DataInputPanel(group, self.source_count, self._choose_source, self._choose_source_files,
                                           self._remove_source, lambda: self._json_viewer('source'))
        self.source_list = self.source_panel.list
        group = card(inputs, "2. 플랫폼 결과 또는 별도 보완 데이터", "JSON / JSONL / NDJSON / CSV · 원본에 반영할 보완 결과를 선택하세요.")
        self.result_panel = DataInputPanel(group, self.result_count, self._choose_result_folder, self._choose_results,
                                           self._remove_results, lambda: self._json_viewer('result'))
        self.result_list = self.result_panel.list
        group = card(inputs, "3. 새 결과를 저장할 폴더", "기본값은 프로그램 폴더의 results입니다. 직접 선택한 경로에는 이름을 덧붙이지 않습니다. 그 아래 날짜 / 순번으로 실행별 결과를 보관합니다.")
        self._path_row(group, self.output, self._choose_output)
        self.default_output_button = ttk.Button(group, text="기본 위치", command=self._reset_output)
        self.default_output_button.pack(anchor="e", pady=(6, 0))
        ttk.Checkbutton(inputs, text="정상 데이터도 변경 / 미변경으로 분리 · 검토·짝 없음·조건 제외·파일 오류는 분류별 examine에 저장", variable=self.split_output).pack(anchor="w", pady=(0, 12))
        group = card(review, "이번 작업", "전체 데이터 검증 후 집계와 보고서를 확인하고 복사본을 저장하세요.")
        ttk.Label(group, textvariable=self.run_summary, style="CardMuted.TLabel", wraplength=760).pack(fill="x")
        self.metric_values = {}
        metrics = ttk.Frame(review); metrics.pack(fill="x", pady=(0, 14))
        for i, (key, title) in enumerate((("source", "원본 레코드"), ("result", "보완 레코드"), ("completed", "처리 완료 / 통과"), ("fields", "변경 필드"), ("changed", "변경"), ("same", "변경 없음"), ("missing", "짝이 없는 데이터"), ("review", "검토 / 오류"))):
            metrics.columnconfigure(i % 4, weight=1, uniform="metric")
            box = ttk.Card(metrics, padding=12)
            box.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 7), pady=(0, 7))
            ttk.Label(box, text=title, style="CardMuted.TLabel").pack(anchor="w")
            var = self.metric_values[key] = tk.StringVar(value="—")
            ttk.Label(box, textvariable=var, style="CardTitle.TLabel", font=("맑은 고딕", 21, "bold")).pack(anchor="w", pady=(8, 0))
        self.review_note = tk.StringVar(value="전체 데이터 검증을 실행하면 집계가 표시됩니다.")
        ttk.Label(review, textvariable=self.review_note, wraplength=760).pack(fill="x", pady=(0, 10))
        self.progress = ttk.Progressbar(review, mode="determinate"); self.progress.pack(fill="x", pady=(0, 12))
        self.status_table = self._table(review, (("구분", 100), ("처리 결과", 420), ("건수", 90)), 5)
        links = ttk.Frame(review); links.pack(fill="x", pady=(0, 12))
        self.preview_button = ttk.Button(links, text="전체 데이터 다시 검증", command=lambda: self._start(True)); self.preview_button.pack(side="left")
        self.report_button = ttk.Button(self.footer_actions, text="처리 결과서 열기", command=self._open_report, state="disabled")
        self.folder_button = ttk.Button(self.footer_actions, text="결과 폴더", command=self._open_folder, state="disabled")
        # Execution code can disable this action, while the footer provides the current label.
        self.run_button = self.primary_button
        ttk.Label(review, text="진행 기록", style="Muted.TLabel").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(review, wrap="word", height=5, font=("맑은 고딕", 9), state="disabled", relief="flat", foreground=MUTED)
        self.log.pack(fill="x", pady=(6, 0))
        self.mapping_host = ttk.Frame(self.content)
        self.builder_host = ttk.Frame(self.content)
        self.help_host = ttk.Card(self.content, padding=12)
        self.tabs = ttk.Notebook(self.help_host); self.tabs.pack(fill="both", expand=True)
        ai, help_page, capabilities, architecture = [ttk.Frame(self.tabs, padding=12) for _ in range(4)]
        self.tabs.add(ai, text="AI 규칙 작성 가이드"); self.tabs.add(help_page, text="사용 방법")
        self.tabs.add(capabilities, text="후처리 지원 범위")
        self.tabs.add(architecture, text="아키텍처 · 규칙 엔진")
        row = ttk.Frame(ai); row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="가이드 복사", command=self._copy_ai_guide).pack(side="left")
        ttk.Button(row, text="가이드 저장", command=self._save_ai_guide).pack(side="left", padx=8)
        self.ai_guide_box = self._document_view(ai, self.ai_guide_text)
        self.help_box = self._document_view(help_page, self.help_text)
        self.capabilities_box = self._document_view(capabilities, self.capabilities_text)
        self.architecture_box = self._document_view(architecture, self.architecture_text)
        card_content(self.help_host)
        from .update_ui import UpdatePanel
        self.update_host = ttk.Card(self.content, padding=12)
        self.update_page = ScrollPage(self.update_host)
        self.update_page.pack(fill="both", expand=True)
        self.updater = UpdatePanel(self.update_page.body, self)
        self.updater.pack(fill="both", expand=True)

    @staticmethod
    def _table(parent, columns, height):
        table = ttk.Treeview(parent, columns=list(range(len(columns))), show="headings", height=height)
        for i, (title, width) in enumerate(columns):
            table.heading(i, text=title); table.column(i, width=width, minwidth=60)
        table.pack(fill="x", pady=12)
        return table

    def _json_viewer(self, side=None):
        from .json_viewer import JsonViewer
        builder = self.builder if self.route == "builder" else None
        if side == 'source':
            paths = [self.source.get()] if self.source.get() else list(self.source_files)
            chosen = self.source_list.curselection()
        elif side == 'result':
            paths = [self.result_folder] if self.result_folder else list(self.result_paths)
            chosen = self.result_list.curselection()
        elif builder:
            paths = [builder.source.get(), *builder.results]
            chosen = ()
        else:
            paths = ([self.source.get()] if self.source.get() else list(self.source_files)) + ([self.result_folder] if self.result_folder else list(self.result_paths))
            chosen = ()
        initial = paths[chosen[0]] if chosen and chosen[0] < len(paths) else None
        self.json_viewer = JsonViewer(self.root, initial=initial, paths=[p for p in paths if p],
                                      on_close=self._json_viewer_closed)

    def _json_viewer_closed(self, viewer):
        if getattr(self, 'json_viewer', None) is viewer:
            self.json_viewer = None

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
        if index == 3 and not self._session_ready() and not self._saved():
            self.status.set("3단계 프리뷰 계산이 끝난 뒤 전체 검증 단계로 이동할 수 있습니다.")
            self._wizard()
            return
        if index == 2:
            self._wizard()
            return
        self.route, self.current_step = "workflow", index
        self.help_host.pack_forget(); self.help_return = None
        self.update_host.pack_forget()
        self.mapping_host.pack_forget()
        self.builder_host.pack_forget()
        self.steps.pack(fill="both", expand=True); self.steps.tab(index, state="normal"); self.steps.select(index)
        self.footer.pack(side="bottom", fill="x", before=self.content)
        self.page_title.set(("프로젝트 규칙 준비", "입력 데이터와 저장 위치", "필드 연결 확인", "처리 결과 확인과 복사본 저장")[index])
        self.page_caption.set(("규칙 파일 불러오기 또는 데이터로 규칙 만들기 중 편한 방법을 선택하세요.",
                               "여기서 선택한 데이터를 다음 단계의 프리뷰와 전체 처리에 함께 사용합니다.", "",
                               "전체 데이터를 검증하고 보고서를 확인한 뒤 복사본을 저장하세요.")[index])
        self._sync_actions()

    def _sync_actions(self):
        self.update_button.configure(state="disabled" if self.running or (self.builder and self.builder.busy) else "normal")
        # Disabling a selected native Notebook tab silently switches its page.
        # Keep an already-open failure/result page visible; navigation into it
        # is still guarded by _advance_review and the disabled sidebar action.
        viewing_result = self.current_step == 3 and self.route == 'workflow'
        self.steps.tab(3, state="normal" if self._session_ready() or viewing_result else "disabled")
        if self.preparing:
            self.preparation_progress.pack(fill="x", pady=(0, 8), before=self.footer_status_row)
        else:
            self.preparation_progress.pack_forget()
        if self.route == "builder" and self.builder:
            builder = self.builder
            self.back_button.configure(text="← 규칙 준비로" if builder.stage == 0 else "← 이전 단계",
                                       state="disabled" if builder.busy else "normal")
            self.primary_button.grid()
            self.primary_button.configure(text=builder.next_title, state="normal" if builder.can_next else "disabled")
            for button in (self.cancel_button, self.report_button, self.folder_button): button.pack_forget()
            for i, button in enumerate(self.nav_buttons):
                button.configure(state="normal" if i == 0 and not builder.busy else "disabled",
                                 bg=ttk.SELECTED if i == 0 else ttk.SURFACE, fg=ttk.SELECTED_INK if i == 0 else INK)
            self.help_button.configure(state="normal", bg=ttk.SURFACE, fg=INK)
            self.step_indicator.set(f"규칙 만들기 · {builder.stage + 1} / 3")
            return
        helping = self.route == "help"
        self.back_button.configure(text="← 작업으로 돌아가기" if helping else "← 이전 단계",
                                   state="disabled" if self.running or (self.current_step == 0 and not helping) else "normal")
        self.cancel_button.configure(state="normal" if self.running and not self.cancel_event.is_set() else "disabled")
        for button in (self.cancel_button, self.report_button, self.folder_button): button.pack_forget()
        for i, button in enumerate(self.nav_buttons):
            active = i == self.current_step and not helping
            button.configure(state="disabled" if self.running or (i > 0 and not self.rule_ready) or (i == 3 and not self._session_ready()) else "normal",
                             bg=ttk.SELECTED if active else ttk.SURFACE, fg=ttk.SELECTED_INK if active else INK)
        self.help_button.configure(state="disabled" if self.running else "normal", bg=ttk.SELECTED if helping else ttk.SURFACE, fg=ttk.SELECTED_INK if helping else INK)
        self.step_indicator.set("도움말" if helping else f"{self.current_step + 1} / 4 단계")
        if helping:
            self.primary_button.grid_remove()
            self._layout_footer()
            return
        self.primary_button.grid()
        if self.running: self.cancel_button.pack(side="left", padx=(0, 8))
        if self.current_step == self.review_step:
            self.report_button.pack(side="left", padx=(0, 8)); self.folder_button.pack(side="left")
        title = "다음 단계 →" if self.current_step < self.review_step else "복사본 저장" if self.preview_fingerprint else "전체 데이터 검증"
        if self.current_step == self.review_step and self._saved(): title = "결과 폴더 열기"
        if self.current_step == 2:
            title = "프리뷰 계산 중…" if self.preparing else "전체 검증 단계로 →" if self._session_ready() else "프리뷰 다시 불러오기"
        if self.current_step == 3 and not self._session_ready() and not self._saved(): title = "3단계 프리뷰 확인 필요"
        self.primary_button.configure(text=title, state="normal" if not self.running and self.rule_ready and (self.current_step != 3 or self._session_ready() or self._saved()) else "disabled")
        self._layout_footer()

    def _layout_footer(self, _=None):
        if not self.footer_actions.winfo_exists(): return
        others = [child for child in self.footer_actions.winfo_children()
                  if child is not self.navigation and child.winfo_manager() == 'pack']
        needed = self.navigation.winfo_reqwidth() + sum(child.winfo_reqwidth() + 8 for child in others)
        stacked = bool(others) and needed > self.footer_actions.winfo_width()
        side = 'bottom' if stacked else 'right'
        if self.navigation.pack_info().get('side') != side:
            self.navigation.pack_configure(side=side, anchor='e', pady=(8, 0) if stacked else 0)

    def _saved(self):
        return self.last_summary and not self.last_summary.get("preview") and self.last_summary["status"].startswith("completed")

    def _navigate(self, index):
        if self.running: return
        if self.route == "help": self._return_from_help()
        if self.route != "builder" and index == self.current_step and (index != self.review_step or self.connection_confirmed): return
        if index == 0: self._show_step(0)
        elif index == 1: self._advance_inputs()
        elif index == 2: self._wizard()
        elif self.route == "mapping" and self.mapping:
            self.mapping.apply_button.invoke()
        else: self._advance_review()

    def _primary(self):
        if self.running or self.route == "help": return
        if self.route == "builder":
            self.builder.next(); return
        if self.current_step == 0: self._advance_inputs()
        elif self.current_step == 1: self._wizard()
        elif self.current_step == 2 and self.mapping: self.mapping._apply()
        elif self._saved(): self._open_folder()
        else: self._start(self.preview_fingerprint is None)

    def _back(self):
        if self.running: return
        if self.route == "help": self._return_from_help()
        elif self.route == "builder": self.builder.back()
        else: self._show_step(max(0, self.current_step - 1))

    def _show_tools(self, tab=1):
        if self.running: return
        if self.route != "help":
            self.help_return = (self.route, self.page_title.get(), self.page_caption.get(), self.status.get())
        self.steps.pack_forget(); self.mapping_host.pack_forget(); self.builder_host.pack_forget()
        self.update_host.pack_forget()
        self.help_host.pack(fill="both", expand=True)
        self.route = "help"
        self.tabs.select(tab)
        self.page_title.set("사용 방법")
        self.page_caption.set("작성 가이드·사용 방법·지원 범위·프로그램 구조를 확인하세요. 돌아가면 보던 단계에서 계속할 수 있습니다.")
        self.status.set("작업의 입력값과 프리뷰 상태를 유지하고 있습니다.")
        self._sync_actions()

    def _show_updates(self):
        if self.running or (self.builder and self.builder.busy): return
        self._show_tools(1)
        self.help_host.pack_forget()
        self.update_host.pack(fill="both", expand=True)
        self.page_title.set("프로그램 업데이트")
        self.page_caption.set("GitHub 정식 릴리스를 확인하고 원하는 시점에 설치하세요.")

    def _return_from_help(self):
        if self.route != "help" or not self.help_return: return
        self.help_host.pack_forget()
        self.update_host.pack_forget()
        self.route, title, caption, status = self.help_return
        self.help_return = None
        (self.builder_host if self.route == "builder" else self.mapping_host if self.route == "mapping" else self.steps).pack(fill="both", expand=True)
        self.page_title.set(title); self.page_caption.set(caption); self.status.set(status)
        self._sync_actions()
        if self.route == "builder": self._builder_changed()

    def _show_builder(self):
        if self.running: return
        from .easy_rules_ui import EasyRules
        self.steps.pack_forget(); self.mapping_host.pack_forget(); self.help_host.pack_forget()
        self.route, self.current_step = "builder", 0
        if self.builder is None:
            self.builder = EasyRules(self.builder_host, self._builder_changed, self._finish_builder,
                                     lambda: self._show_step(0), self.source.get(), [self.result_folder] if self.result_folder else self.result_paths)
            self.builder.pack(fill="both", expand=True)
        self.builder_host.pack(fill="both", expand=True)
        self._builder_changed()

    def _builder_changed(self):
        if self.route != "builder" or self.builder is None: return
        self.page_title.set("데이터로 규칙 만들기")
        self.page_caption.set(f"{self.builder.stage + 1}. {self.builder.TITLES[self.builder.stage]} · 다른 필드명도 직접 연결 · 선택하지 않은 값은 원본 유지")
        self.status.set(self.builder.status.get())
        self._sync_actions()

    def _close_builder(self):
        if self.builder:
            self.builder.close(); self.builder.destroy(); self.builder = None

    def _finish_builder(self, rule_config, inputs, data):
        self._close_mapping()
        self._set_config(rule_config)
        self.source_files = []; self.result_folder = ""
        self.source.set(inputs.source); self._render_sources()
        self.result_paths = list(inputs.result_files); self._render_results()
        self.rule_file.set("간편 작성 규칙 · ‘규칙 내보내기’로 JSON 파일 저장 가능")
        self.preloaded_preview = (self._mapping_inputs(), data)
        self._close_builder()
        self._wizard()
        if self.route == "mapping":
            self.status.set("규칙과 입력을 저장했습니다. 연결·프리뷰를 확인한 뒤 4단계에서 전체 데이터를 검증하세요.")

    def _set_config(self, config):
        config = validate_config(config)
        self._config_text = json.dumps(config, ensure_ascii=False, indent=2)
        self.rule_name.set(config["name"]); self.description.set(config.get("description", ""))
        self._invalidate_rule(); self._summarize_rule(config)
        self._inspect_rule(config)
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
        connections = rule_connections(config)
        self.rule_summary.set(f"수정 필드 {sum(r['kind']=='field' for r in connections)}개 · 목록 {sum(r['kind'] in ('items', 'rebuild_items') for r in connections)}개 · 레코드 조건 / 검증 {checks}개\n규칙에 정의된 연결 · 읽기 전용 · 실제 데이터 비교는 데이터 선택 후 확인합니다.")

    def _inspect_rule(self, config):
        if self.rule_inspector and self.rule_inspector.rule_config == config: return
        if self.rule_inspector: self.rule_inspector._close(notify=False)
        self.rule_inspector_empty.pack_forget()
        self.rule_inspector = RuleReview(self.rule_inspector_host, config, lambda: None, lambda: None,
                                         external_navigation=True, rule_only=True)
        self._resize_rule_inspector()

    def _resize_rule_inspector(self, event=None):
        if self.rule_inspector:
            # Keep the connection list spacious, including after a window resize.
            height = max(360, self.step_pages[0].canvas.winfo_height() - 100)
            if int(self.rule_inspector_host.cget("height")) != height:
                self.rule_inspector_host.configure(height=height)

    def _focus_current_rule(self):
        self.root.update_idletasks()
        page = self.step_pages[0]
        height = page.body.winfo_height()
        if height:
            # Bring the enlarged inspector into view, rather than clipping it
            # below the rule title and summary on smaller displays.
            top = self.rule_inspector_host.winfo_rooty() - page.body.winfo_rooty()
            page.canvas.yview_moveto(max(0, top - 8) / height)

    def _invalidate_preview(self, *, destination_only=False):
        if not destination_only:
            if self.preparing and self.mapping: self.mapping.preview.cancel()
            if self.preloaded_preview and self.preloaded_preview[0] != self._mapping_inputs():
                self.preloaded_preview = None
            if self.mapping: self._close_mapping()
            self.preview_generation = None
            self.connection_confirmed = False
        self.preview_fingerprint = self.last_summary = self.last_run = None
        for var in self.metric_values.values(): var.set('—')
        self.status_table.delete(*self.status_table.get_children())
        self.review_note.set('4단계에서 전체 데이터를 검증해 주세요.')
        self.report_button.configure(state='disabled')
        self.folder_button.configure(state='disabled')
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
            self.status.set("원본·보완 데이터를 폴더 또는 파일로 선택하고 저장 위치를 확인하세요."); self._save_settings()
        except (ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 확인: {exc}")

    def _advance_review(self):
        if self.running: return
        if not self._session_ready():
            self.status.set("3단계 프리뷰 계산이 끝난 뒤 전체 검증 단계로 이동할 수 있습니다.")
            self._wizard(); return
        try:
            self._check_inputs()
            results = self.mapping.preview.results if self.mapping else self.result_paths
            self.run_summary.set(f"규칙  {self._get_config()['name']}\n원본  {self._source_label()}\n보완  {len(results):,}개 파일\n저장  {self.output.get()}\n분리  {'변경 / 미변경 / examine' if self.split_output.get() else '전체 복사본 + examine'}")
            self.steps.tab(3, state="normal"); self._show_step(3)
            self.status.set("프리뷰 확인 완료 · 전체 데이터 검증을 실행한 뒤 복사본을 저장하세요."); self._save_settings()
        except (ValueError, OSError) as exc: self.status.set(f"입력 확인: {exc}")

    def _load_rule(self):
        if self.running: return
        path = filedialog.askopenfilename(title="AI / 저장된 프로젝트 규칙", filetypes=[("규칙 JSON", "*.json")], parent=self.root)
        if path:
            try:
                config = load_config(Path(path))
                self._close_builder()
                self.preloaded_preview = None
                self._close_mapping()
                self.source_files = []; self.result_folder = ""
                self.source.set(""); self.result_paths = []; self._render_sources(); self._render_results()
                if not self.output.get().strip(): self.output.set(str(default_output_directory()))
                self._set_config(config)
                self.rule_file.set(str(Path(path)))
                self._show_step(0)
                self._focus_current_rule()
                self.status.set("규칙을 불러왔습니다. 연결과 JSON을 확인한 뒤 ‘다음 단계’를 눌러 데이터를 선택하세요.")
                self._save_settings()
            except (OSError, ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 불러오기: {exc}")

    def _save_rule(self):
        try:
            config = self._get_config()
            path = filedialog.asksaveasfilename(title="규칙 내보내기", defaultextension=".json", initialfile="project_rule.json", filetypes=[("JSON", "*.json")], parent=self.root)
            if path:
                Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); self.status.set(f"규칙 저장 완료: {path}")
        except (OSError, ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 저장: {exc}")

    def _render_results(self):
        super()._render_results()

    def _wizard(self):
        if self.running: return
        reuse = self.mapping and not self.mapping.closed and self.mapping_inputs == self._mapping_inputs()
        if not reuse:
            self._prepare_mapping()
            return
        self._open_mapping(self.mapping.preview.results, reuse=True)

    def _open_mapping(self, results, reuse=False):
        config = self._get_config()
        self._save_settings()
        self.current_step, self.route = 2, "mapping"
        self.steps.tab(2, state="normal"); self.steps.select(2); self.steps.pack_forget()
        self.builder_host.pack_forget()
        self.mapping_host.pack(fill="both", expand=True)
        self.page_title.set("필드 연결과 실제 값 확인")
        self.page_caption.set("전체 입력을 검색하고 레코드를 선택하여 연결값과 적용 후 JSON을 확인하세요.")
        self.status.set("프리뷰는 선택한 레코드의 적용 결과를 계산합니다. 전체 검증과 파일 저장은 4단계에서 진행합니다.")
        self._sync_actions()
        if not reuse:
            self._close_mapping()
            self.mapping_inputs = self._mapping_inputs()
            preloaded = self.preloaded_preview[1] if self.preloaded_preview and self.preloaded_preview[0] == self.mapping_inputs else None
            self.preloaded_preview = None
            self.mapping = RuleReview(self.mapping_host, config, self._finish_mapping,
                                      lambda: self._show_step(1), source_hint=self._source_input(),
                                      result_hints=results, configured=True, external_navigation=True,
                                      preloaded=preloaded, on_state=self._preview_state_changed)
            self._preview_state_changed()

    def _mapping_inputs(self):
        return (self._config_text, self._source_input(), tuple(self.result_paths), self.result_folder)

    def _finish_mapping(self):
        if self.running: return
        if not self._session_ready():
            self.mapping.preview.load(refresh=True)
            return
        self.connection_confirmed = True
        self._advance_review()

    def _start(self, preview):
        if self.running: return
        if not self._session_ready():
            self._wizard(); return
        self._run_validation(preview)

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
                if kind == 'owned_output':
                    if payload not in self.owned_outputs: self.owned_outputs.append(payload)
                    continue
                if kind == "run_fingerprint":
                    self.checking_inputs = False
                    self.active_fingerprint = payload
                    continue
                if kind in ("mapping_ready", "input_error"):
                    self.running = False
                    self.checking_inputs = False
                    if kind == "input_error":
                        self.status.set("입력 확인: " + payload)
                    else:
                        request, results = payload
                        if self.cancel_event.is_set():
                            self.status.set("입력 확인을 중단했습니다.")
                        elif request != self._input_request():
                            self.status.set("입력 설정이 바뀌었습니다. 다음 단계를 다시 눌러 주세요.")
                        else:
                            self._open_mapping(results)
                    self._sync_actions()
                    continue
                if kind == "progress":
                    self.status.set(payload["message"])
                    self.progress.configure(value=100*payload["done"]/payload["total"] if payload["total"] else 0)
                    self._append_log(payload["message"]); continue
                self.running = False; self.checking_inputs = False; self.preview_button.configure(state="normal")

                for i in range(4): self.steps.tab(i, state="normal")
                if kind == "error":
                    if not self._session_ready():
                        self.preview_fingerprint = None
                        self.connection_confirmed = False
                    self.status.set("실행하지 못했습니다: "+payload); self.review_note.set("실행 실패 · 입력과 규칙을 확인하고 다시 검증하세요."); self._append_log(payload)
                else:
                    self.last_run = payload.directory; summary = self.last_summary = payload.summary
                    self.report_button.configure(state="normal"); self.folder_button.configure(state="normal")
                    if (summary.get("preview") and summary["status"].startswith("completed")
                            and self.active_request == self._input_request()):
                        self.preview_fingerprint = self.active_fingerprint
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
            self.source_files = s.get("source_files", [])
            self.result_folder = s.get("result_folder", "")
            self.source.set(s.get("source", "")); self._render_sources()
            # Legacy settings have no provenance: preserve their path rather than guess
            # whether a directory named '포팅결과' was explicitly selected.
            mode = "default" if s.get("output_mode") == "default" or not (s.get("output") or "").strip() else "custom"
            self.output.set(str(default_output_directory()) if mode == "default" else s["output"])
            self.output_mode = mode
            self.split_output.set(s.get("split_output", True))
            self.result_paths = s.get("results", []); self._render_results()
            if s.get("config"): self._set_config(s["config"])
            else:
                # Older settings stored the bundled rule name under this legacy key.
                rule_name = s.get("rule", s.get("profile"))
                if rule_name in self.bundled_rules: self._set_config(self.bundled_rules[rule_name][1])
            self.rule_file.set(s.get("rule_file", "최근 저장한 프로젝트 규칙"))
            if self._config_text: self.status.set("최근 작업의 규칙과 입력 위치를 복원했습니다. 확인 후 계속하세요.")
        except (OSError, ValueError, TypeError, KeyError): pass

    def _save_settings(self):
        try:
            s = {"source": self.source.get(), "source_files": self.source_files, "result_folder": self.result_folder, "output": self.output.get(), "output_mode": self.output_mode, "results": self.result_paths, "split_output": self.split_output.get(),
                 "rule_file": self.rule_file.get(), "config": self._get_config() if self._config_text else None}
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix(".tmp"); temp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8"); os.replace(temp, self.settings_path)
            return True
        except (OSError, ValueError, TypeError): return False

    def _destroyed(self, event):
        if event.widget is not self.root: return
        self.cancel_event.set()
        self.queue.close()
        remove_traces(self.traces)
        self.root.after_cancel(self.poll_job)
        self.poll_job = None
        self.metric_values.clear()
        self.preloaded_preview = None
        release_variables(self)

    def _close(self):
        if self.running: self.status.set("‘작업 중단’을 누르고 보고서 저장이 끝나면 닫을 수 있습니다."); return
        if self.mapping and not self.mapping.closed: self.mapping._close()
        if self.rule_inspector: self.rule_inspector._close(notify=False)
        self._close_builder()
        self._save_settings(); self.root.after_cancel(self.poll_job); self.root.destroy()


def main():
    # CTk owns DPI awareness; setting Tk's older system-only mode first prevents
    # CTk from enabling its per-monitor scaling on Windows.
    root = ttk.create_root(); app = App(root)
    app.updater.startup()
    root.mainloop()


if __name__ == "__main__": main()
