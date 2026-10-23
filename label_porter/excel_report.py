"""Template-inspired Excel reports with lossless, portable merge data."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from pathlib import Path
import xlsxwriter
from .result_dashboard import CATEGORIES
from .report_data import native_data, rows, line

MAX_ROWS = 1_048_576
MAX_CELL = 32_767
SCOPES = {"source": "원본", "result": "보완", "source_file": "원본 파일", "change": "변경 필드"}
STATES = {"completed": "처리 완료", "completed_with_issues": "처리 완료 · 확인 필요", "failed": "실패 · 부분 결과", "cancelled": "중단 · 부분 결과"}


def _text(value, limit=MAX_CELL):
    if value is None: return ""
    if not isinstance(value, str): value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    suffix = " … [표시 생략 · report_data JSONL 또는 병합 내장 데이터에 전체 값 보존]"
    return value if len(value) <= limit else value[:limit-len(suffix)] + suffix


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _json_field(item, key):
    return _json(item[key]) if key in item else ""


class _SheetRows:
    def __init__(self, workbook, name, headers, widths, formats, *, max_rows=MAX_ROWS, row_height=32, hidden=False):
        self.book, self.name, self.headers, self.widths = workbook, name, headers, widths
        self.formats, self.max_rows, self.row_height, self.hidden = formats, max_rows, row_height, hidden
        self.part = self.total = self.row = 0
        self.sheet = None
        self._next_sheet()

    def _next_sheet(self):
        if self.sheet is not None: self.finish()
        self.part += 1
        self.sheet = self.book.add_worksheet(self.name if self.part == 1 else f"{self.name}_{self.part}")
        self.sheet.hide_gridlines(2); self.sheet.freeze_panes(1, 2 if len(self.headers) > 4 else 0)
        self.sheet.set_zoom(85); self.sheet.set_tab_color("#51815C")
        self.sheet.set_landscape(); self.sheet.fit_to_pages(1, 0); self.sheet.repeat_rows(0)
        self.sheet.set_row(0, 34)
        for col, (header, width) in enumerate(zip(self.headers, self.widths)):
            self.sheet.set_column(col, col, width)
            self.sheet.write_string(0, col, header, self.formats["head"])
        if self.hidden: self.sheet.hide()
        self.row = 1

    def append(self, values):
        if len(values) != len(self.headers): raise ValueError(f"{self.name}: 열 수가 맞지 않습니다.")
        if self.row >= self.max_rows: self._next_sheet()
        self.sheet.set_row(self.row, self.row_height)
        for col, value in enumerate(values):
            if value is None or value == "": continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                self.sheet.write_number(self.row, col, value, self.formats["number"])
            else: self.sheet.write_string(self.row, col, _text(value), self.formats["text"])
        self.row += 1; self.total += 1

    def finish(self):
        if not self.hidden: self.sheet.autofilter(0, 0, max(0, self.row - 1), len(self.headers) - 1)


def _scan(data_path, checkpoint):
    runs, metadata = {}, {}
    changes = defaultdict(lambda: {"records": set(), "count": 0})
    keys, outcomes, examples = defaultdict(set), defaultdict(dict), {}
    for event in rows(data_path):
        checkpoint()
        kind, item, rid = event["type"], event.get("data", {}), event.get("run_id")
        if kind == "format": metadata = event.get("metadata", {})
        elif kind == "run": runs[rid] = {**item, "_display": event.get("display", {})}
        elif kind == "catalog" and item.get("scope") == "source" and item.get("키") is not None:
            keys[json.dumps(item["키"], ensure_ascii=False)].add(rid)
        elif kind == "change":
            key = json.dumps(item.get("key"), ensure_ascii=False)
            changes[rid]["records"].add((key, item.get("source_file"), item.get("record_path")))
            changes[rid]["count"] += 1
            marker = (key, item.get("path", ""))
            outcomes[marker][rid] = hashlib.sha256(line(item.get("after")).encode()).hexdigest()
            sample_key = (rid, item.get("path", ""))
            if sample_key not in examples and sum(k[0] == rid for k in examples) < 6: examples[sample_key] = item
    return runs, metadata, changes, keys, outcomes, examples


def _formats(book):
    base = {"font_name": "맑은 고딕", "font_size": 10, "font_color": "#24382A", "valign": "top"}
    return {
        "title": book.add_format({**base, "font_size": 19, "bold": True}),
        "section": book.add_format({**base, "font_size": 12, "bold": True, "bg_color": "#DBE8D3"}),
        "head": book.add_format({**base, "bold": True, "bg_color": "#DBE8D3", "border": 1, "border_color": "#ACBDA6", "align": "center", "valign": "vcenter", "text_wrap": True}),
        "label": book.add_format({**base, "bold": True, "bg_color": "#F0F4EE", "text_wrap": True}),
        "text": book.add_format({**base, "text_wrap": True, "bottom": 1, "bottom_color": "#E3E9DF"}),
        "number": book.add_format({**base, "num_format": "#,##0", "align": "right", "bottom": 1, "bottom_color": "#E3E9DF"}),
    }


def _summary(book, runs, meta, changes, keys, outcomes, fmt):
    sheet = book.add_worksheet("항목 및 보완 요약표")
    sheet.hide_gridlines(2); sheet.set_zoom(85); sheet.set_tab_color("#426447")
    for col, width in enumerate([10, 18, 42, 17, 17, 17, 17, 17, 17, 62]): sheet.set_column(col, col, width)
    sheet.merge_range(0, 0, 0, 9, meta.get("title") or "라벨 데이터 보완 결과서", fmt["title"]); sheet.set_row(0, 38)
    sheet.merge_range(2, 0, 2, 1, "과제 데이터번호", fmt["label"])
    sheet.write_string(2, 2, str(meta.get("project_id") or "미지정"), fmt["text"])
    sheet.merge_range(2, 3, 2, 4, "데이터명", fmt["label"])
    sheet.merge_range(2, 5, 2, 9, meta.get("dataset") or (next(iter(runs.values())).get("rule", "") if len(runs) == 1 else "여러 처리 작업"), fmt["text"])
    sheet.merge_range(3, 0, 3, 1, "보완 단계", fmt["label"])
    sheet.write_string(3, 2, str(meta.get("stage") or "미지정"), fmt["text"])
    sheet.merge_range(3, 3, 3, 4, "수록 작업 수", fmt["label"]); sheet.write_number(3, 5, len(runs), fmt["number"])
    sheet.merge_range(3, 6, 3, 7, "중복 KEY 수", fmt["label"]); sheet.write_number(3, 8, sum(len(v) > 1 for v in keys.values()), fmt["number"])
    sheet.write_string(3, 9, "두 개 이상 작업에 나타난 KEY · 동일 원본인지 별도 확인", fmt["text"]); sheet.set_row(3, 34)
    headers = ["작업", "품질특성", "품질검증 항목 / 규칙", "원본 레코드", "보완 레코드", "변경 레코드", "변경 필드", "검토/오류 행", "짝 없음", "비고"]
    for col, header in enumerate(headers): sheet.write_string(5, col, header, fmt["head"])
    sheet.set_row(5, 36); row = 6
    for number, (rid, summary) in enumerate(runs.items(), 1):
        mode = "미리보기 · 반영 예정" if summary.get("preview") else "복사본 저장 결과"
        note = f"{mode}\n{STATES.get(summary.get('status'), summary.get('status', ''))}\n라벨 수는 종류별로 ‘라벨 수’ 시트에서 확인"
        values = [f"R{number:03}", meta.get("quality") or summary.get("_display", {}).get("quality") or "미지정", summary.get("rule", ""), summary.get("source_records", 0), summary.get("result_records", 0), len(changes[rid]["records"]), changes[rid]["count"], summary.get("category_counts", {}).get("review", 0), summary.get("unmatched_records", 0), note]
        for col, value in enumerate(values):
            if isinstance(value, int): sheet.write_number(row, col, value, fmt["number"])
            else: sheet.write_string(row, col, _text(value), fmt["text"])
        sheet.set_row(row, 66); row += 1
    row += 2
    sheet.merge_range(row, 0, row, 9, "집계 기준과 병합 결과", fmt["section"]); sheet.set_row(row, 28)
    notes = [
        f"원본 처리 누계 {sum(s.get('source_records', 0) for s in runs.values()):,}건. 서로 다른 작업의 동일 원본이 포함될 수 있으므로 고유 전체 데이터 수가 아닙니다.",
        f"작업 간 같은 KEY·같은 필드에서 보완 후 값이 다른 조합 {sum(len(set(v.values())) > 1 for v in outcomes.values()):,}건. ‘병합 확인’에서 출처를 확인하세요. 나중 실행 값으로 덮어쓰지 않습니다.",
        f"동일 실행 결과 중복 제외 {meta.get('duplicate_runs_skipped', 0):,}건. 복사된 결과서와 이미 병합된 결과서도 작업 ID를 기준으로 구분합니다.",
        "변경 레코드는 각 작업에서 실제 변경 경로가 기록된 원본 레코드 수입니다. 변경 필드는 경로 수이며 배열 전체 교체도 1건입니다. 변경한 개별 라벨 수와 같지 않습니다.",
        "검토/오류·짝 없음은 다른 분류와 겹칠 수 있습니다. 미리보기와 최종 저장은 별도 작업으로 표시하며, 보고서 병합은 라벨 JSON을 수정하지 않습니다.",
        "보완 상세 이력은 실제 전후 값, 보완 예시는 경로별 첫 변경 사례입니다. 의미 오류 유형이나 검수 합격 여부를 자동 추정하지 않습니다.",
        "Excel 표는 읽기·필터용입니다. 병합은 내장된 원 실행 데이터를 사용하므로 셀을 수동으로 편집한 값은 병합에 반영되지 않습니다. 구조·단위는 ‘작성 기준’을 확인하세요.",
    ]
    for note in notes:
        row += 1; sheet.merge_range(row, 0, row, 9, note, fmt["text"]); sheet.set_row(row, 34)
    sheet.freeze_panes(6, 3); sheet.set_landscape(); sheet.fit_to_pages(1, 0); sheet.print_area(0, 0, row, 9)


def export_workbook(data_path, destination, checkpoint=lambda: None):
    runs, meta, changes, keys, outcomes, examples = _scan(data_path, checkpoint)
    if not runs: raise ValueError("출력할 처리 작업이 없습니다.")
    names = {rid: f"R{n:03}" for n, rid in enumerate(runs, 1)}
    book = xlsxwriter.Workbook(str(destination), {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    try:
        book.set_properties({"title": meta.get("title") or "라벨 데이터 보완 결과서", "comments": "label-porter.report schema_version 1"})
        fmt = _formats(book); _summary(book, runs, meta, changes, keys, outcomes, fmt)
        history = _SheetRows(book, "보완 상세 이력", ["No.", "데이터 ID / KEY", "검증 항목 / 규칙", "처리 내용", "보완 위치", "보완 전", "보완 후", "비고 / 처리 상태", "작업", "원본 파일", "원본 레코드", "보완 파일", "보완 행"], [9, 28, 35, 40, 40, 70, 70, 32, 12, 45, 25, 45, 15], fmt, row_height=72)
        sample = _SheetRows(book, "보완 예시", ["작업", "검증 항목", "데이터 ID / KEY", "보완 위치", "보완 전", "보완 후", "선정 기준"], [12, 35, 28, 35, 75, 75, 32], fmt, row_height=110)
        labels = _SheetRows(book, "라벨 수", ["작업", "원본 라벨 경로", "보완 라벨 경로", "원본 입력", "보완 입력", "처리 대상 원본", "처리 후", "정상 처리", "examine 대상", "전후 증감", "집계 주의"], [12, 38, 38, 17, 17, 18, 17, 17, 18, 17, 70], fmt)
        records = _SheetRows(book, "전체 목록", ["작업", "범위", "처리 상태", "분류", "KEY", "원본 파일", "보완 파일", "레코드·행 위치", "변경 필드", "검토 사유 수", "처리 내용"], [12, 16, 28, 48, 28, 45, 45, 28, 15, 17, 80], fmt)
        reviews = _SheetRows(book, "검토 사유", ["작업", "검토 ID", "처리 상태 코드", "KEY", "원본 파일", "원본 위치", "보완 파일", "보완 행", "사유 번호", "사유 코드", "문제 경로", "검토 사유", "근거 JSON"], [12, 16, 26, 28, 45, 28, 45, 15, 15, 28, 40, 70, 80], fmt, row_height=64)
        overlap = _SheetRows(book, "병합 확인", ["확인 유형", "KEY", "필드 경로", "출처 작업", "해석"], [28, 28, 44, 45, 95], fmt, row_height=52)
        detail = _SheetRows(book, "레코드 JSON", ["작업", "범위", "처리 상태 코드", "KEY", "원본 파일", "원본 위치", "보완 파일", "보완 행", "원본 JSON", "보완 JSON", "처리 후 JSON", "반영 예정 JSON", "파일 원문(해석 실패)"], [12, 16, 26, 28, 45, 28, 45, 15, 85, 85, 85, 85, 85], fmt)
        sources = _SheetRows(book, "작업 출처", ["작업", "고유 작업 ID", "규칙", "처리 상태", "구분", "시작", "종료", "원본 위치", "원 결과 위치", "과제 번호", "데이터명", "보완 단계", "품질특성"], [12, 70, 45, 28, 24, 30, 30, 70, 70, 20, 45, 30, 25], fmt, row_height=42)
        glossary = _SheetRows(book, "작성 기준", ["항목", "정의 / 구조", "사용 방법"], [32, 105, 95], fmt, row_height=48)
        for rid, summary in runs.items():
            display = {**summary.get("_display", {}), **{k:v for k,v in meta.items() if v}}
            sources.append([names[rid], rid, summary.get("rule"), STATES.get(summary.get("status"), summary.get("status")), "반영 예정" if summary.get("preview") else "복사본 저장", summary.get("started_at"), summary.get("finished_at"), summary.get("source_directory"), summary.get("run_directory"), *(display.get(k) or "미지정" for k in ("project_id", "dataset", "stage", "quality"))])
            if not summary.get("label_counts"):
                labels.append([names[rid], "집계 정보 없음", "", *(["미집계"] * 7), "원 보고서에 라벨 집계가 없거나 규칙 대상 배열이 없습니다. 0개로 간주하지 않습니다."])
            for item in summary.get("label_counts", []):
                values, notes = [], []
                for side in ("source", "result", "output_before", "output", "completed", "examine"):
                    c = item["counts"][side]; problem = any(c.get(k) for k in ("missing", "non_array", "unavailable"))
                    values.append("확인 필요" if not c.get("arrays") and problem else c["items"])
                    if problem: notes.append(f"{side}: 경로 누락 {c.get('missing',0)}, 배열 아님 {c.get('non_array',0)}, 집계 불가 {c.get('unavailable',0)}")
                labels.append([names[rid], item["path"], item.get("result_path") or "집계 불가", *values, item.get("delta") if item.get("delta") is not None else "확인 필요", "; ".join(notes)])
        for (rid, path), item in examples.items():
            sample.append([names[rid], runs[rid].get("rule"), _text(item.get("key")), path, _text(_json_field(item, "before"), 2400), _text(_json_field(item, "after"), 2400), "작업별 경로의 첫 변경 사례 · 최대 6경로. 전체 값은 상세 이력/내장 데이터에 보관"])
        for key, ids in keys.items():
            if len(ids) > 1: overlap.append(["작업 간 KEY 중복", key, "", ", ".join(names[r] for r in runs if r in ids), "작업별 이력을 모두 유지했습니다. KEY가 같아도 서로 다른 데이터일 수 있어 전체 레코드 수로 단순 합산하지 않습니다."])
        for (key, path), values in outcomes.items():
            if len(set(values.values())) > 1: overlap.append(["같은 KEY·필드의 결과 차이", key, path, ", ".join(names[r] for r in runs if r in values), "서로 다른 보완 후 값입니다. 순차 보완 이력 또는 충돌일 수 있습니다. 최신값 자동 선택 없이 상세 이력에서 각 작업을 확인하세요."])
        for item in [
            ("원본 / 보완 레코드", "각 작업의 입력 레코드 수. 해석 실패 파일은 별도 파일 오류이며 레코드 수에 포함되지 않습니다.", "작업 사이에 입력이 겹칠 수 있습니다. 전체 데이터 수로 합산하지 마세요."),
            ("변경 레코드 / 변경 필드", "변경 이력이 있는 원본 파일+레코드 위치+KEY의 수 / 변경 경로 수.", "배열 전체 교체 1건은 라벨 1개 수정이라는 뜻이 아닙니다."),
            ("라벨 수", "규칙 대상 배열의 항목 수. 처리 후 = 정상 처리 + examine 대상.", "미리보기는 반영 예정 값. 종류가 다른 라벨은 합산하지 않습니다."),
            ("보완 상세 이력", "한 행 = 한 작업에서 한 원본 레코드의 한 변경 경로. No.는 이 통합문서의 행 순서.", "작업+KEY+원본 파일+원본 레코드로 연결. before/after는 JSON 값 표현을 유지합니다."),
            ("원본 / 처리 후 / 반영 예정 JSON", "미리보기의 처리 후 JSON은 저장 전 원본이며, 적용될 값은 반영 예정 JSON입니다.", "복사본 저장 작업의 처리 후 JSON이 실제 출력입니다. 검토 보류는 원본 유지입니다."),
            ("결과서 병합", "고유 작업 ID가 동일한 실행만 중복 제외. 다른 실행은 KEY가 같아도 유지.", "라벨 JSON 값은 병합하지 않습니다. 같은 KEY·필드의 결과 차이는 병합 확인에서 검토합니다."),
            ("기계 판독 구조", "label-porter.report / schema_version=1. JSONL 행: type, run_id, data. type=run,catalog,change,review,detail.", "format 행은 형식/보고서 메타데이터, end 행은 행수/SHA-256입니다. 작업 출처의 ID로 연결하세요."),
            ("Excel 내장 원문", "숨김 _porter_data 시트: entry, part, parts, jsonl. 각 JSONL 행을 텍스트 조각으로 보존하며 Excel 파일 자체 압축을 사용합니다.", "외부 파일 없이 이 Excel만으로 재병합할 수 있습니다. 표의 수동 편집은 원 실행 데이터에 반영되지 않습니다."),
            ("긴 값", "표 셀은 32,767자 한도이며 생략 표식이 표시됩니다. 내장 원문과 .data.jsonl 또는 report_data.jsonl에는 전체 값을 보존합니다.", "재병합에서도 생략 전 원문을 사용합니다. Excel 행 한도는 후속 시트로 분할합니다."),
            ("품질특성·과제 정보", "병합 화면에서 입력한 표시용 정보입니다. 미입력 값은 미지정.", "원 규칙명과 처리 집계를 유지합니다. 의미 정확성·오류 원인·검수 통과를 자동 판정하지 않습니다."),
        ]: glossary.append(item)
        embedded = _SheetRows(book, "_porter_data", ["entry", "part", "parts", "jsonl"], [14, 12, 12, 80], fmt, hidden=True, row_height=15)
        with Path(data_path).open(encoding="utf-8") as stream:
            for n, text in enumerate(stream, 1):
                checkpoint(); event = json.loads(text)
                chunks = [text[i:i+30000] for i in range(0, len(text), 30000)]
                for part, chunk in enumerate(chunks, 1): embedded.append([n, part, len(chunks), chunk])
                kind, item, rid = event["type"], event.get("data", {}), event.get("run_id"); job = names.get(rid, "")
                if kind == "catalog" and item.get("scope") != "change":
                    records.append([job, SCOPES.get(item.get("scope"), item.get("scope", "")), item.get("상태"), " · ".join(CATEGORIES[g][0] for g in item.get("groups", []) if g in CATEGORIES), _text(item.get("키")), item.get("원본"), item.get("보완"), item.get("위치"), item.get("changed_fields"), item.get("issues"), item.get("사유")])
                elif kind == "change":
                    history.append([history.total + 1, _text(item.get("key")), runs[rid].get("rule"), "보완 전후 값 반영" if item.get("operation") != "remove" else "필드 삭제", item.get("path"), _json_field(item, "before"), _json_field(item, "after"), "반영 예정" if item.get("preview") else "변경 완료", job, item.get("source_file"), item.get("record_path"), item.get("result_file"), item.get("result_row")])
                elif kind == "review":
                    for number, issue in enumerate(item.get("issues", []), 1):
                        reviews.append([job, item.get("review_id"), item.get("status"), _text(item.get("key")), item.get("source_file"), item.get("record_path"), item.get("result_file"), item.get("result_row"), number, issue.get("code"), issue.get("source_path"), issue.get("message"), _json(issue)])
                elif kind == "detail":
                    detail.append([job, SCOPES.get(item.get("scope"), item.get("scope", "")), item.get("status"), _text(item.get("key")), item.get("source_file"), item.get("record_path"), item.get("result_file"), item.get("result_row"), *(_json_field(item, k) for k in ("source_record", "platform_record", "final_record", "candidate_record")), item.get("input_text")])
        for table in (history, sample, labels, records, reviews, overlap, detail, sources, glossary, embedded): table.finish()
    finally: book.close()


def write_excel_report(directory: Path, summary: dict, catalog: list[dict]) -> Path:
    directory = Path(directory)
    with tempfile.TemporaryDirectory(prefix="excel-", dir=directory) as temporary:
        data = native_data(directory, Path(temporary) / "report_data.jsonl", summary, catalog)
        workbook = Path(temporary) / "report.xlsx"
        export_workbook(data, workbook)
        os.replace(data, directory / "report_data.jsonl")
        os.replace(workbook, directory / "report.xlsx")
    return directory / "report.xlsx"
