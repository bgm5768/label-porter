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
from .formats import parse_json
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
        self.mapping_source = self.mapping_result = ""
        self.mapping = None
        self.last_run = self.last_summary = None
        self.result_paths = []
        self.current_step, self.route = 0, "workflow"
        self.source, self.output = tk.StringVar(), tk.StringVar()
        self.split_output = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="AI에게 원본 라벨·플랫폼 결과·MD 가이드를 전달한 뒤 받은 규칙을 불러오세요.")
        self.rule_summary = tk.StringVar(value="아직 프로젝트 규칙이 없습니다.")
        self.rule_title = tk.StringVar(value="규칙을 준비해 주세요")
        self.run_summary, self.profile_name, self.description = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.input_hint = tk.StringVar(value="실제로 처리할 전체 원본 폴더와 결과 파일을 지정하세요.")
        self.result_count = tk.StringVar(value="선택한 결과 파일 없음")
        self.profiles = {c["name"]: (p, c) for p in sorted((resource_directory()/"profiles").glob("*.json")) for c in [load_config(p)]}
        self.settings_path = Path(os.environ.get("APPDATA", str(Path.home()))) / "LabelPorter" / "settings.json"
        self.ai_guide_text = (resource_directory()/AI_GUIDE_FILENAME).read_text(encoding="utf-8")
        self.help_text = (resource_directory()/"사용설명.md").read_text(encoding="utf-8")
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
        self.toolbar_buttons = []
        for label, command in (("＋ 새 프로젝트", self._new_project),
                               ("AI 규칙 작성 가이드", lambda: self._show_tools(0)),
                               ("사용 방법", lambda: self._show_tools(2))):
            button = ttk.Button(self.toolbar, text=label, command=command, style="Toolbar.TButton")
            button.pack(side="left", padx=(0, 6))
            self.toolbar_buttons.append(button)
        ttk.Button(self.toolbar, text="JSON 뷰어", command=self._json_viewer, style="Toolbar.TButton").pack(side="left", padx=6)
        ttk.Label(self.toolbar, text="로컬 작업 공간  ·  원본 보존", style="Toolbar.TLabel").pack(side="right", padx=8)
        ttk.Separator(self.root).pack(side="top", fill="x")
        workspace = ttk.Frame(self.root)
        workspace.pack(fill="both", expand=True)
        menu_font = font.Font(self.root, family="맑은 고딕", size=11, weight="bold")
        sidebar = tk.Frame(workspace, bg="#edf0f4", width=max(200, menu_font.measure("3   검증·복사본 저장")+46))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="라벨 포터", bg="#edf0f4", fg=INK, font=("맑은 고딕", 19, "bold")).pack(anchor="w", padx=22, pady=(28, 4))
        tk.Label(sidebar, text="플랫폼 결과 → 원본 라벨", bg="#edf0f4", fg=MUTED, font=("맑은 고딕", 9)).pack(anchor="w", padx=22, pady=(0, 30))
        self.nav_buttons = []
        for i, title in enumerate(("1   프로젝트 규칙", "2   입력·저장 위치", "3   검증·복사본 저장")):
            b = tk.Button(sidebar, text=title, anchor="w", padx=12, pady=16, bd=0,
                          bg="#edf0f4", fg=MUTED, activebackground="#e0e5ec", activeforeground=INK,
                          font=("맑은 고딕", 11, "bold"), command=lambda i=i: self._navigate(i))
            b.pack(fill="x", padx=10, pady=3)
            self.nav_buttons.append(b)
        tk.Label(sidebar, text=f"원본 보존 · 누락 항목 유지\n\nv{__version__}", justify="left", bg="#edf0f4", fg=MUTED, font=("맑은 고딕", 9)).pack(side="bottom", anchor="w", padx=22, pady=24)
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
        status = ttk.Label(self.footer, textvariable=self.status, wraplength=800, style="Muted.TLabel")
        status.pack(fill="x", pady=(0, 10))
        self.footer.bind("<Configure>", lambda e: status.configure(wraplength=max(300, e.width)))
        actions = ttk.Frame(self.footer)
        self.footer_actions = actions
        actions.pack(fill="x")
        self.back_button = ttk.Button(actions, command=self._back)
        self.back_button.pack(side="left")
        self.primary_button = ttk.Button(actions, style="Primary.TButton", command=self._primary)
        self.primary_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="작업 중단", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self.content = ttk.Frame(shell)
        self.content.pack(fill="both", expand=True)
        self.steps = ttk.Notebook(self.content, style="Flow.TNotebook")
        self.steps.pack(fill="both", expand=True)
        self.step_pages = [ScrollPage(self.steps) for _ in range(3)]
        for i, page in enumerate(self.step_pages):
            self.steps.add(page, text=str(i+1), state="normal" if i == 0 else "disabled")
        project, inputs, review = [p.body for p in self.step_pages]
        group = card(project, "01  AI에게 가이드와 데이터 전달", "원본 라벨 · 플랫폼 최종 결과 · 작성 가이드를 AI에게 전달해 규칙 작성을 요청하세요.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x")
        ttk.Button(row, text="가이드 복사", command=self._copy_ai_guide).pack(side="left")
        ttk.Button(row, text="MD 파일 저장", command=self._save_ai_guide).pack(side="left", padx=8)
        group = card(project, "02  AI가 만든 규칙 가져오기", "JSON 파일을 열거나 AI 답변의 JSON을 붙여넣으세요. 가져오면 필드 연결 확인 화면이 열립니다.")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x")
        self.import_rule_button = ttk.Button(row, text="규칙 파일 열기…", command=self._load_profile)
        self.import_rule_button.pack(side="left")
        self.paste_rule_button = ttk.Button(row, text="JSON 붙여넣기…", command=lambda: self._show_tools(1))
        self.paste_rule_button.pack(side="left", padx=8)
        group = card(project, "현재 규칙")
        ttk.Label(group, textvariable=self.rule_title, style="CardTitle.TLabel").pack(anchor="w", pady=(12, 8))
        ttk.Label(group, textvariable=self.rule_summary, style="CardMuted.TLabel", wraplength=720).pack(fill="x")
        row = ttk.Frame(group, style="Card.TFrame"); row.pack(fill="x", pady=(12, 0))
        self.edit_rule_button = ttk.Button(row, text="필드 연결 확인", command=self._wizard, state="disabled")
        self.edit_rule_button.pack(side="left")
        self.save_rule_button = ttk.Button(row, text="규칙 내보내기", command=self._save_profile, state="disabled")
        self.save_rule_button.pack(side="left", padx=8)
        ttk.Label(project, text="연결 확인 화면은 읽기 전용입니다. 수정이 필요하면 AI에 현재 규칙과 변경 내용을 전달해 새 규칙을 받아 불러오세요.", style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=5)
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
        ttk.Checkbutton(inputs, text="정상 데이터도 변경 / 미변경으로 분리 · 검토 대상은 항상 examine에 저장", variable=self.split_output).pack(anchor="w", pady=(0, 12))
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
        self.mapping_host, self.tools_host = ttk.Frame(self.content), ttk.Frame(self.content)
        self.tabs = ttk.Notebook(self.tools_host); self.tabs.pack(fill="both", expand=True)
        ai, rule, help_page = [ttk.Frame(self.tabs, padding=12) for _ in range(3)]
        for f, title in ((ai, "AI 규칙 작성"), (rule, "규칙 JSON 붙여넣기"), (help_page, "사용 방법")):
            self.tabs.add(f, text=title)
        row = ttk.Frame(ai); row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="가이드 전체 복사", command=self._copy_ai_guide).pack(side="left")
        ttk.Button(row, text="MD 파일로 저장", command=self._save_ai_guide).pack(side="left", padx=8)
        ttk.Button(row, text="받은 규칙 불러오기", command=self._load_profile).pack(side="right")
        self.ai_guide_box = self._document_view(ai, self.ai_guide_text)
        ttk.Label(rule, text="AI가 반환한 JSON 전체를 붙여 넣고 ‘JSON 적용’을 누르세요. 적용하면 필드 연결 확인 화면이 열립니다.", wraplength=760).pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(rule); row.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(row, text="JSON 적용", command=self._apply_json, style="Primary.TButton").pack(side="right")
        ttk.Button(row, text="문법 검사", command=self._check_config).pack(side="left")
        self.editor = scrolledtext.ScrolledText(rule, wrap="none", undo=True, font=("Consolas", 10), relief="flat"); self.editor.pack(fill="both", expand=True)
        self.help_box = self._document_view(help_page, self.help_text)

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

    def _show_step(self, index):
        self.route, self.current_step = "workflow", index
        self.mapping_host.pack_forget(); self.tools_host.pack_forget()
        self.steps.pack(fill="both", expand=True); self.steps.select(index)
        self.footer.pack(side="bottom", fill="x", before=self.content)
        self.page_title.set(("AI 규칙 준비와 확인", "입력과 저장 위치 확인", "검증 후 복사본 저장")[index])
        self.page_caption.set(("AI가 규칙을 작성하고, 사용자는 연결과 적용 조건을 확인합니다.", "실제로 처리할 전체 범위와 저장 위치를 확인하세요.", "결과가 없는 원본은 유지합니다. 변경·미변경과 검토 사유를 함께 확인하세요.")[index])
        for i, b in enumerate(self.nav_buttons):
            b.configure(bg="#dfe5ed" if i == index else "#edf0f4", fg=INK if i == index else MUTED)
        self._sync_actions()

    def _sync_actions(self):
        self.back_button.configure(text="작업으로 돌아가기" if self.route == "tools" else "규칙 개요로 돌아가기" if self.route == "mapping" else "이전 단계",
                                   state="disabled" if self.running or (self.route == "workflow" and self.current_step == 0) else "normal")
        self.cancel_button.configure(state="normal" if self.running and not self.cancel_event.is_set() else "disabled")
        for button in (self.primary_button, self.cancel_button, self.report_button, self.folder_button):
            button.pack_forget()
        for b in self.nav_buttons: b.configure(state="disabled" if self.running else "normal")
        for b in self.toolbar_buttons:
            b.configure(state="disabled" if self.running or self.route == "mapping" else "normal")
        if self.route != "workflow":
            return
        self.primary_button.pack(side="right")
        if self.running:
            self.cancel_button.pack(side="right", padx=8)
        if self.current_step == 2:
            self.report_button.pack(side="left", padx=8)
            self.folder_button.pack(side="left")
        title = ("필드 연결 확인 →", "입력 확인 · 검증으로 →", "복사본 저장" if self.preview_fingerprint else "전체 데이터 검증")[self.current_step]
        if self.current_step == 2 and self._saved(): title = "저장 완료 · 결과 폴더 열기"
        enabled = not self.running and (self.current_step != 0 or bool(self._config_text))
        self.primary_button.configure(text=title, state="normal" if enabled else "disabled")

    def _saved(self):
        return self.last_summary and not self.last_summary.get("preview") and self.last_summary["status"].startswith("completed")

    def _navigate(self, index):
        if self.running: return
        if self.route == "mapping":
            self.status.set("아래 ‘연결 확인 완료’를 누르면 입력 선택으로 이동합니다."); return
        if index == 0: self._show_step(0)
        elif index == 1:
            self._advance_inputs() if self.rule_ready else self._wizard()
        elif self.rule_ready: self._advance_review()
        else: self.status.set("먼저 프로젝트 규칙을 만들고 확정해 주세요.")

    def _primary(self):
        if self.current_step == 0: self._wizard()
        elif self.current_step == 1: self._advance_review()
        elif self._saved(): self._open_folder()
        else: self._start(self.preview_fingerprint is None)

    def _back(self):
        if self.running: return
        if self.route == "mapping" and self.mapping:
            self.mapping._close(); self.mapping = None
        self._show_step(self.current_step if self.route == "tools" else max(0, self.current_step-1))

    def _show_tools(self, tab):
        if self.running or self.route == "mapping":
            self.status.set("연결 확인 화면을 닫거나 실행을 마친 뒤 가이드를 열어 주세요."); return
        self.route = "tools"; self.steps.pack_forget(); self.tools_host.pack(fill="both", expand=True); self.tabs.select(tab)
        self.page_title.set("AI 가이드와 규칙 가져오기")
        self.page_caption.set("AI는 외부에서 규칙을 작성합니다. 프로그램은 로컬 파일만 처리합니다.")
        self._sync_actions()

    def _set_config(self, config):
        config = validate_config(config)
        self._config_text = json.dumps(config, ensure_ascii=False, indent=2)
        self.editor.delete("1.0", "end"); self.editor.insert("1.0", self._config_text); self.editor.edit_modified(False)
        self.profile_name.set(config["name"]); self.description.set(config.get("description", ""))
        self._invalidate_rule(); self._summarize_rule(config)
        self.edit_rule_button.configure(state="normal"); self.save_rule_button.configure(state="normal")
        self._sync_actions()
        if self.route == "workflow":
            self._show_step(0)

    def _get_config(self):
        if not self._config_text: raise ValueError("프로젝트 규칙을 먼저 만들거나 불러오세요.")
        return validate_config(json.loads(self._config_text))

    def _draft_config(self):
        docs = parse_json(self.editor.get("1.0", "end"))
        if len(docs) != 1: raise ValueError("규칙은 JSON 객체 하나여야 합니다.")
        return validate_config(docs[0])

    def _apply_json(self):
        try:
            self._set_config(self._draft_config()); self._show_step(0); self._wizard()
        except (ValueError, TypeError, KeyError) as exc: self.status.set(f"JSON 확인: {exc}")

    def _check_config(self):
        try: self.status.set(f"문법 확인 완료: {self._draft_config()['name']} · ‘JSON 적용’을 눌러 반영하세요.")
        except (ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 확인: {exc}")

    def _summarize_rule(self, config):
        self.rule_title.set(config["name"])
        checks = sum(len(config.get(k, [])) for k in ("include_when", "checks", "assertions"))
        connections = profile_connections(config)
        self.rule_summary.set(f"수정 필드 {sum(r['kind']=='field' for r in connections)}개 · 목록 {sum(r['kind']=='items' for r in connections)}개 · 레코드 조건 / 검증 {checks}개\n원본 대상: {config['source'].get('glob', '**/*.json')}\n‘필드 연결 확인’에서 기준·수정값·목록·검사를 구분해 볼 수 있습니다.")

    def _invalidate_preview(self):
        self.preview_fingerprint = self.last_summary = None
        for var in self.metric_values.values(): var.set("—")
        self.status_table.delete(*self.status_table.get_children())
        self.review_note.set("현재 설정으로 전체 데이터를 검증해 주세요.")
        self.report_button.configure(state="disabled"); self.folder_button.configure(state="disabled")
        self._sync_actions()

    def _invalidate_rule(self):
        self.rule_ready = False; self._invalidate_preview()
        self.steps.tab(1, state="disabled"); self.steps.tab(2, state="disabled")

    def _advance_inputs(self):
        if self.running: return
        try:
            self._summarize_rule(self._get_config()); self.rule_ready = True
            self.steps.tab(1, state="normal"); self._show_step(1)
            self.status.set("규칙이 확정되었습니다. 전체 입력 범위와 저장 위치를 확인하세요."); self._save_settings()
        except (ValueError, TypeError, KeyError) as exc: self.status.set(f"규칙 확인: {exc}")

    def _advance_review(self):
        if self.running: return
        try:
            self._check_inputs()
            pattern = self._get_config()["source"].get("glob", "**/*.json")
            if not any(p.is_file() for p in Path(self.source.get()).glob(pattern)):
                raise ValueError(f"원본 폴더에서 대상 패턴 ‘{pattern}’에 맞는 파일을 찾지 못했습니다.")
            self.run_summary.set(f"규칙  {self._get_config()['name']}\n원본  {self.source.get()}\n결과  {len(self.result_paths):,}개 파일\n저장  {self.output.get()}\n분리  {'변경 / 미변경 / examine' if self.split_output.get() else '전체 복사본 + examine'}")
            self.steps.tab(2, state="normal"); self._show_step(2)
            self.status.set("전체 데이터 검증 후 집계와 보고서를 확인하세요."); self._save_settings()
        except (ValueError, OSError) as exc: self.status.set(f"입력 확인: {exc}")

    def _load_profile(self):
        if self.running: return
        path = filedialog.askopenfilename(title="AI / 저장된 프로젝트 규칙", filetypes=[("규칙 JSON", "*.json")], parent=self.root)
        if path:
            try:
                config = load_config(Path(path))
                self._new_project()
                self._set_config(config)
                self._wizard()
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
        if self.running or (self.mapping and not self.mapping.closed): return
        try: config = self._get_config()
        except (ValueError, TypeError, KeyError) as exc: self.status.set(str(exc)); return
        self.current_step, self.route = 0, "mapping"
        self.steps.pack_forget(); self.tools_host.pack_forget(); self.mapping_host.pack(fill="both", expand=True)
        self.page_title.set("AI 규칙 · 필드 연결 확인")
        self.page_caption.set(config["name"] + " · 읽기 전용 · 한 쌍 프리뷰 확인 가능 · 전체 데이터 검증은 다음 단계에서 진행합니다.")
        self._sync_actions()
        self.footer.pack_forget()
        self.mapping = RuleReview(self.mapping_host, config, self._finish_mapping,
                                  lambda: self._show_step(0), source_hint=self.source.get(), result_hints=self.result_paths)

    def _finish_mapping(self):
        self.mapping = None; self._advance_inputs()

    def _new_project(self):
        if self.running or self.route == "mapping":
            self.status.set("연결 확인 화면을 닫거나 실행을 마친 뒤 새 프로젝트를 시작하세요.")
            return
        self._config_text = ""
        self.editor.delete("1.0", "end")
        self.mapping_source = self.mapping_result = ""
        self.source.set(""); self.output.set(""); self.result_paths = []; self._render_results()
        self.rule_ready = False
        self.steps.tab(1, state="disabled"); self.steps.tab(2, state="disabled")
        self.rule_title.set("규칙을 준비해 주세요"); self.rule_summary.set("아직 프로젝트 규칙이 없습니다.")
        self.edit_rule_button.configure(state="disabled"); self.save_rule_button.configure(state="disabled")
        self.input_hint.set("실제로 처리할 전체 원본 폴더와 결과 파일을 지정하세요.")
        self.last_run = None
        self._show_step(0)
        self.status.set("새 프로젝트를 시작합니다. 기존 데이터와 결과 폴더는 유지됩니다.")

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
        text += "\n검토·오류·짝이 없는 항목은 examine에서 확인하세요. 처리 결과서에서 모든 분류를 검색할 수 있습니다."
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
                self.steps.tab(0, state="normal"); self.steps.tab(1, state="normal")
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
            self.mapping_source, self.mapping_result = s.get("mapping_source", ""), s.get("mapping_result", "")
            if s.get("config"): self._set_config(s["config"])
            elif s.get("profile") in self.profiles: self._set_config(self.profiles[s["profile"]][1])
            if self._config_text: self.status.set("최근 작업의 규칙과 입력 위치를 복원했습니다. 확인 후 계속하세요.")
        except (OSError, ValueError, TypeError, KeyError): pass

    def _save_settings(self):
        try:
            s = {"source": self.source.get(), "output": self.output.get(), "results": self.result_paths, "split_output": self.split_output.get(),
                 "mapping_source": self.mapping_source, "mapping_result": self.mapping_result, "config": self._get_config() if self._config_text else None}
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
