"""Shared local file operations and asynchronous porting workflow."""
from __future__ import annotations
import json
import os
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, scrolledtext
from .engine import run

AI_GUIDE_FILENAME = "AI_규칙작성_가이드.md"


def resource_directory():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def default_output_directory():
    # A frozen app's resources live in a temporary _MEIPASS folder, not beside its EXE.
    program = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return program / "results"


class Workflow:
    def _document_view(self, frame, text):
        from .markdown_viewer import MarkdownViewer
        return MarkdownViewer(frame, text, navigate=lambda index: self.tabs.select(index))

    def _copy_ai_guide(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.ai_guide_text)
            self.status.set("AI 가이드 전체를 복사했습니다. AI 대화에 원본 라벨과 플랫폼 결과를 함께 전달하세요.")
        except tk.TclError as exc:
            self.status.set(f"복사 실패: {exc}")

    def _save_ai_guide(self):
        path = filedialog.asksaveasfilename(title="AI에게 전달할 가이드 저장", initialfile=AI_GUIDE_FILENAME,
                                          defaultextension=".md", filetypes=[("Markdown", "*.md")], parent=self.root)
        if path:
            try:
                Path(path).write_text(self.ai_guide_text, encoding="utf-8")
                self.status.set(f"AI 가이드 저장: {path}")
            except OSError as exc:
                self.status.set(f"가이드 저장 실패: {exc}")

    def _check_inputs(self):
        if not self.rule_ready:
            raise ValueError("1단계에서 프로젝트 규칙을 먼저 준비하세요.")
        if not self.source.get().strip() or not Path(self.source.get()).is_dir():
            raise ValueError("전체 원본 라벨이 있는 폴더를 선택하세요.")
        if not self.result_paths or any(not Path(path).is_file() for path in self.result_paths):
            raise ValueError("실제 처리할 작업 결과 파일을 선택하세요.")
        if not self.output.get().strip():
            self.output.set(str(default_output_directory()))
        source, output = Path(self.source.get()).resolve(), Path(self.output.get()).resolve()
        if output == source or output.is_relative_to(source):
            raise ValueError("저장 위치는 원본 폴더 밖으로 선택하세요.")
        if output.exists() and not output.is_dir():
            raise ValueError("저장 위치가 파일입니다. 폴더를 선택하세요.")

    def _fingerprint(self):
        result_stats = [(str(Path(path).resolve()), Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in self.result_paths]
        return json.dumps([self._get_config(), str(Path(self.source.get()).resolve()), result_stats,
                           str(Path(self.output.get()).resolve()), self.split_output.get()], ensure_ascii=False, sort_keys=True)

    def _choose_source(self):
        path = filedialog.askdirectory(title="보완 전 라벨 JSON이 있는 폴더 선택")
        if path:
            self.source.set(path)

    def _choose_output(self):
        path = filedialog.askdirectory(title="실행 결과를 저장할 상위 폴더")
        if path:
            self.output.set(path)

    def _choose_results(self):
        paths = filedialog.askopenfilenames(title="어드민 다운로드 결과 선택", filetypes=[("JSON / CSV", "*.json *.jsonl *.ndjson *.csv"), ("모든 파일", "*.*")])
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved not in self.result_paths:
                self.result_paths.append(resolved)
        self._render_results()

    def _remove_results(self):
        for index in reversed(self.result_list.curselection()):
            del self.result_paths[index]
        self._render_results()

    def _render_results(self):
        self._invalidate_preview()
        self.result_list.delete(0, "end")
        for path in self.result_paths:
            self.result_list.insert("end", path)

    def _start(self, preview):
        if self.running:
            return
        try:
            config = self._get_config()
            self._check_inputs()
            fingerprint = self._fingerprint()
            if not preview and self.preview_fingerprint != fingerprint:
                raise ValueError("현재 규칙과 입력으로 검증 미리보기를 먼저 실행하세요.")
        except Exception as exc:
            self.status.set(f"입력 확인: {exc}")
            return
        source, results, output = self.source.get(), list(self.result_paths), self.output.get()
        split_output = self.split_output.get()
        self._save_settings()
        self.running = True
        self.active_fingerprint = fingerprint
        self.preview_fingerprint = None
        self.cancel_event.clear()
        self.preview_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.report_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.steps.select(self.review_step)
        for index in range(self.review_step): self.steps.tab(index, state="disabled")
        self.status.set("검증 미리보기를 시작합니다." if preview else "새 복사본에 반영을 시작합니다.")
        self._append_log(f"\n{'미리보기' if preview else '복사본 반영'} · {config['name']}\n원본: {source}\n")
        def work():
            try:
                result = run(source, results, output, config, preview, lambda event: self.queue.put(("progress", event)), self.cancel_event, split_output=split_output)
                self.queue.put(("complete", result))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("현재 처리 단계가 끝나는 대로 중단하고 보고서를 저장합니다.")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_report(self):
        if self.last_run:
            webbrowser.open((self.last_run / "reports" / "report.html").as_uri())

    def _open_folder(self):
        if self.last_run:
            if hasattr(os, "startfile"):
                os.startfile(str(self.last_run))
            else:
                webbrowser.open(self.last_run.as_uri())
