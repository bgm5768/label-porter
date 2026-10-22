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
from .input_files import folder_files, source_paths
from .input_check import InputRequest, inspect_inputs, selection_paths

AI_GUIDE_FILENAME = "AI_규칙작성_가이드.md"


def resource_directory():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def default_output_directory():
    # A frozen app's resources live in a temporary _MEIPASS folder, not beside its EXE.
    program = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return program / "results"


class Workflow:
    def _session_ready(self):
        panel = self.mapping.preview if self.mapping and not self.mapping.closed else None
        return bool(panel and panel.ready_for_navigation)

    def _preview_state_changed(self):
        panel = self.mapping.preview if self.mapping and not self.mapping.closed else None
        if panel is None: return
        if self.running and not self.preparing: return
        if self.preview_generation != panel.generation:
            self.preview_generation = panel.generation
            self.preview_fingerprint = self.last_summary = self.last_run = None
            self.connection_confirmed = False
            for var in self.metric_values.values(): var.set('—')
            self.status_table.delete(*self.status_table.get_children())
            self.report_button.configure(state='disabled')
            self.folder_button.configure(state='disabled')
        was_preparing = self.preparing
        self.running = self.preparing = panel.navigation_pending
        if self.preparing:
            self.cancel_event = panel.cancel_event
            if not was_preparing:
                self.preparation_progress.configure(mode='indeterminate')
                self.preparation_progress.start()
        elif was_preparing:
            self.preparation_progress.stop()
        self.status.set(panel.status.get())
        self._sync_actions()

    def _run_validation(self, preview):
        if self.running: return
        try:
            self._check_inputs()
            request = self._input_request()
            if not preview and self.preview_fingerprint is None:
                raise ValueError('4단계에서 전체 데이터 검증을 먼저 실행하세요.')
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(f'입력 확인: {exc}')
            return
        expected = self.preview_fingerprint
        owned = tuple(self.owned_outputs)
        self.preview_fingerprint = None
        self.running = self.checking_inputs = True
        self.active_request = request
        self.cancel_event = cancel = threading.Event()
        self.report_button.configure(state='disabled')
        self.folder_button.configure(state='disabled')
        self.preview_button.configure(state='disabled')
        self.status.set('전체 데이터 검증을 시작합니다.' if preview else '최종 복사본을 저장합니다.')
        self._save_settings()
        self._sync_actions()
        events = self.queue
        def work():
            from .engine import run, Cancelled
            try:
                def checkpoint():
                    if cancel.is_set(): raise Cancelled('작업을 중단했습니다.')
                results, stamp = inspect_inputs(request, checkpoint, stamp=True, exclude_result_directories=owned)
                if not preview and stamp != expected:
                    raise ValueError('입력이 변경되었습니다. 전체 데이터 검증을 다시 실행하세요.')
                events.put(('run_fingerprint', stamp))
                result = run(request.source, results, request.output, request.config, preview=preview,
                             progress=lambda event: events.put(('progress', event)), cancel=cancel,
                             split_output=request.split)
                events.put(('owned_output', result.directory))
                if preview and result.summary['status'].startswith('completed'):
                    _, current = inspect_inputs(request, checkpoint, stamp=True,
                                                exclude_result_directories=(*owned, result.directory))
                    if current != stamp:
                        raise ValueError('검증 도중 입력이 변경되었습니다. 전체 데이터 검증을 다시 실행하세요.')
                events.put(('complete', result))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True, name='validate-or-save').start()

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
        """Check selections only. Filesystem validation belongs to the worker."""
        if not self.rule_ready:
            raise ValueError("1단계에서 프로젝트 규칙을 먼저 준비하세요.")
        if not self.source.get().strip() and not self.source_files:
            raise ValueError("원본 라벨 폴더 또는 파일을 선택하세요.")
        if not self.result_folder and not self.result_paths:
            raise ValueError("실제 처리할 작업 결과 파일을 선택하세요.")
        if not self.output.get().strip():
            self.output.set(str(default_output_directory()))

    def _input_request(self):
        return InputRequest(self._get_config(), self._source_input(), tuple(self.result_paths),
                            self.result_folder, self.output.get(), self.split_output.get())

    def _prepare_mapping(self):
        try:
            self._check_inputs()
            request = self._input_request()
        except (ValueError, TypeError, KeyError) as exc:
            if self.rule_ready: self._show_step(1)
            self.status.set(f"입력 확인: {exc}")
            return
        self.running = True
        self.checking_inputs = True
        self.cancel_event = cancel = threading.Event()
        self.status.set("입력 파일을 확인하고 있습니다… 다른 작업을 계속하거나 중단할 수 있습니다.")
        self._sync_actions()
        events = self.queue
        owned = tuple(self.owned_outputs)
        def work():
            try:
                def checkpoint():
                    if cancel.is_set(): raise InterruptedError("입력 확인을 중단했습니다.")
                results, _ = inspect_inputs(request, checkpoint, exclude_result_directories=owned)
                events.put(("mapping_ready", (request, results)))
            except Exception as exc:
                events.put(("input_error", str(exc)))
        threading.Thread(target=work, daemon=True, name="input-check").start()

    def _choose_source(self):
        path = filedialog.askdirectory(title="보완 전 원본 라벨 폴더 선택", parent=self.root)
        if path:
            self.source_files = []
            self.source.set(path)
            self._render_sources()

    def _source_input(self):
        return tuple(self.source_files) if self.source_files else self.source.get()

    def _source_label(self):
        return f'선택한 원본 파일 {len(self.source_files):,}개' if self.source_files else self.source.get()

    def _result_files(self):
        return [str(p) for p in folder_files(self.result_folder)] if self.result_folder else list(self.result_paths)

    def _choose_source_files(self):
        paths = filedialog.askopenfilenames(title="원본 라벨 파일 다중 선택", parent=self.root,
            filetypes=[("JSON", "*.json *.jsonl *.ndjson"), ("모든 파일", "*.*")])
        if paths:
            self.source_files = selection_paths([*self.source_files, *paths])
            self.source.set(''); self._render_sources()

    def _choose_result_folder(self):
        path = filedialog.askdirectory(title="플랫폼 결과 또는 보완 데이터 폴더 선택", parent=self.root)
        if path:
            self.result_folder = path; self.result_paths = []; self._render_results()

    def _remove_source(self):
        selected = self.source_list.curselection()
        if not selected: return
        if self.source.get(): self.source.set('')
        else:
            for index in reversed(selected): del self.source_files[index]
        self._render_sources()

    def _render_sources(self):
        self.source_panel.display(self.source.get(), self.source_files)
        self.source_count.set('폴더 선택 · 규칙의 파일 패턴 적용 · 파일 다중 선택 시 선택 파일만 처리' if self.source.get() else
                              f'{len(self.source_files):,}개 파일 선택 · 파일 추가 선택 가능')
        self._invalidate_preview()

    def _choose_output(self):
        path = filedialog.askdirectory(title="결과 저장 위치 선택", initialdir=self.output.get(), parent=self.root)
        if path:
            self.output.set(path)
            self.output_mode = "custom"

    def _reset_output(self):
        self.output.set(str(default_output_directory()))
        self.output_mode = "default"
        self.status.set("저장 위치를 프로그램 폴더의 results로 변경했습니다.")

    def _output_changed(self, *_):
        value = self.output.get().strip()
        self.output_mode = "default" if not value or os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(default_output_directory())) else "custom"

    def _choose_results(self):
        paths = filedialog.askopenfilenames(title="플랫폼 결과 또는 보완 파일 다중 선택", parent=self.root, filetypes=[("JSON / CSV", "*.json *.jsonl *.ndjson *.csv"), ("모든 파일", "*.*")])
        if not paths: return
        self.result_folder = ''
        self.result_paths = selection_paths([*self.result_paths, *paths])
        self._render_results()

    def _remove_results(self):
        selected = self.result_list.curselection()
        if not selected: return
        if self.result_folder: self.result_folder = ''
        else:
            for index in reversed(selected): del self.result_paths[index]
        self._render_results()

    def _render_results(self):
        self._invalidate_preview()
        self.result_panel.display(self.result_folder, self.result_paths)
        self.result_count.set('폴더 선택 · JSON / JSONL / NDJSON / CSV · 하위 폴더 포함' if self.result_folder else
                              f'{len(self.result_paths):,}개 파일 선택 · 파일 추가 선택 가능')

    def _cancel(self):
        if self.preparing and self.mapping:
            self.mapping.preview.cancel()
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("입력 확인을 중단하고 있습니다. 데이터는 저장하지 않습니다." if getattr(self, 'checking_inputs', False)
                        else "현재 처리 단계가 끝나는 대로 중단하고 보고서를 저장합니다.")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_report(self):
        if self.last_run:
            webbrowser.open((self.last_run / "reports" / "report.html").as_uri())

    def _open_excel_report(self):
        if self.last_run:
            path = self.last_run / "reports" / "report.xlsx"
            if path.is_file():
                if hasattr(os, "startfile"):
                    os.startfile(str(path))
                else:
                    webbrowser.open(path.as_uri())

    def _open_folder(self):
        if self.last_run:
            if hasattr(os, "startfile"):
                os.startfile(str(self.last_run))
            else:
                webbrowser.open(self.last_run.as_uri())
