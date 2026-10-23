"""Portable, versioned report data. Report merging never edits labeling JSON."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

FORMAT = {"type": "format", "schema": "label-porter.report", "schema_version": 1}
TYPES = {"run", "catalog", "change", "review", "detail"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def line(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def rows(path):
    with Path(path).open(encoding="utf-8") as stream:
        for text in stream:
            if text.strip():
                yield json.loads(text)


def write_data(path, events, metadata=None):
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        text = line({**FORMAT, "metadata": metadata or {}})
        stream.write(text); digest.update(text.encode())
        for item in events:
            text = line(item); stream.write(text); digest.update(text.encode()); count += 1
        stream.write(line({"type": "end", "entries": count, "sha256": digest.hexdigest()}))


def validate_data(path):
    digest, count, ended, runs = hashlib.sha256(), 0, False, set()
    with Path(path).open(encoding="utf-8") as stream:
        for number, text in enumerate(stream):
            item = json.loads(text)
            if number == 0:
                if any(item.get(k) != v for k, v in FORMAT.items()):
                    raise ValueError("지원하지 않는 결과서 데이터 형식입니다.")
            elif item.get("type") == "end":
                if item.get("entries") != count or item.get("sha256") != digest.hexdigest():
                    raise ValueError("결과서 데이터가 누락되거나 변경되었습니다.")
                ended = True
                if stream.read().strip(): raise ValueError("결과서 종료 정보 뒤에 데이터가 있습니다.")
                break
            else:
                if item.get("type") not in TYPES or not isinstance(item.get("data"), dict):
                    raise ValueError("결과서 행 구조가 올바르지 않습니다.")
                run_id = item.get("run_id")
                if not isinstance(run_id, str) or len(run_id) != 64:
                    raise ValueError("작업 식별자가 올바르지 않습니다.")
                if item["type"] == "run":
                    if run_id in runs: raise ValueError("같은 작업의 정보가 중복되었습니다.")
                    runs.add(run_id)
                elif run_id not in runs:
                    raise ValueError("출처 작업이 없는 결과 행입니다.")
                count += 1
            digest.update(text.encode())
    if not ended or not runs: raise ValueError("완전한 결과서 데이터가 아닙니다.")
    return runs


def native_data(directory, destination, summary=None, catalog=None, checkpoint=lambda: None):
    directory = Path(directory)
    summary = summary if summary is not None else json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    catalog = catalog if catalog is not None else json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or not isinstance(catalog, list):
        raise ValueError("라벨 포터 처리 결과서 폴더를 선택하세요.")
    streams = [("change", "changes.jsonl"), ("review", "review_details.jsonl"), ("detail", "record_details.jsonl")]
    digest = hashlib.sha256(line(summary).encode())
    for item in catalog:
        checkpoint(); digest.update(line(item).encode())
    for _, filename in streams:
        path = directory / filename
        if not path.is_file(): raise ValueError(f"결과서 구성 파일이 없습니다: {filename}")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checkpoint(); digest.update(chunk)
    run_id = digest.hexdigest()
    def events():
        yield {"type": "run", "run_id": run_id, "data": summary}
        for item in catalog:
            checkpoint(); yield {"type": "catalog", "run_id": run_id, "data": item}
        for kind, filename in streams:
            for item in rows(directory / filename):
                checkpoint(); yield {"type": kind, "run_id": run_id, "data": item}
    write_data(destination, events())
    return Path(destination)


def embedded_rows(path, checkpoint=lambda: None):
    """Read only our hidden schema sheets; never evaluate workbook formulas."""
    with zipfile.ZipFile(path) as archive:
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        sheets = [s for s in wb.find("m:sheets", NS) if s.attrib["name"].startswith("_porter_data")]
        if not sheets: raise ValueError("병합 데이터가 없는 Excel입니다. 이전 버전 결과서는 같은 reports 폴더의 summary.json을 선택하세요.")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(t.text or "" for t in s.findall(".//m:t", NS)) for s in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        expected, fragments, total, encoding = 1, [], 0, None
        for sheet in sheets:
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rels[rid]
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            with archive.open(target) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag != "{" + NS["m"] + "}row": continue
                    checkpoint()
                    cells = {}
                    for cell in element:
                        col = "".join(c for c in cell.attrib.get("r", "") if c.isalpha())
                        value = cell.findtext("m:v", default="", namespaces=NS)
                        if cell.attrib.get("t") == "inlineStr": value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                        elif cell.attrib.get("t") == "s": value = shared[int(value)]
                        # Decode OOXML escapes once, preserving literal _xNNNN_ strings.
                        cells[col] = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m[1], 16)), value)
                    element.clear()
                    if cells.get("A") == "entry":
                        if cells.get("D") not in ("jsonl", "data"): raise ValueError("지원하지 않는 Excel 내장 데이터 인코딩입니다.")
                        encoding = cells["D"]
                        continue
                    if int(cells.get("A", 0)) != expected or int(cells.get("B", 0)) != len(fragments) + 1:
                        raise ValueError("Excel 병합 데이터의 순서가 손상되었습니다.")
                    if not fragments: total = int(cells["C"])
                    if total < 1 or int(cells["C"]) != total: raise ValueError("병합 데이터 조각 수가 다릅니다.")
                    fragments.append(cells["D"])
                    if len(fragments) == total:
                        decoded = "".join(fragments)
                        if encoding == "data": decoded = zlib.decompress(base64.b64decode(decoded, validate=True)).decode("utf-8")
                        elif encoding != "jsonl": raise ValueError("Excel 병합 데이터의 머리글이 없습니다.")
                        yield decoded
                        expected += 1; fragments = []
        if fragments: raise ValueError("Excel 병합 데이터가 중간에 끊어졌습니다.")


def import_data(source, destination, checkpoint=lambda: None):
    source, destination = Path(source), Path(destination)
    if source.suffix.lower() == ".xlsx":
        with destination.open("w", encoding="utf-8", newline="\n") as stream:
            for text in embedded_rows(source, checkpoint): stream.write(text)
    elif source.name == "summary.json" or source.is_dir():
        directory = source if source.is_dir() else source.parent
        if (directory / "reports/summary.json").is_file(): directory /= "reports"
        native_data(directory, destination, checkpoint=checkpoint)
    elif source.suffix.lower() == ".jsonl":
        import shutil
        shutil.copyfile(source, destination)
    else:
        raise ValueError("라벨 포터 결과서 .xlsx 또는 summary.json을 선택하세요.")
    validate_data(destination)
    return destination


def _publish_files(pairs, temporary):
    """Rollback both outputs if one target is locked or cannot be replaced."""
    backups, published = [], []
    try:
        for number, (_, target) in enumerate(pairs):
            if target.exists():
                backup = Path(temporary) / f"previous-{number}"
                os.replace(target, backup)
                backups.append((backup, target))
        for staged, target in pairs:
            os.replace(staged, target); published.append(target)
    except OSError:
        for target in reversed(published): target.unlink(missing_ok=True)
        for backup, target in reversed(backups): os.replace(backup, target)
        raise


def merge_reports(inputs, output, metadata=None, progress=lambda message: None, cancel=None):
    """Deduplicate whole runs; retain all distinct runs and their original values."""
    from .excel_report import export_workbook
    output = Path(output).resolve()
    if output.suffix.lower() != ".xlsx": raise ValueError("저장 파일 확장자는 .xlsx여야 합니다.")
    inputs = [Path(p).resolve() for p in inputs]
    if not inputs: raise ValueError("병합할 결과서를 선택하세요.")
    sidecar = output.with_suffix(".data.jsonl")
    if output in inputs or sidecar in inputs: raise ValueError("입력 결과서와 다른 저장 파일을 선택하세요.")
    def checkpoint():
        if cancel is not None and cancel.is_set(): raise InterruptedError("결과서 병합을 중단했습니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="porter-merge-", dir=output.parent) as tmp:
        paths, seen, duplicate = [], {}, 0
        for n, source in enumerate(inputs, 1):
            checkpoint(); progress(f"결과서 읽는 중 {n}/{len(inputs)} · {source.name}")
            path = import_data(source, Path(tmp) / f"input-{n}.jsonl", checkpoint)
            accepted, source_metadata = set(), {}
            for event in rows(path):
                checkpoint()
                if event["type"] == "format": source_metadata = event.get("metadata", {})
                if event["type"] == "run":
                    run_id = event["run_id"]
                    if run_id in seen:
                        if event["data"] != seen[run_id]: raise ValueError("동일 작업 ID의 집계가 서로 다릅니다.")
                        duplicate += 1
                    else:
                        seen[run_id] = event["data"]; accepted.add(run_id)
            paths.append((path, accepted, source_metadata))
        def events():
            for path, accepted, source_metadata in paths:
                for event in rows(path):
                    checkpoint()
                    if event.get("run_id") in accepted:
                        if event["type"] == "run":
                            event["display"] = {**source_metadata, **event.get("display", {}), **{k:v for k,v in (metadata or {}).items() if v}}
                        yield event
        data = Path(tmp) / "report_data.jsonl"
        write_data(data, events(), {**(metadata or {}), "duplicate_runs_skipped": duplicate})
        progress(f"{len(seen)}개 작업의 Excel 작성 중 · 동일 작업 {duplicate}개 제외")
        staged = Path(tmp) / "report.xlsx"
        export_workbook(data, staged, checkpoint=checkpoint)
        checkpoint()
        # The workbook contains complete merge data; its sidecar is a convenience for AI/tools.
        _publish_files([(staged, output), (data, sidecar)], tmp)
    return {"path": str(output), "data_path": str(sidecar), "runs": len(seen), "duplicates_skipped": duplicate}
