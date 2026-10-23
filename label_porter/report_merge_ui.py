"""Inline, background-only Excel report merger."""
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from . import widgets as ttk
from .ui_lifecycle import UiEventQueue, release_variables


class ReportMergePanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=18)
        self.paths, self.busy, self.closed, self.saved = [], False, False, None
        self.events, self.cancel, self.job = UiEventQueue(), threading.Event(), None
        self.message = tk.StringVar(value="결과서를 선택하고 저장할 Excel 이름을 지정하세요. 한 개만 선택하면 새 양식으로 다시 작성합니다.")
        self.fields = {}
        ttk.Label(self, text="여러 처리 결과를 하나의 보완 결과서로", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(self, text="라벨 포터 Excel(.xlsx) · 이전 결과는 reports/summary.json 선택\n작업별 집계·전후 값·검토 사유를 보존합니다. 원본 JSON 수정과 여러 룰 실행은 별도 작업입니다.", wraplength=880).pack(anchor="w")
        bar = ttk.Frame(self); bar.pack(fill="x", pady=12)
        self.add = ttk.Button(bar, text="결과서 추가…", command=self._choose); self.add.pack(side="left")
        self.remove = ttk.Button(bar, text="선택 제거", command=self._remove); self.remove.pack(side="left", padx=8)
        box = ttk.Frame(self); box.pack(fill="both", expand=True)
        self.list = tk.Listbox(box, selectmode="extended", height=7, exportselection=False)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); self.list.pack(fill="both", expand=True)
        ttk.Label(self, text="보고서 표시 정보 (선택 입력)", style="CardTitle.TLabel").pack(anchor="w", pady=(16, 8))
        for key, title, default in (("title", "보고서 제목", "통합 보완 결과서"), ("project_id", "과제 데이터번호", ""), ("dataset", "데이터명", ""), ("stage", "보완 단계", ""), ("quality", "품질특성", "")):
            row = ttk.Frame(self); row.pack(fill="x", pady=3)
            ttk.Label(row, text=title, width=140).pack(side="left")
            var = self.fields[key] = tk.StringVar(value=default)
            ttk.Entry(row, textvariable=var).pack(fill="x", expand=True)
        ttk.Label(self, text="같은 실행은 한 번만 포함합니다. 서로 다른 실행의 같은 KEY는 모두 유지하며 값 차이를 표시합니다.\nExcel 표를 수동 편집한 값은 병합에 반영되지 않습니다. 파일에 내장된 원 실행 데이터를 사용합니다.", wraplength=880).pack(anchor="w", pady=12)
        ttk.Label(self, textvariable=self.message, wraplength=880).pack(fill="x", pady=8)
        self.progress = ttk.Progressbar(self, mode="indeterminate"); self.progress.pack(fill="x", pady=6)
        actions = ttk.Frame(self); actions.pack(fill="x")
        self.save = ttk.Button(actions, text="통합 Excel 저장…", command=self._save, style="Primary.TButton"); self.save.pack(side="left")
        self.stop = ttk.Button(actions, text="중단", command=self.cancel.set, state="disabled"); self.stop.pack(side="left", padx=8)
        self.open = ttk.Button(actions, text="저장한 결과서 열기", command=self._open, state="disabled"); self.open.pack(side="left")
        self.bind("<Destroy>", self._destroyed, add="+")

    def _choose(self):
        if self.busy: return
        paths = filedialog.askopenfilenames(parent=self, title="병합할 처리 결과서 선택", filetypes=[("라벨 포터 결과서", "*.xlsx *.json *.jsonl")])
        for path in paths:
            if path not in self.paths: self.paths.append(path); self.list.insert("end", path)

    def _remove(self):
        if self.busy: return
        for index in reversed(self.list.curselection()): del self.paths[index]; self.list.delete(index)

    def _save(self):
        if self.busy: return
        if not self.paths: self.message.set("결과서를 한 개 이상 추가하세요."); return
        output = filedialog.asksaveasfilename(parent=self, title="통합 Excel 저장", initialfile="통합_보완결과서.xlsx", defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not output: return
        self.start(tuple(self.paths), output, {k: v.get().strip() for k, v in self.fields.items()})

    def start(self, paths, output, metadata):
        if self.busy or self.closed: return
        from .report_data import merge_reports
        self.busy = True; self.saved = None; self.cancel.clear()
        for button in (self.add, self.remove, self.save, self.open): button.configure(state="disabled")
        self.stop.configure(state="normal"); self.progress.start()
        events, cancel = self.events, self.cancel
        def work():
            try: events.put(("done", merge_reports(paths, output, metadata, lambda value: events.put(("progress", value)), cancel)))
            except Exception as exc: events.put(("error", str(exc)))
        # On window close, finish cancellation/temporary-file cleanup before process exit.
        threading.Thread(target=work, daemon=False, name="excel-report-merge").start()
        self.job = self.after(100, self._poll)

    def _poll(self):
        self.job = None
        if self.closed: return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress": self.message.set(payload); continue
                self.busy = False; self.progress.stop(); self.stop.configure(state="disabled")
                for button in (self.add, self.remove, self.save): button.configure(state="normal")
                if kind == "done":
                    self.saved = Path(payload["path"]); self.open.configure(state="normal")
                    self.message.set(f"{payload['runs']}개 작업 저장 완료 · 동일 작업 {payload['duplicates_skipped']}개 제외\n{self.saved}")
                else: self.message.set("저장하지 못했습니다. " + payload)
        except queue.Empty: pass
        if self.busy: self.job = self.after(100, self._poll)

    def _open(self):
        if self.saved:
            try: os.startfile(str(self.saved))
            except OSError as exc: self.message.set(f"Excel을 열지 못했습니다: {exc}")

    def _destroyed(self, event):
        if event.widget is not self: return
        self.closed = True; self.cancel.set(); self.events.close()
        if self.job:
            try: self.after_cancel(self.job)
            except tk.TclError: pass
        self.fields.clear()
        release_variables(self)
