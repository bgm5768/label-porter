"""Searchable Excel companion to the offline HTML processing report.

Read the report streams once, write rows in order, and keep the workbook from
holding a second copy of every JSON record in memory.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path

import xlsxwriter

from .examine import CATEGORIES as EXAMINE_CATEGORIES
from .result_dashboard import CATEGORIES


MAX_ROWS = 1_048_576
MAX_CELL = 32_767
MAX_LINKS = 65_530
SCOPES = {"source": "원본", "result": "보완", "source_file": "원본 파일", "change": "변경 필드"}
SIDES = {"source": "원본 입력", "result": "보완 입력", "output_before": "처리 대상 원본",
         "output": "처리 후", "completed": "정상 처리", "examine": "examine 대상"}
DETAIL_LINK = re.compile(r"(?:detail\.html\?chunk=\d{6}&id=\d{8}|review/\d{6}\.html)\Z")


def _text(value, limit=MAX_CELL):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(value) > limit:
        return value[:limit - 61] + " … [Excel 셀 제한으로 생략 · 같은 폴더의 JSONL에서 전체 값 확인]"
    return value


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _json_field(item, key):
    return _json(item[key]) if key in item else ""


def _rows(path):
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


class _SheetRows:
    def __init__(self, workbook, name, headers, widths, formats, *, max_rows=MAX_ROWS, link_column=None):
        self.book, self.name, self.headers, self.widths = workbook, name, headers, widths
        self.formats, self.max_rows, self.link_column = formats, max_rows, link_column
        self.part = 0
        self.total = 0
        self.sheet = None
        self.row = 0
        self.links = 0
        self._next_sheet()

    def _next_sheet(self):
        if self.sheet is not None:
            self.sheet.autofilter(0, 0, max(0, self.row - 1), len(self.headers) - 1)
        self.part += 1
        self.sheet = self.book.add_worksheet(self.name if self.part == 1 else f"{self.name}_{self.part}")
        self.sheet.hide_gridlines(2)
        self.sheet.freeze_panes(1, 2 if len(self.headers) > 4 else 0)
        self.sheet.set_zoom(85)
        self.sheet.set_tab_color("#244FC6" if self.part == 1 else "#8392A5")
        self.sheet.set_row(0, 27)
        for col, (header, width) in enumerate(zip(self.headers, self.widths)):
            self.sheet.set_column(col, col, width)
            self.sheet.write_string(0, col, header, self.formats["head"])
        self.row, self.links = 1, 0

    def append(self, values):
        if len(values) != len(self.headers):
            raise ValueError(f"{self.name}: 열 수가 맞지 않습니다.")
        if self.row >= self.max_rows:
            self._next_sheet()
        for col, value in enumerate(values):
            if value is None or value == "":
                continue
            if (col == self.link_column and isinstance(value, str) and DETAIL_LINK.fullmatch(value)
                    and self.links < MAX_LINKS):
                result = self.sheet.write_url(self.row, col, "external:" + value,
                                              self.formats["link"], value)
                if result == 0:
                    self.links += 1
                    continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                self.sheet.write_number(self.row, col, value, self.formats["number"])
            else:
                self.sheet.write_string(self.row, col, _text(value), self.formats["text"])
        self.row += 1
        self.total += 1

    def finish(self):
        self.sheet.autofilter(0, 0, max(0, self.row - 1), len(self.headers) - 1)


def _overview(book, summary, fmt):
    sheet = book.add_worksheet("요약")
    sheet.hide_gridlines(2)
    sheet.set_zoom(95)
    sheet.set_tab_color("#172B46")
    for col, width in enumerate((27, 22, 14, 82)):
        sheet.set_column(col, col, width)
    sheet.write_string(1, 0, "라벨 데이터 처리 결과", fmt["title"])
    state = {"completed": "처리 완료", "completed_with_issues": "처리 완료 · 확인 필요",
             "failed": "실패 · 부분 결과", "cancelled": "중단 · 부분 결과"}.get(summary.get("status"), "진행 중")
    metadata = [("규칙", summary.get("rule", "")), ("작업 상태", state),
                ("구분", "전체 검증 미리보기 · 저장 전" if summary.get("preview") else "복사본 저장 결과"),
                ("시작", summary.get("started_at", "")), ("종료", summary.get("finished_at", "")),
                ("원본 위치", summary.get("source_directory", "")),
                ("결과 위치", summary.get("run_directory", ""))]
    for row, (label, value) in enumerate(metadata, 3):
        sheet.write_string(row, 0, label, fmt["label"])
        sheet.write_string(row, 1, _text(value), fmt["text"])
    sheet.write_string(11, 0, "처리 집계", fmt["section"])
    sheet.write_string(12, 0, "항목", fmt["head"])
    sheet.write_string(12, 1, "건수", fmt["head"])
    sheet.write_string(12, 3, "집계 기준", fmt["head"])
    counts = summary.get("category_counts", {})
    line = 13
    for group in ("source", "result", "output", "completed", "changes", "review", "unmatched", "excluded", "files"):
        title, explanation = CATEGORIES[group]
        if group == "source":
            number = summary.get("source_records", 0)
        elif group == "result":
            number = summary.get("result_records", 0)
        else:
            number = counts.get(group, 0)
        if group == "completed" and summary.get("preview"):
            title = "검증 통과"
            explanation = "저장 전 예시 · 실제 반영은 복사본 저장 시 진행"
        sheet.write_string(line, 0, title, fmt["label"])
        sheet.write_number(line, 1, number, fmt["number"])
        sheet.write_string(line, 3, explanation, fmt["text"])
        line += 1
    for title, number, explanation in (
        ("짝 없음 · 원본", summary.get("unmatched_source_records", 0), "보완 결과가 없는 원본 레코드"),
        ("짝 없음 · 보완", summary.get("unmatched_result_records", 0), "원본에 연결되지 않은 보완 레코드"),
        ("검토 세부 사유", summary.get("review_findings", 0), "검토 사유 시트의 세부 사유 행 수"),
    ):
        sheet.write_string(line, 0, title, fmt["label"])
        sheet.write_number(line, 1, number, fmt["number"])
        sheet.write_string(line, 3, explanation, fmt["text"])
        line += 1
    line += 2
    sheet.write_string(line, 0, "읽는 방법", fmt["section"])
    for title, detail in (
        ("분류", "전체 목록에서 상태·범위·분류를 필터로 좁힙니다. 한 레코드가 여러 분류에 속할 수 있습니다."),
        ("집계", "원본과 보완은 서로 다른 입력입니다. 검토·짝 없음·변경 필드는 다른 분류와 겹치므로 숫자를 합산하지 않습니다."),
        ("라벨 수", "규칙 대상 배열의 항목 수입니다. 어절과 태그처럼 종류가 다른 목록은 각각 확인합니다."),
        ("변경 필드", "변경 경로 수입니다. 목록 전체 교체는 경로 1개로 세며 개별 라벨 변경 수를 뜻하지 않습니다."),
        ("전후 데이터", "레코드 JSON 시트의 원본·보완·출력·반영 예정 값을 비교합니다. 미리보기의 출력은 저장 전 원본입니다."),
        ("긴 JSON", "Excel 셀 한도(32,767자)를 넘으면 표시가 잘립니다. reports 폴더의 record_details.jsonl·changes.jsonl에 전체 값이 있습니다."),
        ("완전한 결과", "이 파일은 처리 결과 요약입니다. 상세 HTML과 JSONL을 함께 보관해야 상세 연결과 긴 값을 계속 확인할 수 있습니다."),
    ):
        line += 1
        sheet.write_string(line, 0, title, fmt["label"])
        sheet.write_string(line, 3, detail, fmt["text"])


def _label_rows(book, summary, fmt):
    headers = ["원본 라벨 경로", "보완 라벨 경로", "원본 입력", "보완 입력", "처리 대상 원본", "처리 후", "정상 처리", "examine 대상", "전후 증감", "집계 주의"]
    rows = _SheetRows(book, "라벨 수", headers, [39, 39, 16, 16, 17, 16, 16, 17, 15, 75], fmt)
    for item in summary.get("label_counts", []):
        counts = item["counts"]
        def visible(side):
            c = counts[side]
            if not c["arrays"] and any(c[k] for k in ("missing", "non_array", "unavailable")):
                return "확인 필요"
            return c["items"]
        problems = []
        for side in ("source", "result", "output"):
            c = counts[side]
            for key, label in (("missing", "경로 누락"), ("non_array", "배열 아님"), ("unavailable", "입력 경로 집계 불가")):
                if c.get(key):
                    problems.append(f"{SIDES[side]} {label} {c[key]}건")
        rows.append([item["path"], item.get("result_path") or "집계 불가", *
            (visible(side) for side in ("source", "result", "output_before", "output", "completed", "examine")),
            item.get("delta") if item.get("delta") is not None else "확인 필요", "; ".join(problems)])
    rows.finish()


def _record_rows(book, catalog, fmt):
    headers = ["범위", "처리 상태", "분류", "KEY", "원본 파일", "보완 파일", "레코드·행 위치", "변경 필드", "검토 사유", "처리 내용", "상세 HTML 경로"]
    rows = _SheetRows(book, "전체 목록", headers, [15, 27, 55, 25, 48, 48, 29, 14, 14, 95, 50], fmt, link_column=10)
    for item in catalog:
        if item.get("scope") == "change":
            continue  # One exact change per row is in the next sheet.
        labels = [CATEGORIES[key][0] for key in item.get("groups", ()) if key in CATEGORIES]
        rows.append([SCOPES.get(item.get("scope"), item.get("scope", "")), item.get("상태", ""),
            " · ".join(labels), _text(item.get("키")), item.get("원본"), item.get("보완"),
            item.get("위치"), item.get("changed_fields"), item.get("issues"), item.get("사유"), item.get("link")])
    rows.finish()


def _change_rows(book, directory, fmt):
    headers = ["구분", "KEY", "원본 파일", "원본 레코드", "보완 파일", "보완 행", "변경 경로", "방식", "변경 전 JSON", "변경 후 JSON"]
    rows = _SheetRows(book, "변경 필드", headers, [17, 25, 48, 27, 48, 16, 46, 15, 95, 95], fmt)
    for item in _rows(directory / "changes.jsonl"):
        rows.append(["반영 예정" if item.get("preview") else "변경 완료", _text(item.get("key")),
            item.get("source_file"), item.get("record_path"), item.get("result_file"), item.get("result_row"),
            item.get("path"), item.get("operation"), _json(item.get("before")), _json(item.get("after"))])
    rows.finish()


def _review_rows(book, directory, fmt):
    headers = ["검토 ID", "처리 상태", "KEY", "원본 파일", "원본 위치", "보완 파일", "보완 행", "사유 번호", "사유 코드", "문제 경로", "검토 사유", "근거 JSON"]
    rows = _SheetRows(book, "검토 사유", headers, [17, 26, 25, 48, 28, 48, 16, 15, 28, 46, 95, 95], fmt)
    for item in _rows(directory / "review_details.jsonl"):
        for number, issue in enumerate(item.get("issues", ()), 1):
            rows.append([item.get("review_id"), item.get("status"), _text(item.get("key")),
                item.get("source_file"), item.get("record_path"), item.get("result_file"), item.get("result_row"),
                number, issue.get("code"), issue.get("source_path"), issue.get("message"), _json(issue)])
    rows.finish()


def _detail_rows(book, directory, fmt):
    headers = ["JSONL 줄", "범위", "처리 상태", "KEY", "원본 파일", "원본 위치", "보완 파일", "보완 행", "원본 JSON", "보완 JSON", "처리 후 JSON", "반영 예정 JSON", "파일 원문(해석 실패)"]
    rows = _SheetRows(book, "레코드 JSON", headers, [15, 16, 25, 25, 48, 28, 48, 16, 95, 95, 95, 95, 95], fmt)
    for number, item in enumerate(_rows(directory / "record_details.jsonl"), 1):
        rows.append([number, SCOPES.get(item.get("scope"), item.get("scope", "")), item.get("status"),
            _text(item.get("key")), item.get("source_file"), item.get("record_path"), item.get("result_file"),
            item.get("result_row"), _json_field(item, "source_record"), _json_field(item, "platform_record"),
            _json_field(item, "final_record"), _json_field(item, "candidate_record"), item.get("input_text")])
    rows.finish()


def write_excel_report(directory: Path, summary: dict, catalog: list[dict]) -> Path:
    """Write reports/report.xlsx atomically from the same records as the HTML report."""
    directory = Path(directory)
    fd, temporary = tempfile.mkstemp(prefix="report-", suffix=".xlsx.tmp", dir=directory)
    os.close(fd)
    try:
        book = xlsxwriter.Workbook(temporary, {"constant_memory": True, "strings_to_formulas": False,
                                               "strings_to_urls": False, "remove_timezone": True})
        try:
            book.set_properties({"title": "라벨 데이터 처리 결과서", "subject": summary.get("rule", ""),
                                 "comments": "라벨 포터 처리 결과 · HTML과 동일한 실행 기준"})
            fmt = {
                "title": book.add_format({"font_name": "맑은 고딕", "font_size": 16, "bold": True, "font_color": "#172B46"}),
                "section": book.add_format({"font_name": "맑은 고딕", "font_size": 11, "bold": True, "font_color": "#244FC6"}),
                "head": book.add_format({"font_name": "맑은 고딕", "font_size": 10, "bold": True, "font_color": "#FFFFFF", "bg_color": "#263D59", "align": "center", "valign": "vcenter"}),
                "label": book.add_format({"font_name": "맑은 고딕", "font_size": 10, "bold": True, "font_color": "#354B63"}),
                "text": book.add_format({"font_name": "맑은 고딕", "font_size": 10, "font_color": "#172B46", "valign": "vcenter"}),
                "number": book.add_format({"font_name": "맑은 고딕", "font_size": 10, "font_color": "#172B46", "num_format": "#,##0"}),
                "link": book.add_format({"font_name": "맑은 고딕", "font_size": 10, "font_color": "#244FC6", "underline": 1}),
            }
            _overview(book, summary, fmt)
            _label_rows(book, summary, fmt)
            _record_rows(book, catalog, fmt)
            _change_rows(book, directory, fmt)
            _review_rows(book, directory, fmt)
            _detail_rows(book, directory, fmt)
        finally:
            book.close()
        destination = directory / "report.xlsx"
        os.replace(temporary, destination)
        return destination
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
