from __future__ import annotations

import csv
import html
import json
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .formats import atomic_text
from .review_report import write_review
from .report_paging import STYLE, CONTROLS, PAGER, SCRIPT


STATUS = {
    "updated": "반영 완료", "planned_update": "반영 예정", "unchanged": "변경 없음",
    "no_result": "결과 없음 · 원본 보존", "excluded": "대상 조건 제외 · 원본 보존",
    "review_required": "검토 필요 · 원본 보존", "duplicate_source_key": "원본 키 중복 · 원본 보존",
    "duplicate_result": "결과 키 중복 · 원본 보존", "orphan_result": "연결할 원본 없음",
    "invalid_result": "결과 해석/키 오류", "invalid_source": "원본 해석/키 오류 · 원본 보존",
}
ISSUES = {"review_required", "duplicate_source_key", "duplicate_result", "orphan_result", "invalid_result", "invalid_source"}


def csv_safe(value):
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


class Reports:
    def __init__(self, directory: Path, split_output=False):
        self.directory = directory
        self.split_output = bool(split_output)
        directory.mkdir(parents=True, exist_ok=True)
        self.counts = Counter()
        self.scope_counts = {"source": Counter(), "result": Counter(), "source_file": Counter()}
        self.change_count = 0
        self.examples = []
        self.review_issue_count = 0
        self.advisory_rows = 0
        self.held_reference_count = 0
        self.changed_files = set()
        # Bound both disk concurrency and retained evidence. Main CSV/JSONL order
        # stays deterministic; independent detail files can be written concurrently.
        self._detail_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="review")
        self._pending_details = deque()
        self._records = (directory / "records.csv").open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._records)
        self._writer.writerow(["scope", "status", "상태", "source_file", "record_path", "key", "result_file", "result_row", "reason", "changed_fields", "output_group", "review_detail", "review_issue_count"])
        self._changes = (directory / "changes.jsonl").open("w", encoding="utf-8")
        self._reviews = (directory / "review_details.jsonl").open("w", encoding="utf-8")
        self._held = (directory / "held_references.jsonl").open("w", encoding="utf-8")

    def record(self, scope, status, source_file="", record_path="", key="", result_file="", result_row="", reason="", changed_fields=0, evidence=None):
        self.counts[status] += 1
        self.scope_counts[scope][status] += 1
        group = ("changed" if status in ("updated", "planned_update") else "unchanged") if self.split_output and scope == "source" else ""
        review_link, issue_count = "", 0
        has_advisories = status not in ISSUES and evidence and evidence.get("issues")
        if status in ISSUES or has_advisories:
            if has_advisories:
                self.advisory_rows += 1
            review_id = f"{len(self.examples)+1:06d}"
            detail = {"review_id": review_id, "status": status, "source_file": source_file,
                      "record_path": record_path, "key": key, "result_file": result_file, "result_row": result_row,
                      **(evidence or {})}
            detail.setdefault("issues", [{"code": status, "message": reason, "source_path": record_path}])
            for issue in detail["issues"]:
                if issue.get("code") == "held_reference":
                    self.held_reference_count += 1
                    self._held.write(json.dumps({"source_file": source_file, "record_path": record_path,
                        "result_file": result_file, "result_row": result_row, "review_id": review_id, **issue},
                        ensure_ascii=False, allow_nan=False) + "\n")
            issue_count = len(detail["issues"])
            self.review_issue_count += issue_count
            # Keep every reason in CSV; full unabridged values live alongside it.
            reason = "\n".join(f"{i}. {item['message']} [{item.get('source_path') or '레코드 전체'}]"
                               for i, item in enumerate(detail["issues"], 1))
            if len(self._pending_details) >= 32:
                self._pending_details.popleft().result()
            self._pending_details.append(self._detail_pool.submit(write_review, self.directory, review_id, detail))
            self._reviews.write(json.dumps(detail, ensure_ascii=False, allow_nan=False) + "\n")
            review_link = f"review/{review_id}.html"
            self.examples.append({"상태": STATUS[status] + (" · 태그 별도 검토" if has_advisories else ""), "원본": source_file, "키": key, "사유": reason,
                                  "link": review_link, "issues": issue_count})
        row = [scope, status, STATUS.get(status, status), source_file, record_path, key, result_file, result_row, reason, changed_fields, group, review_link, issue_count]
        self._writer.writerow([csv_safe(value) for value in row])

    def changes(self, source_file, record_path, key, changes, result_file, result_row, preview):
        if changes:
            self.changed_files.add(source_file)
        for change in changes:
            self.change_count += 1
            row = {"source_file": source_file, "record_path": record_path, "key": key,
                   "result_file": result_file, "result_row": result_row, "preview": preview, **change}
            self._changes.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def finish(self, summary):
        try:
            for pending in self._pending_details:
                pending.result()
        except Exception as exc:
            summary.update(status="failed", error=f"상세 보고서 저장 실패: {exc}")
        finally:
            self._detail_pool.shutdown(wait=True)
            self._records.close()
            self._changes.close()
            self._reviews.close()
            self._held.close()
        label_issues = sum(self.counts[k] for k in ISSUES) + self.advisory_rows
        summary.update(status_counts=dict(self.counts), source_status_counts=dict(self.scope_counts["source"]),
                       result_issue_counts=dict(self.scope_counts["result"]), source_file_issue_counts=dict(self.scope_counts["source_file"]), changed_fields=self.change_count,
                       changed_files=len(self.changed_files), label_issue_rows=label_issues,
                       issue_rows=label_issues, review_findings=self.review_issue_count,
                       advisory_rows=self.advisory_rows, held_references=self.held_reference_count)
        atomic_text(self.directory / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        e = lambda value: html.escape(str(value), quote=True)
        rows = "".join(f"<tr><td>{e(STATUS.get(key, key))}</td><td>{value:,}</td></tr>" for key, value in self.counts.items())
        examples = "".join("<tr>" + "".join(f"<td>{e(row[key])}</td>" for key in ("상태", "원본", "키"))
                           + f'<td style="white-space:pre-wrap">{e(row["사유"])}</td><td><a href="{e(row["link"])}">비교 보기 · {row["issues"]}개 사유</a></td></tr>' for row in self.examples[:25])
        # Escape script terminators and HTML metacharacters in untrusted JSON.
        paging_data = json.dumps(self.examples, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        mode = "검증 미리보기 · 데이터 파일을 생성하지 않았습니다" if summary["preview"] else "원본 복사본에 반영한 결과"
        state = {"completed": "완료", "completed_with_issues": "완료 · 검토 항목 있음", "failed": "실패 · 결과를 납품에 사용하지 마세요", "cancelled": "중단 · 부분 결과"}.get(summary["status"], summary["status"])
        partition_section = ""
        if summary.get("split_output"):
            counts = summary.get("partition_records", {})
            files = summary.get("partition_files", {})
            partition_state = {"completed": "저장 완료", "planned": "분리 예정 · 미리보기", "pending": "미실행", "in_progress": "중단/실패로 미완료"}.get(summary.get("partition_status"), "미완료")
            partition_section = f'''<div class="card"><h2>변경 / 미변경 라벨 분리 · {e(partition_state)}</h2>
<table><thead><tr><th>폴더</th><th>기준</th><th>레코드</th><th>파일</th></tr></thead><tbody>
<tr><td>changed/labels/</td><td>반영 후 원본과 실제로 달라진 라벨</td><td>{counts.get('changed',0):,}</td><td>{files.get('changed',0):,}</td></tr>
<tr><td>unchanged/labels/</td><td>최종 라벨이 원본 그대로인 항목</td><td>{counts.get('unchanged',0):,}</td><td>{files.get('unchanged',0):,}</td></tr>
<tr><td>unclassified/labels/</td><td>JSON을 해석할 수 없어 레코드 분류가 불가능한 파일</td><td>확인 불가</td><td>{files.get('unclassified',0):,}</td></tr></tbody></table>
<p>미변경에는 값이 같은 결과, 결과 없음, 조건 제외, 검토 필요 항목이 포함됩니다. 검수 완료를 뜻하지 않으며, 각각의 사유는 records.csv에서 확인하세요. 분리 대상은 라벨 JSON이며 원본 폴더의 다른 파일은 전체 data/에 보존합니다.</p>
<p>통합 JSON은 레코드별로 분리하고, 개별 JSON은 기존 파일명·상대 경로를 유지합니다. <a href="partition_index.jsonl">원본 ↔ 분리본 레코드 위치</a></p></div>'''
        doc = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>라벨 포터 결과 보고서</title><style>
body{{font:15px/1.65 'Malgun Gothic',sans-serif;color:#173044;background:#f3f6f9;margin:0}}main{{max-width:1100px;margin:auto;padding:40px}}
h1{{font-size:30px;margin-bottom:8px}}.muted{{color:#5e7381}}.card{{background:white;padding:24px;border:1px solid #dde5eb;border-radius:12px;margin:18px 0}}
.metrics{{display:flex;gap:16px;flex-wrap:wrap}}.metric{{flex:1;background:#eaf3f5;padding:18px;border-radius:8px;min-width:150px}}strong{{font-size:25px;display:block}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:10px 12px;border-bottom:1px solid #e1e8ed;text-align:left;word-break:break-word}}th{{background:#f2f6f8}}
a{{color:#006b82}}code{{word-break:break-all}}.notice{{border-left:4px solid #d49c30;padding:12px 18px;background:#fff6df}}footer{{margin-top:24px;font-size:13px;color:#667}}
{STYLE}</style><main><p class="muted">LABEL PORTER · 파일 기반 라벨 반영</p><h1>{e(summary['profile'])}</h1><p>{e(mode)} · <b>{e(state)}</b></p>
<div class="metrics"><div class="metric">원본 레코드<strong>{summary.get('source_records',0):,}</strong></div><div class="metric">작업 결과 레코드<strong>{summary.get('result_records',0):,}</strong></div><div class="metric">변경 필드<strong>{self.change_count:,}</strong></div><div class="metric">검토/오류 행<strong>{summary['issue_rows']:,}</strong></div></div>
<div class="card"><b>원본 유지 정책</b><p>결과가 없는 레코드는 삭제하지 않았습니다. 매칭 오류·중복·검증 실패가 발생한 레코드는 전체를 원본 상태로 보존합니다. 결과 없음은 곧 작업 누락을 뜻하지 않으며 실제 작업 대상 목록과 대조해야 합니다.</p>
<p>원본: <code>{e(summary['source_directory'])}</code><br>실행 결과: <code>{e(summary['run_directory'])}</code></p>
<p class="muted">정상 종료 시 원본 수는 source 상태 행의 합계입니다. 파일 해석 실패는 source_file, 결과 키 오류·연결 실패는 result 행으로 별도 기록됩니다. 아래 상태 행을 모두 합한 값은 원본 수와 다를 수 있습니다.</p></div>
{partition_section}<div class="card"><h2>처리 집계</h2><table><thead><tr><th>상태</th><th>건수</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="card"><h2>상세 파일</h2><p><a href="records.csv">전체 항목 상태 CSV</a> · <a href="changes.jsonl">필드별 변경 전후 JSONL</a> · <a href="review_details.jsonl">모든 검토 사유·전체 JSON</a> · <a href="held_references.jsonl">별도 보관 태그 ({self.held_reference_count:,}개)</a> · <a href="summary.json">집계 JSON</a> · <a href="../manifest.json">입력·파일 해시 명세</a> · <a href="../profile.json">실행한 규칙</a></p>
<p>CSV는 UTF-8 BOM을 포함합니다. 엑셀에서 열 때 ID 열은 텍스트로 가져오세요. JSONL에는 변경 전후 값이 생략 없이 기록됩니다.</p></div>
<div class="card"><h2>전체 검토 항목 · {len(self.examples):,}건 / 세부 사유 {self.review_issue_count:,}개</h2>
<p>‘비교 보기’에서 문제 위치·ID·변환 전후 값과 원본, 플랫폼 최종 결과, 실제 출력, 후보 JSON을 확인하세요. ‘원본 보존’ 항목은 전체 반영을 보류합니다. ‘태그 별도 검토’ 항목은 어절·시간·전사를 반영하고 연결이 불확실한 태그만 별도 보관합니다.</p>
{CONTROLS}<div class="table-scroll"><table id="review-table"><thead><tr><th>상태</th><th>원본</th><th>키</th><th>모든 검토 사유</th><th>데이터 비교</th></tr></thead><tbody id="review-list">{examples or '<tr><td colspan="5">라벨 검토 항목이 없습니다.</td></tr>'}</tbody></table></div>{PAGER}</div>
<div class="notice">{e(summary.get('error') or '이 보고서는 파일 연결·반영·구조 검증 결과입니다. 라벨 내용의 정답 여부까지 판단하지는 않습니다.')}</div>
<footer>{e(summary['started_at'])} → {e(summary.get('finished_at',''))} · 입력과 결과는 이 컴퓨터에서 처리됩니다.</footer></main>
<script id="review-data" type="application/json">{paging_data}</script><script type="text/javascript">{SCRIPT}</script></html>'''
        atomic_text(self.directory / "report.html", doc)
        return summary
