from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import validate_config
from .diagnostics import diagnose_record
from .formats import (DataError, atomic_text, canonical_key, get_pointer, read_document,
                      records, set_pointer, write_document)
from .reports import ISSUES, Reports
from .partitions import export_partitions
from .rules import ReviewRequired, apply_record, expression
from .storage import now, output_session


class Cancelled(Exception):
    pass


@dataclass
class RunResult:
    directory: Path
    summary: dict


def _now():
    return now().isoformat(timespec="seconds")


def _meta(relative: Path, result_file="", result_row=""):
    return {"file_name": relative.name, "relative_path": relative.as_posix(), "file_stem": relative.stem,
            "result_file": result_file, "result_row": result_row}


def _context(record, meta):
    return {"source": record, "original": record, "result": record,
            "root_source": record, "root_original": record, "root_result": record, "meta": meta}


def _hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _unreadable_evidence(path, encoding):
    try:
        return {"input_text": path.read_bytes().decode(encoding, errors="replace"),
                "input_note": "해석할 수 없는 입력입니다. 잘못된 인코딩 바이트는 대체 문자로 표시됩니다."}
    except (OSError, LookupError) as exc:
        return {"input_read_error": str(exc)}


def _inventory(source: Path):
    files = []
    for current, directories, names in os.walk(source, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise DataError(f"원본 내부의 링크/정션은 복사하지 않습니다: {path}")
            if not path.resolve().is_relative_to(source):
                raise DataError(f"원본 폴더 밖을 가리키는 경로입니다: {path}")
        for name in names:
            path = Path(current) / name
            if not path.is_file():
                raise DataError(f"일반 파일이 아닙니다: {path}")
            files.append(path.relative_to(source))
    return sorted(files, key=lambda path: path.as_posix())


def run(source_directory: Path | str, result_files: list[Path | str], output_directory: Path | str,
        config: dict, preview=False, progress: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None, split_output=True) -> RunResult:
    config = copy.deepcopy(validate_config(config))
    source = Path(source_directory).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    if not source.is_dir():
        raise DataError("원본 폴더를 찾을 수 없습니다.")
    if output == source or output.is_relative_to(source):
        raise DataError("결과 저장 위치는 원본 폴더 밖으로 지정해야 합니다.")
    result_paths = [Path(path).expanduser().resolve() for path in result_files]
    if not result_paths or any(not path.is_file() for path in result_paths):
        raise DataError("존재하는 작업 결과 파일을 하나 이상 선택하세요.")
    if len(set(result_paths)) != len(result_paths):
        raise DataError("같은 결과 파일이 두 번 선택되었습니다.")
    inventory = _inventory(source)
    selected = {path.relative_to(source) for path in source.glob(config["source"].get("glob", "**/*.json")) if path.is_file()}
    if not selected:
        raise DataError("source.glob과 일치하는 원본 JSON 파일이 없습니다.")
    if not selected.issubset(set(inventory)):
        raise DataError("원본 선택 범위를 확인할 수 없습니다.")
    if any(path.is_relative_to(source) and path.relative_to(source) in selected for path in result_paths):
        raise DataError("작업 결과 파일이 원본 선택 범위에도 포함되었습니다. 원본 폴더/패턴을 분리하세요.")
    with output_session(output, preview, source, result_paths) as run_dir:
        return _execute_run(run_dir, source, result_paths, inventory, selected, config,
                            preview, progress, cancel, split_output)


def _execute_run(run_dir, source, result_paths, inventory, selected, config,
                 preview, progress, cancel, split_output):
    data_dir = run_dir / "data"
    report = Reports(run_dir / "reports", split_output=split_output, preview=preview, result_spec=config["result"])
    summary = {"version": 4, "profile": config["name"], "preview": bool(preview), "status": "running",
               "started_at": _now(), "source_directory": str(source), "run_directory": str(run_dir),
               "split_output": bool(split_output), "partition_status": "pending" if split_output else "disabled",
               "source_files": len(inventory), "label_files": len(selected), "source_records": 0, "result_records": 0}
    manifest = {"source_directory": str(source), "preview": bool(preview), "result_files": [], "files": [],
                "examine_original_inputs": True}
    atomic_text(run_dir / "profile.json", json.dumps(config, ensure_ascii=False, indent=2))
    atomic_text(run_dir / "run_status.json", json.dumps(summary, ensure_ascii=False, indent=2))

    def emit(phase, message, done=0, total=0):
        if progress:
            progress({"phase": phase, "message": message, "done": done, "total": total})

    def checkpoint():
        if cancel and cancel.is_set():
            raise Cancelled("사용자가 실행을 중단했습니다. 원본은 변경하지 않았습니다.")

    result_index = defaultdict(list)
    source_index = defaultdict(list)
    file_locations = defaultdict(list)
    record_positions = {}
    changed_positions = defaultdict(set)
    manifest_by_file = {}
    try:
        emit("result", "작업 결과 파일을 읽는 중입니다.")
        for index, path in enumerate(result_paths, 1):
            checkpoint()
            spec = config["result"]
            last_read_update = -1.0
            last_read_done = 0
            def read_progress(done, total, count):
                nonlocal last_read_update, last_read_done
                now = time.monotonic()
                if now - last_read_update >= 0.2 or (done > 0 and last_read_done == 0):
                    last_read_update = now
                    last_read_done = done
                    emit("result_read", f"결과 {index}/{len(result_paths)} · 읽기·JSON 해석 "
                         f"{done / 1048576:,.1f}/{total / 1048576:,.1f} MB · {count:,}건 해석", done, total)
            try:
                document = read_document(path, spec.get("encoding", "utf-8-sig"), spec.get("format", "auto"),
                                         spec.get("csv_json_columns", []), progress=read_progress, checkpoint=checkpoint)
                # Materialize container discovery before indexing to avoid partial-file acceptance.
                rows = []
                for location in records(document, spec):
                    checkpoint()
                    rows.append(location)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                report.record("result", "invalid_result", result_file=str(path), reason=str(exc),
                              evidence=_unreadable_evidence(path, spec.get("encoding", "utf-8-sig")))
                manifest["result_files"].append({"path": str(path), "error": str(exc)})
                continue
            manifest["result_files"].append({"path": str(path), "sha256": document.sha256, "records": len(rows)})
            report.result_archive.register(path, document, rows)
            invalid_rows = 0
            last_index_update = time.monotonic()
            emit("result_index", f"결과 {index}/{len(result_paths)} · 연결키 검사 0/{len(rows):,}건", 0, len(rows))
            for row_number, (_, _, row) in enumerate(rows, 1):
                checkpoint()
                summary["result_records"] += 1
                try:
                    if not isinstance(row, dict):
                        raise DataError("작업 결과 레코드가 객체가 아닙니다.")
                    value = expression(spec["key"], _context(row, _meta(path, str(path), row_number)))
                    key = canonical_key(value)
                    result_index[key].append({"row": row, "file": str(path), "number": row_number, "key": value})
                    report.result_input(str(path), row_number, value, row)
                except (ValueError, TypeError, KeyError) as exc:
                    invalid_rows += 1
                    report.record("result", "invalid_result", result_file=str(path), result_row=row_number, reason=str(exc), evidence={"platform_record": row})
                now = time.monotonic()
                if now - last_index_update >= 0.2 or row_number == len(rows):
                    last_index_update = now
                    emit("result_index", f"결과 {index}/{len(result_paths)} · 연결키 검사 {row_number:,}/{len(rows):,}건"
                         f" · 오류 {invalid_rows:,}건 (검토 근거 저장)", row_number, len(rows))
            # The index retains valid rows. Release the last file's full container,
            # especially when every record has an invalid key.
            del rows, document
            emit("result", f"작업 결과 {index}/{len(result_paths)}개 파일 읽음", index, len(result_paths))

        if not preview:
            emit("copy", f"원본 파일 {len(inventory):,}개를 새 폴더에 복사합니다.")
            data_dir.mkdir()
            for current, directories, _ in os.walk(source):
                for name in directories:
                    (data_dir / (Path(current) / name).relative_to(source)).mkdir(parents=True, exist_ok=True)
            for index, relative in enumerate(inventory, 1):
                checkpoint()
                original, destination = source / relative, data_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with original.open("rb") as src, destination.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        checkpoint()
                        digest.update(chunk)
                        dst.write(chunk)
                # Preserve timestamps, but keep copies writable even when sources are read-only.
                stat = original.stat()
                os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                entry = {"path": relative.as_posix(), "source_sha256": digest.hexdigest(), "output_sha256": digest.hexdigest(), "changed": False}
                manifest["files"].append(entry)
                manifest_by_file[relative] = entry
                if index % 100 == 0 or index == len(inventory):
                    emit("copy", f"원본 복사 {index:,}/{len(inventory):,}", index, len(inventory))
        base = source if preview else data_dir
        emit("index", "원본 레코드의 연결키를 검사합니다.")
        for index, relative in enumerate(sorted(selected), 1):
            checkpoint()
            try:
                document = read_document(base / relative, config["source"].get("encoding", "utf-8-sig"), "json")
                found = list(records(document, config["source"]))
                record_positions[relative] = [(doc_index, pointer) for doc_index, pointer, _ in found]
                report.source_input(relative.as_posix(), record_positions[relative])
                if not found:
                    report.record("source_file", "invalid_source", source_file=relative.as_posix(), reason="선택한 레코드 배열이 비어 있습니다.")
                if preview:
                    entry = {"path": relative.as_posix(), "source_sha256": document.sha256, "changed": False}
                    manifest["files"].append(entry)
                    manifest_by_file[relative] = entry
                for doc_index, pointer, row in found:
                    summary["source_records"] += 1
                    try:
                        if not isinstance(row, dict):
                            raise DataError("원본 레코드가 객체가 아닙니다.")
                        value = expression(config["source"]["key"], _context(row, _meta(relative)))
                        key = canonical_key(value)
                        location = (relative, doc_index, pointer, value, key)
                        source_index[key].append(location)
                        file_locations[relative].append(location)
                    except (ValueError, TypeError, KeyError) as exc:
                        report.record("source", "invalid_source", source_file=relative.as_posix(), record_path=f"doc:{doc_index}{pointer}", reason=str(exc), evidence={"source_record": row, "final_record": row})
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                report.record("source_file", "invalid_source", source_file=relative.as_posix(), reason=str(exc),
                              evidence=_unreadable_evidence(base / relative, config["source"].get("encoding", "utf-8-sig")))
            if index % 100 == 0 or index == len(selected):
                emit("index", f"원본 검사 {index:,}/{len(selected):,}", index, len(selected))

        def source_evidence(key):
            snapshots = []
            for relative, doc_index, pointer, value, _ in source_index.get(key, []):
                doc = read_document(base / relative, config["source"].get("encoding", "utf-8-sig"), "json")
                snapshots.append({"file": relative.as_posix(), "record_path": f"doc:{doc_index}{pointer}",
                                  "key": value, "record": get_pointer(doc.docs[doc_index], pointer)})
            return snapshots

        emit("apply", "매칭 및 필드 반영 규칙을 적용합니다.")
        total = len(file_locations)
        for file_number, (relative, locations) in enumerate(file_locations.items(), 1):
            checkpoint()
            document = None
            pending = []
            for _, doc_index, pointer, value, key in locations:
                checkpoint()
                source_path = f"doc:{doc_index}{pointer}"
                matches = result_index.get(key, [])
                detail = {"scope": "source", "source_file": relative.as_posix(), "record_path": source_path, "key": value}
                if document is None:
                    document = read_document(base / relative, config["source"].get("encoding", "utf-8-sig"), "json")
                    if document.sha256 != manifest_by_file[relative]["source_sha256"]:
                        raise DataError(f"검사 중 원본/복사본이 변경되었습니다: {relative}")
                original = get_pointer(document.docs[doc_index], pointer)
                if len(source_index[key]) > 1:
                    report.record(**detail, status="duplicate_source_key", reason="원본 연결키가 중복되어 자동 반영하지 않았습니다.",
                                  evidence={"source_record": original, "final_record": original, "source_candidates": source_evidence(key), "platform_candidates": matches})
                    continue
                if not matches:
                    report.record(**detail, status="no_result", reason="대응하는 보완 데이터가 없습니다. 원본을 보존하고 examine으로 분리합니다.", evidence={"source_record": original, "final_record": original})
                    continue
                if len(matches) > 1:
                    report.record(**detail, status="duplicate_result", reason=f"같은 키의 작업 결과 {len(matches)}건이 있습니다. 최신 값을 임의 선택하지 않았습니다.",
                                  evidence={"source_record": original, "final_record": original, "source_candidates": source_evidence(key), "platform_candidates": matches})
                    continue
                match = matches[0]
                detail.update(result_file=match["file"], result_row=match["number"])
                try:
                    advisories = []
                    candidate, changes, notes, status = apply_record(original, match["row"], config, _meta(relative, match["file"], match["number"]), advisories=advisories)
                    if changes:
                        if pointer == "":
                            document.docs[doc_index] = candidate
                        else:
                            set_pointer(document.docs[doc_index], pointer, candidate)
                    evidence = {"issues": advisories, "source_record": original, "platform_record": match["row"],
                                "final_record": original if preview else candidate, "candidate_record": candidate,
                                "applied_with_advisories": bool(advisories), "preview": bool(preview), "changes": changes,
                                "candidate_note": "검증을 통과한 반영 예정 값입니다." if preview else "복사본에 반영한 값입니다."}
                    if not preview and not advisories:
                        evidence.pop("candidate_record")
                        evidence.pop("candidate_note")
                    pending.append((detail, "planned_update" if preview and status == "updated" else status, changes, notes, evidence))
                except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
                    try:
                        evidence = diagnose_record(original, match["row"], config, _meta(relative, match["file"], match["number"]), exc)
                    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as diagnostic_error:
                        evidence = {"source_record": original, "platform_record": match["row"], "final_record": original,
                                    "diagnostic_error": str(diagnostic_error),
                                    "issues": [{"code": "validation_error", "message": str(exc), "source_path": pointer}]}
                    report.record(**detail, status="review_required", reason=str(exc), evidence=evidence)
            changed = any(changes for _, _, changes, _, _ in pending)
            if changed:
                checkpoint()
                if not preview:
                    destination = (data_dir / relative).resolve()
                    if not destination.is_relative_to(data_dir.resolve()):
                        raise DataError("결과 폴더 밖에 쓸 수 없습니다.")
                    write_document(destination, document)
                    manifest_by_file[relative]["output_sha256"] = _hash(destination)
                manifest_by_file[relative]["changed"] = True
            for detail, status, changes, notes, evidence in pending:
                if changes:
                    doc_part, _, pointer_part = detail["record_path"].partition("/")
                    changed_positions[relative].add((int(doc_part[4:]), "/" + pointer_part if "/" in detail["record_path"] else ""))
                report.record(**detail, status=status, changed_fields=len(changes), evidence=evidence, reason="; ".join(notes) or ("허용된 필드만 반영했습니다." if changes else "수정 대상 조건이 없거나 값이 이미 같습니다."))
                report.changes(detail["source_file"], detail["record_path"], detail["key"], changes, detail["result_file"], detail["result_row"], preview)
            if file_number % 100 == 0 or file_number == total:
                emit("apply", f"반영 검사 {file_number:,}/{total:,}", file_number, total)

        for key, matches in result_index.items():
            checkpoint()
            if key not in source_index:
                for match in matches:
                    report.record("result", "orphan_result", key=match["key"], result_file=match["file"], result_row=match["number"], reason="선택한 원본에 연결할 키가 없습니다.", evidence={"platform_record": match["row"]})
            elif len(matches) > 1 or len(source_index[key]) > 1:
                for match in matches:
                    status = "duplicate_result" if len(matches) > 1 else "duplicate_source_key"
                    report.record("result", status, key=match["key"], result_file=match["file"], result_row=match["number"], reason="중복 키로 인해 연결을 확정할 수 없습니다.",
                                  evidence={"source_candidates": source_evidence(key), "platform_candidates": matches})
        emit("partition", "검토 대상은 examine으로, 정상 데이터는 처리 상태별로 분리합니다.")
        export_partitions(run_dir, base, selected, record_positions, changed_positions,
                          manifest_by_file, config["source"], summary, manifest, preview, checkpoint, emit,
                          report.examine_positions, report.examine_files, split_output, report.examine_categories, original_source=source)
        report.result_archive.export(run_dir, preview, checkpoint, emit)
        summary["status"] = "completed_with_issues" if any(report.counts[key] for key in ISSUES) or report.advisory_rows or report.counts["no_result"] else "completed"
    except Cancelled as exc:
        summary.update(status="cancelled", error=str(exc))
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        emit("report", "검토 보고서를 마무리하고 있습니다.")
        summary["finished_at"] = _now()
        summary = report.finish(summary)
        manifest["status"] = summary["status"]
        atomic_text(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        atomic_text(run_dir / "run_status.json", json.dumps(summary, ensure_ascii=False, indent=2))
    emit("done", f"실행 종료: {summary['status']} · {run_dir}")
    return RunResult(run_dir, summary)
