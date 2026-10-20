"""Independently verify a successful run against source hashes and reported changes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from label_porter.formats import equivalent, get_pointer, read_document, records, set_pointer


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def address(document, pointer):
    match = re.fullmatch(r"doc:(\d+)(.*)", pointer)
    assert match, "레코드 경로 형식 오류"
    return get_pointer(document.docs[int(match.group(1))], match.group(2))


def verify_partitions(directory, manifest, summary, config):
    if not summary.get("split_output"):
        return
    assert summary["partition_status"] == "completed", "분리 저장이 완료되지 않았습니다."
    entries = manifest["partition_outputs"]
    paths = {entry["path"] for entry in entries}
    actual_paths = {path.relative_to(directory).as_posix() for group in ("changed", "unchanged", "unclassified", "examine")
                    for path in (directory / group / "labels").rglob("*") if path.is_file()}
    actual_paths.update(path.relative_to(directory).as_posix() for path in (directory / "examine" / "unparsed").rglob("*") if path.is_file())
    actual_paths.update(path.relative_to(directory).as_posix() for path in (directory / "examine").glob("*/labels/**/*") if path.is_file())
    actual_paths.update(path.relative_to(directory).as_posix() for path in (directory / "examine/file_errors/files/source").rglob("*") if path.is_file())
    assert paths == actual_paths, "분리 라벨 파일 누락/추가"
    rows_by_source = {}
    with (directory / "reports" / "partition_index.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows_by_source.setdefault(row["source_file"], []).append(row)
    spec = config["source"]
    counts = {key: 0 for key in summary["partition_records"]}
    with (directory / "reports" / "records.csv").open(encoding="utf-8-sig", newline="") as handle:
        examined = {(r["source_file"], r["record_path"]) for r in csv.DictReader(handle) if r["scope"] == "source" and r["output_group"] == "examine"}
    for entry in entries:
        path = directory / entry["path"]
        assert digest(path) == entry["sha256"], f"분리 라벨 해시 불일치: {path}"
        if entry["group"] == "unclassified":
            assert path.read_bytes() == (directory / "data" / entry["source_file"]).read_bytes()
        else:
            document = read_document(path, spec.get("encoding", "utf-8-sig"), "json")
            assert len(list(records(document, spec))) == entry["records"], "분리 라벨 건수 불일치"
    for source_file, rows in rows_by_source.items():
        original = read_document(Path(manifest["source_directory"]) / source_file, spec.get("encoding", "utf-8-sig"), "json")
        combined = read_document(directory / "data" / source_file, spec.get("encoding", "utf-8-sig"), "json")
        expected_addresses = {f"doc:{doc_index}{pointer}" for doc_index, pointer, _ in records(combined, spec)}
        seen_source, seen_destination = set(), set()
        subsets = {}
        for row in rows:
            old_address = row["source_record_path"]
            destination = (row["partition_file"], row["partition_record_path"])
            assert old_address not in seen_source and destination not in seen_destination, "분리본에 중복 레코드가 있습니다."
            seen_source.add(old_address)
            seen_destination.add(destination)
            if row["partition_file"] not in subsets:
                subsets[row["partition_file"]] = read_document(directory / row["partition_file"], spec.get("encoding", "utf-8-sig"), "json")
            before, after = address(original, old_address), address(combined, old_address)
            expected_group = "examine" if (source_file, old_address) in examined else "unchanged" if equivalent(before, after) else "changed"
            assert row["group"] == expected_group, "실제 변경 여부와 분리 그룹이 다릅니다."
            expected_record = before if row["group"] == "examine" and manifest.get("examine_original_inputs") else after
            assert equivalent(address(subsets[row["partition_file"]], row["partition_record_path"]), expected_record), "분리본 내용이 저장 기준과 다릅니다."
            counts[row["group"]] += 1
        assert seen_source == expected_addresses, "분리본에 원본 레코드가 누락되었습니다."
    assert counts == summary["partition_records"], "분리 집계 불일치"
    assert sum(counts.values()) == summary["source_records"], "분리본 합계가 원본과 다릅니다."


def verify(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "reports" / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    assert not manifest["preview"], "미리보기에는 데이터 복사본이 없습니다."
    assert summary["status"] in ("completed", "completed_with_issues"), "정상 종료된 실행만 검증할 수 있습니다."
    source = Path(manifest["source_directory"])
    changes_by_file = {}
    with (directory / "reports" / "changes.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            change = json.loads(line)
            changes_by_file.setdefault(change["source_file"], []).append(change)
    actual_names = {path.relative_to(directory / "data").as_posix() for path in (directory / "data").rglob("*") if path.is_file()}
    assert actual_names == {entry["path"] for entry in manifest["files"]}, "복사본의 파일 목록이 명세와 다릅니다."
    for number, entry in enumerate(manifest["files"], 1):
        original = source / entry["path"]
        output = directory / "data" / entry["path"]
        assert digest(original) == entry["source_sha256"], f"원본 내용 변경: {entry['path']}"
        assert digest(output) == entry["output_sha256"], f"결과 해시 불일치: {entry['path']}"
        if not entry["changed"]:
            assert entry["source_sha256"] == entry["output_sha256"], f"미변경 파일이 달라짐: {entry['path']}"
        else:
            expected = read_document(original, config["source"].get("encoding", "utf-8-sig"), "json")
            actual = read_document(output, config["source"].get("encoding", "utf-8-sig"), "json")
            for change in changes_by_file.get(entry["path"], []):
                match = re.fullmatch(r"doc:(\d+)(.*)", change["record_path"])
                assert match, "변경 보고서의 레코드 경로가 잘못되었습니다."
                record = get_pointer(expected.docs[int(match.group(1))], match.group(2))
                if change["operation"] == "replace":
                    assert equivalent(get_pointer(record, change["path"]), change["before"]), "변경 전 값이 보고서와 다릅니다."
                set_pointer(record, change["path"], change["after"], change["operation"] == "add")
            assert equivalent(expected.docs, actual.docs), f"보고서에 없는 변경 발생: {entry['path']}"
            del expected, actual
        if number % 2000 == 0:
            print(f"verified {number}/{len(manifest['files'])}", flush=True)
    assert sum(summary["source_status_counts"].values()) == summary["source_records"], "원본 상태 집계 불일치"
    assert sum(len(items) for items in changes_by_file.values()) == summary["changed_fields"], "변경 필드 집계 불일치"
    verify_partitions(directory, manifest, summary, config)
    print(json.dumps({"verified": True, "files": len(manifest["files"]), "changed_files": len(changes_by_file), "originals_unchanged": True, "only_reported_changes": True, "partition_records": summary.get("partition_records")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    verify(parser.parse_args().directory)
