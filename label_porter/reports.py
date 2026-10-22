from __future__ import annotations

import csv
import html
import json
import shutil
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .formats import atomic_text
from .review_report import write_review
from .detail_store import DetailStore
from .result_dashboard import write_dashboard
from .examine import category_for, label_path, ResultArchive
from .json_display import json_prefix
from .label_counts import LabelCounts


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
    def __init__(self, directory: Path, split_output=False, preview=False, result_spec=None, result_files=None, record_sink=None, config=None):
        self.record_sink = record_sink
        self.preview = preview
        self.labels = LabelCounts(config)
        self.directory = directory
        self.split_output = bool(split_output)
        directory.mkdir(parents=True, exist_ok=True)
        self.catalog = []
        self.result_catalog = {}
        self.source_links = {}
        self.examine_positions = {}
        self.examine_files = set()
        self.examine_categories = {}
        self.examine_counts = Counter()
        self.result_archive = ResultArchive(result_spec or {}, result_files)
        self.source_addresses = {}
        self.details = DetailStore(directory)
        self._all_details = (directory / "record_details.jsonl").open("w", encoding="utf-8")
        self._examine_number = 0
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

    def source_input(self, file, positions):
        self.source_addresses[file] = {f"doc:{d}{p}": ordinal for ordinal, (d, p) in enumerate(positions, 1)}

    def _archive_links(self, detail, category):
        sources = [(detail["source_file"], detail["record_path"])]
        sources.extend((row["file"], row["record_path"]) for row in detail.get("source_candidates", []))
        originals = []
        for file, pointer in sources:
            ordinal = self.source_addresses.get(file, {}).get(pointer)
            if ordinal:
                originals.append(label_path(category, file, ordinal, len(self.source_addresses[file])).as_posix())
        if detail["scope"] == "source_file":
            originals.append(label_path(category, detail["source_file"], None, 0).as_posix())
        results = [(detail["result_file"], detail["result_row"])]
        results.extend((row["file"], row["number"]) for row in detail.get("platform_candidates", []))
        incoming = []
        for file, number in results:
            target = self.result_archive.request(category, file, number)
            if target:
                incoming.append(target)
        detail["examine_sources"] = list(dict.fromkeys(originals))
        detail["examine_results"] = list(dict.fromkeys(incoming))
        if incoming:
            detail["examine_data"] = incoming[0]

    def record(self, scope, status, source_file="", record_path="", key="", result_file="", result_row="", reason="", changed_fields=0, evidence=None):
        connections = (evidence or {}).pop("_preview_connections", None)
        if self.record_sink:
            self.record_sink({"scope": scope, "status": status, "source_file": source_file,
                "record_path": record_path, "key": key, "result_file": result_file,
                "result_row": result_row, "reason": reason, "changed_fields": changed_fields,
                **(evidence or {})}, connections)
        self.counts[status] += 1
        self.scope_counts[scope][status] += 1
        category = category_for(scope, status, result_row, bool(evidence and evidence.get("issues")))
        attention = bool(category)
        if scope == "source" and evidence and "source_record" in evidence:
            self.labels.add("output_before", evidence["source_record"])
            final = evidence.get("final_record", evidence["source_record"])
            if self.preview and status in ("planned_update", "unchanged", "excluded"):
                final = evidence.get("candidate_record", final)
            self.labels.add("output", final)
            self.labels.add("examine" if attention else "completed", final)
        group = ("changed" if status in ("updated", "planned_update") else "unchanged") if self.split_output and scope == "source" else ""
        if attention and scope == "source":
            group = "examine"
            doc, _, pointer = record_path.partition("/")
            self.examine_positions.setdefault(Path(source_file), set()).add((int(doc[4:]), "/" + pointer if "/" in record_path else ""))
            self.examine_categories.setdefault(Path(source_file), {})[(int(doc[4:]), "/" + pointer if "/" in record_path else "")] = category
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
        if attention:
            self._archive_links(detail, category)
            self.examine_counts[category] += 1
            self._examine_number += 1
            if not self.preview:
                base = self.directory.parent
                if scope == "result" and result_file and not result_row:
                    target = self.result_archive.raw_path(category, result_file)
                    try:
                        (base / target).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(result_file, base / target)
                        detail["examine_data"] = target.as_posix()
                        detail["examine_results"] = [target.as_posix()]
                    except OSError as exc:
                        detail["archive_error"] = str(exc)
                atomic_text(base / "examine" / category / "details" / f"{self._examine_number:08d}.json",
                            json.dumps(detail, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        serialized = json.dumps(detail, ensure_ascii=False, allow_nan=False)
        self._all_details.write(serialized + "\n")
        link = review_link or self.details.add(detail, serialized)
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
                text, truncated = json_prefix(value, 180, indent=None)
                return text + "… (상세에서 전체 값 확인)" if truncated else text
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
                    serialized = json.dumps(detail, ensure_ascii=False)
                    item["link"] = self.details.add(detail, serialized)
                    self._all_details.write(serialized + "\n")
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
        label_issues = sum(self.counts[k] for k in ISSUES) + self.advisory_rows
        summary.update(status_counts=dict(self.counts), source_status_counts=dict(self.scope_counts["source"]),
                       result_issue_counts=dict(self.scope_counts["result"]), source_file_issue_counts=dict(self.scope_counts["source_file"]), changed_fields=self.change_count,
                       changed_files=len(self.changed_files), label_issue_rows=label_issues,
                       issue_rows=label_issues, review_findings=self.review_issue_count,
                       advisory_rows=self.advisory_rows, held_references=self.held_reference_count)
        category_counts = Counter(group for item in self.catalog for group in item["groups"])
        summary.update(category_counts=dict(category_counts), completed_records=category_counts["completed"],
                       examine_category_counts=dict(self.examine_counts),
                       output_records=category_counts["output"], unmatched_records=category_counts["unmatched"],
                       unmatched_source_records=self.scope_counts["source"]["no_result"],
                       unmatched_result_records=self.scope_counts["result"]["orphan_result"])
        summary["label_counts"] = self.labels.rows()
        with (self.directory / "label_counts.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["라벨 경로", "보완 경로", "집계 범위", "레코드 수", "배열 수", "라벨 수", "경로 누락", "배열 아님", "집계 불가", "처리 전후 증감"])
            for row in summary["label_counts"]:
                for side, counts in row["counts"].items():
                    writer.writerow([csv_safe(row["path"]), csv_safe(row["result_path"]), side,
                        *[counts[key] for key in ("records", "arrays", "items", "missing", "non_array", "unavailable")],
                        row["delta"] if side == "output" and row["delta"] is not None else ""])
        atomic_text(self.directory / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        write_dashboard(self.directory, summary, self.catalog, self.examples)
        return summary
