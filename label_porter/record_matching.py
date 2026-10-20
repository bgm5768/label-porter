"""Optional one-record field matching, backed by the normal rule engine."""
from __future__ import annotations

import copy
import json
import tkinter as tk
from tkinter import scrolledtext, ttk

from .formats import DataError, canonical_key
from .mapping import (SampleRows, collect_fields, preview_profile, row_metadata,
                      _children, _context, _wire_expr)
from .mapping_ui import MappingWindow
from .rules import expression


class RecordView:
    """Keep selected record provenance without changing the input collection."""
    def __init__(self, rows, index):
        self.meta = row_metadata(rows, index)
        self.selected = SampleRows([rows[index]], [self.meta])

    def rows(self, pointer="", record_mode="auto"):
        return self.selected


def refresh_examples(scope, source, result, meta=None):
    """Keep partial wiring while replacing examples, including nested arrays."""
    scope.source_rows, scope.result_rows, scope.meta = source, result, meta
    for side, rows in (("source", source), ("result", result)):
        old = getattr(scope, side + "_fields")
        fresh = collect_fields(rows, meta if side == "source" else None)
        for wire in scope.wires:
            path = getattr(wire, side)
            if path not in fresh:
                info = copy.deepcopy(old[path])
                info.examples, info.count = [], 0
                fresh[path] = info
        setattr(scope, side + "_fields", fresh)
    scope._scores.clear()
    for wire in scope.wires:
        if wire.child:
            refresh_examples(wire.child,
                             _children(source, {"path": wire.source}, "source"),
                             _children(result, (wire.raw_rule or {}).get("from", wire.result), "result"))


class RecordMatching(MappingWindow):
    def __init__(self, parent, on_apply, **kwargs):
        self.source_number = tk.StringVar(parent, value="1")
        self.result_number = tk.StringVar(parent, value="1")
        self.pair_info = tk.StringVar(parent, value="자료를 읽은 뒤 비교할 항목을 선택하세요.")
        self.source_rows = self.result_rows = None
        self.active_pair = (0, 0)
        self.connection_tools = tk.BooleanVar(parent, value=False)
        self.scroll_board = True
        super().__init__(parent, on_apply, **kwargs)
        self.name.set("직접 연결한 프로젝트 규칙")

    def _build(self):
        super()._build()
        panel = ttk.Frame(self.board_page)
        panel.pack(fill="x", before=self.board_page.winfo_children()[0], pady=(0, 10))
        row = ttk.Frame(panel); row.pack(fill="x")
        self.record_spins = []
        for label, variable in (("원본 항목", self.source_number), ("결과 항목", self.result_number)):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 6))
            spin = ttk.Spinbox(row, from_=1, to=1, textvariable=variable, width=7)
            spin.pack(side="left", padx=(0, 12))
            spin.bind("<Return>", self._select_pair)
            self.record_spins.append(spin)
        ttk.Button(row, text="이 한 건 비교", command=self._select_pair).pack(side="left")
        ttk.Button(row, text="양쪽 JSON 보기", command=self._show_pair).pack(side="left", padx=8)
        more = ttk.Frame(panel); more.pack(fill="x", pady=(6, 0))
        self.find_result_button = ttk.Button(more, text="연결한 키로 같은 결과 찾기", command=self._find_result)
        self.find_result_button.pack(side="left")
        ttk.Checkbutton(more, text="연결 도구 · 이름 / 검색 / 기준 변경", variable=self.connection_tools,
                        command=self._toggle_connection_tools).pack(side="left", padx=12)
        ttk.Label(panel, textvariable=self.pair_info, wraplength=760, foreground="#62728a").pack(fill="x", pady=(6, 0))
        self.test_button.configure(text="선택한 원본 1건 시험 적용")
        self.review_caption.configure(text="선택한 원본 한 건의 연결키로 결과 전체에서 같은 건을 찾습니다.\n읽은 자료의 중복 키도 검사합니다. 실제 저장 전에는 전체 데이터 검증을 거칩니다.")
        self.canvas.configure(height=360)
        self._toggle_connection_tools()

    def _toggle_connection_tools(self):
        frames = (self.heading_frame, self.trail_frame, self.search_frame)
        if self.connection_tools.get():
            for frame in frames:
                frame.pack(fill="x", before=self.connection_actions, pady=(0, 6))
        else:
            for frame in frames:
                frame.pack_forget()
            self.completed_frame.pack_forget()
            self.connection_list_open.set(False)

    def _scope_samples(self, sp, rp):
        self.source_rows = self.source_sample.rows(sp, (self.profile or {}).get("source", {}).get("record_mode", "auto"))
        self.result_rows = self.result_sample.rows(rp, (self.profile or {}).get("result", {}).get("record_mode", "auto"))
        self.active_pair = (0, 0)
        self.source_number.set("1"); self.result_number.set("1")
        for spin, rows in zip(self.record_spins, (self.source_rows, self.result_rows)):
            spin.configure(to=len(rows))
        return RecordView(self.source_rows, 0), RecordView(self.result_rows, 0)

    def _reset_scope(self):
        super()._reset_scope()
        if self.root_scope and not self.dirty:
            self._pair_label()

    def _pair_label(self):
        si, ri = self.active_pair
        source_file = row_metadata(self.source_rows, si).get("relative_path", "")
        self.pair_info.set(
            f"원본 {si + 1:,} / {len(self.source_rows):,}건 · {source_file}  ↔  결과 {ri + 1:,} / {len(self.result_rows):,}건")
        self.sample_info.set(f"원본 {self.source_sample.file_count:,}개 파일 / {len(self.source_rows):,}건 · "
                             f"결과 {self.result_sample.file_count:,}개 파일 / {len(self.result_rows):,}건 읽음")
        self.status.set("한 건의 값을 보며 같은 건 찾기 → 바꿀 값 연결 → 시험 적용 순서로 진행하세요.")

    def _select_pair(self, event=None):
        if self.busy or self.dirty or self.source_rows is None:
            return
        try:
            si, ri = int(self.source_number.get()) - 1, int(self.result_number.get()) - 1
            if not (0 <= si < len(self.source_rows) and 0 <= ri < len(self.result_rows)):
                raise ValueError
        except ValueError:
            self.status.set("항목 번호는 읽은 건수 범위 안의 정수로 입력하세요.")
            return
        source, result = RecordView(self.source_rows, si), RecordView(self.result_rows, ri)
        refresh_examples(self.root_scope, source.rows(), result.rows(), source.meta)
        self.active_pair = (si, ri)
        self.scope, self.history = self.root_scope, []
        self.selected = self.pending = None
        self.query.set("")
        self._invalidate(); self._refresh(); self._pair_label()

    def _preview_report(self, config):
        return preview_profile(config, self.source_rows, self.result_rows, self.source_sample.meta,
                               sample_indices=[self.active_pair[0]])

    def _find_result(self):
        if self.busy or self.dirty or self.source_rows is None:
            return
        try:
            key = next((wire for wire in self.root_scope.wires if wire.kind == "key"), None)
            if key is None:
                raise DataError("먼저 같은 건을 찾을 ID 또는 파일명을 양쪽에서 연결하세요.")
            originals = key.original_keys or (None, None)
            source_key = _wire_expr(self.root_scope, key, "source", originals[0])
            result_key = _wire_expr(self.root_scope, key, "result", originals[1])
            si = self.active_pair[0]
            wanted = canonical_key(expression(source_key, _context(self.source_rows[si], row_metadata(self.source_rows, si))))
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(f"연결키 확인: {exc}")
            return
        def work():
            matches, invalid = [], 0
            for index, row in enumerate(self.result_rows):
                if self.closed:
                    raise DataError("같은 결과 찾기를 중단했습니다.")
                try:
                    if canonical_key(expression(result_key, _context(row, row_metadata(self.result_rows, index)))) == wanted:
                        matches.append(index)
                except (ValueError, TypeError, KeyError):
                    invalid += 1
            return matches, invalid
        def complete(found):
            matches, invalid = found
            if len(matches) == 1:
                self.source_number.set(str(si + 1)); self.result_number.set(str(matches[0] + 1))
                self._select_pair()
                self.status.set(f"같은 키의 결과 {matches[0] + 1:,}번을 찾았습니다. 바꿀 값을 연결한 뒤 시험 적용하세요.")
            else:
                self.status.set("같은 키의 결과가 없습니다." if not matches else f"같은 키의 결과가 {len(matches):,}건입니다. 중복을 확인하세요.")
            if invalid:
                self.status.set(self.status.get() + f" · 결과 {invalid:,}건은 키를 읽을 수 없습니다.")
        self.status.set("연결한 키로 같은 결과를 찾고 있습니다…")
        self._async(work, complete)

    def _preview_note(self):
        return (f"선택한 원본 {self.active_pair[0] + 1:,}번 한 건을 시험 적용했습니다. "
                "중복 키는 읽은 원본·결과 전체에서 검사합니다. 행 번호로 강제 연결하지 않습니다.")

    def _show_pair(self):
        if self.busy or self.dirty or self.source_rows is None:
            self.status.set("먼저 원본과 결과 자료를 읽어 주세요.")
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("선택한 한 건 · 원본 / 보완 결과")
        dialog.geometry("1000x650")
        dialog.transient(self.window)
        panes = ttk.Panedwindow(dialog, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=12)
        for label, rows, index in (("원본", self.source_rows, self.active_pair[0]),
                                   ("보완 결과", self.result_rows, self.active_pair[1])):
            panel = ttk.Frame(panes); panes.add(panel, weight=1)
            ttk.Label(panel, text=f"{label} · {index + 1:,}번").pack(anchor="w", pady=6)
            box = scrolledtext.ScrolledText(panel, wrap="none", font=("Consolas", 10), width=40)
            box.pack(fill="both", expand=True)
            text = json.dumps(rows[index], ensure_ascii=False, indent=2)
            box.insert("1.0", text[:200000] + ("\n… 표시 한도 200,000자 (데이터는 그대로 유지)" if len(text) > 200000 else ""))
            box.configure(state="disabled")

    def _apply(self):
        if not self._checked():
            self.status.set("선택한 원본 한 건의 시험 적용에 성공한 뒤 규칙을 사용하세요.")
            return
        super()._apply()

    def _checked(self):
        if self.busy or self.dirty or self.last_checked is None:
            return False
        try:
            return self.last_checked == json.dumps(self._config(), sort_keys=True, ensure_ascii=False)
        except (ValueError, TypeError, KeyError):
            return False

    def _update_apply(self):
        super()._update_apply()
        if not self._checked():
            self.apply_button.configure(state="disabled")
