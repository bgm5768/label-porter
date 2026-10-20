from __future__ import annotations

import csv
import html
import json
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .formats import atomic_text
from .review_report import write_review
from .detail_store import DetailStore
from .result_dashboard import write_dashboard


STATUS = {
    "updated": "반영 완료", "planned_update": "반영 예정", "unchanged": "변경 없음",
    "no_result": "결과 없음 · 원본 보존", "excluded": "대상 조건 제외 · 원본 보존",
    "review_required": "검토 필요 · 원본 보존", "duplicate_source_key": "원본 키 중복 · 원본 보존",
    "duplicate_result": "결과 키 중복 · 원본 보존", "orphan_result": "연결할 원본 없음",
    "pending": "처리 미완료", "invalid_result": "결과 해석/키 오류", "invalid_source": "원본 해석/키 오류 · 원본 보존",
}
ISSUES = {"review_required", "duplicate_source_key", "duplicate_result", "orphan_result", "invalid_result", "invalid_source"}


def csv_safe(value):
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


class Reports:
    def __init__(self, directory: Path, split_output=False, preview=False):
        self.preview = preview
        self.directory = directory
        self.split_output = bool(split_output)
        directory.mkdir(parents=True, exist_ok=True)
        self.catalog = []
        self.result_catalog = {}
        self.source_links = {}
        self.examine_positions = {}
        self.examine_files = set()
        self.details = DetailStore(directory)
        self._all_details = (directory / "record_details.jsonl").open("w", encoding="utf-8")
        self._examine = None
        if not preview:
            (directory.parent / "examine").mkdir(exist_ok=True)
            self._examine = (directory.parent / "examine" / "records.jsonl").open("w", encoding="utf-8")
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

    def result_input(self, file, number, key, record):
        self.result_catalog[(file, number)] = {"scope": "result", "status": "pending", "상태": STATUS["pending"],
            "원본": "", "보완": file, "키": key, "사유": "중단 또는 실패로 처리가 끝나지 않았습니다.",
            "위치": str(number), "link": "", "issues": 0, "groups": ["result"], "_record": record}

    def record(self, scope, status, source_file="", record_path="", key="", result_file="", result_row="", reason="", changed_fields=0, evidence=None):
        self.counts[status] += 1
        self.scope_counts[scope][status] += 1
        attention = status in ISSUES or status == "no_result" or bool(evidence and evidence.get("issues"))
        group = ("changed" if status in ("updated", "planned_update") else "unchanged") if self.split_output and scope == "source" else ""
        if attention and scope == "source":
            group = "examine"
            doc, _, pointer = record_path.partition("/")
            self.examine_positions.setdefault(Path(source_file), set()).add((int(doc[4:]), "/" + pointer if "/" in record_path else ""))
        elif attention and scope == "source_file":
            self.examine_files.add(Path(source_file))
        review_link, issue_count = "", 0
        has_advisories = status not in ISSUES and evidence and evidence.get("issues")
        if status in ISSUES or has_advisories:
            if has_advisories:
                self.advisory_rows += 1
            review_id = f"{len(self.examples)+1:06d}"
            detail = {"review_id": review_id, "status": status, "source_file": source_file,
                      "record_path": record_path, "key": key, "result_file": result_file, "result_row": result_row, "preview": self.preview,
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
        detail = {"status": status, "scope": scope, "source_file": source_file, "record_path": record_path,
                  "key": key, "result_file": result_file, "result_row": result_row, "reason": reason,
                  "preview": self.preview, **(evidence or {})}
        self._all_details.write(json.dumps(detail, ensure_ascii=False, allow_nan=False) + "\n")
        if attention and self._examine:
            self._examine.write(json.dumps(detail, ensure_ascii=False, allow_nan=False) + "\n")
        link = review_link or self.details.add(detail)
        groups = ["source", "output"] if scope == "source" else ["result"] if scope == "result" and result_row else ["files"]
        if status in ("updated", "planned_update", "unchanged") and not attention and scope == "source":
            groups.append("completed")
        if attention: groups.append("examine")
        if status == "excluded": groups.append("excluded")
        if status in ISSUES or has_advisories: groups.append("review")
        if status in ("no_result", "orphan_result"): groups.append("unmatched")
        item = {"scope": scope, "status": status, "상태": STATUS.get(status, status), "원본": source_file, "보완": result_file, "키": key, "사유": reason, "위치": record_path or str(result_row),
                "link": link, "issues": issue_count, "groups": groups, "changed_fields": changed_fields, "output_group": group}
        if scope == "result" and result_row:
            self.result_catalog[(result_file, result_row)] = item
        else:
            self.catalog.append(item)
        if scope == "source":
            self.source_links[(source_file, record_path)] = link
            incoming = self.result_catalog.get((result_file, result_row))
            if incoming is not None:
                incoming.update({k: v for k, v in item.items() if k not in ("scope", "groups", "위치")})
                incoming["groups"] = ["result"]
                incoming.pop("_record", None)
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
            def short(value):
                text = json.dumps(value, ensure_ascii=False)
                return text if len(text) <= 180 else text[:180] + "… (상세에서 전체 값 확인)"
            self.catalog.append({"scope": "change", "status": "planned_update" if preview else "updated",
                "상태": "변경 예정" if preview else "변경 완료", "원본": source_file, "보완": result_file, "키": key,
                "위치": change["path"], "사유": f'{change["path"]}\n{short(change.get("before"))} → {short(change.get("after"))}',
                "groups": ["changes"], "link": self.source_links.get((source_file, record_path), ""), "issues": 0})

    def finish(self, summary):
        try:
            for item in self.result_catalog.values():
                if "_record" in item:
                    detail = {"status": "pending", "reason": item["사유"], "platform_record": item.pop("_record"),
                              "result_file": item["보완"], "result_row": item["위치"], "preview": self.preview}
                    item["link"] = self.details.add(detail)
                    self._all_details.write(json.dumps(detail, ensure_ascii=False) + "\n")
            self.catalog.extend(self.result_catalog.values())
            self.details.close()
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
            self._all_details.close()
            if self._examine: self._examine.close()
        label_issues = sum(self.counts[k] for k in ISSUES) + self.advisory_rows
        summary.update(status_counts=dict(self.counts), source_status_counts=dict(self.scope_counts["source"]),
                       result_issue_counts=dict(self.scope_counts["result"]), source_file_issue_counts=dict(self.scope_counts["source_file"]), changed_fields=self.change_count,
                       changed_files=len(self.changed_files), label_issue_rows=label_issues,
                       issue_rows=label_issues, review_findings=self.review_issue_count,
                       advisory_rows=self.advisory_rows, held_references=self.held_reference_count)
        category_counts = Counter(group for item in self.catalog for group in item["groups"])
        summary.update(category_counts=dict(category_counts), completed_records=category_counts["completed"],
                       output_records=category_counts["output"], unmatched_records=category_counts["unmatched"],
                       unmatched_source_records=self.scope_counts["source"]["no_result"],
                       unmatched_result_records=self.scope_counts["result"]["orphan_result"])
        atomic_text(self.directory / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        write_dashboard(self.directory, summary, self.catalog, self.examples)
        return summary
