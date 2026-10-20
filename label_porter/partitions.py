"""Export disjoint record subsets without modifying the combined output."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .formats import DataError, Document, get_pointer, read_document, set_pointer, write_document


def _subset(document, spec, selected):
    """Return a document subset and old -> new record addresses, preserving order."""
    pointer = spec.get("records_path", "")
    mode = spec.get("record_mode", "auto")
    docs, addresses = [], []
    for old_doc_index, original in enumerate(document.docs):
        container = get_pointer(original, pointer)
        is_array = mode == "array" or (mode == "auto" and isinstance(container, list))
        if is_array:
            chosen = [(index, item) for index, item in enumerate(container)
                      if (old_doc_index, pointer + "/" + str(index)) in selected]
            if not chosen:
                continue
            new_doc_index = len(docs)
            subset = [item for _, item in chosen]
            if pointer:
                wrapper = copy.deepcopy(original)
                set_pointer(wrapper, pointer, subset)
            else:
                wrapper = subset
            docs.append(wrapper)
            addresses.extend(((old_doc_index, pointer + "/" + str(old_index)),
                              (new_doc_index, pointer + "/" + str(new_index)))
                             for new_index, (old_index, _) in enumerate(chosen))
        elif (old_doc_index, pointer) in selected:
            new_doc_index = len(docs)
            docs.append(original)
            addresses.append(((old_doc_index, pointer), (new_doc_index, pointer)))
    result = Document(docs, "", document.jsonl, document.bom, document.encoding)
    return result, addresses


def _copy_with_hash(source, destination, checkpoint):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with source.open("rb") as src, destination.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            checkpoint()
            digest.update(chunk)
            dst.write(chunk)
    return digest.hexdigest()


def export_partitions(run_dir: Path, source_base: Path, selected_files, record_positions,
                      changed_positions, file_manifest, spec, summary, manifest,
                      preview, checkpoint, progress):
    """A record goes to exactly one group; unparseable files remain unclassified."""
    summary["partition_status"] = "in_progress"
    counts = summary["partition_records"] = {"changed": 0, "unchanged": 0}
    file_counts = summary["partition_files"] = {"changed": 0, "unchanged": 0, "unclassified": 0}
    outputs = manifest["partition_outputs"] = []
    if not preview:
        for group in ("changed", "unchanged"):
            (run_dir / group).mkdir()
    with (run_dir / "reports" / "partition_index.jsonl").open("w", encoding="utf-8") as index_file:
        for number, relative in enumerate(sorted(selected_files), 1):
            checkpoint()
            positions = record_positions.get(relative)
            changed = changed_positions.get(relative, set())
            document = None
            if positions is None:
                groups = {"unclassified": None}
            else:
                positions_set = set(positions)
                if not changed.issubset(positions_set):
                    raise DataError(f"분리 대상 레코드 위치를 확인할 수 없습니다: {relative}")
                groups = {"changed": [item for item in positions if item in changed],
                          "unchanged": [item for item in positions if item not in changed]}
            for group, members in groups.items():
                if members == [] and not (group == "unchanged" and positions == []):
                    continue
                if members is not None:
                    counts[group] += len(members)
                file_counts[group] += 1
                output_relative = Path(group) / "labels" / relative
                destination = run_dir / output_relative
                whole_file = positions is None or len(members) == len(positions)
                if whole_file:
                    addresses = [(address, address) for address in members or []]
                else:
                    if document is None:
                        document = read_document(source_base / relative, spec.get("encoding", "utf-8-sig"), "json")
                        expected_hash = file_manifest[relative].get("output_sha256", file_manifest[relative]["source_sha256"])
                        if document.sha256 != expected_hash:
                            raise DataError(f"분리 저장 중 파일 내용이 달라졌습니다: {relative}")
                    subset, addresses = _subset(document, spec, set(members))
                    if len(addresses) != len(members):
                        raise DataError(f"분리 저장할 레코드 개수가 맞지 않습니다: {relative}")
                entry = {"group": group, "source_file": relative.as_posix(),
                         "path": output_relative.as_posix(), "records": len(members) if members is not None else None}
                if not preview:
                    if whole_file:
                        digest = _copy_with_hash(source_base / relative, destination, checkpoint)
                        if digest != file_manifest[relative]["output_sha256"]:
                            raise DataError(f"분리 복사본 해시가 일치하지 않습니다: {relative}")
                    else:
                        write_document(destination, subset)
                        with destination.open("rb") as handle:
                            digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    entry["sha256"] = digest
                outputs.append(entry)
                for old, new in addresses:
                    index_file.write(json.dumps({"group": group, "source_file": relative.as_posix(),
                        "source_record_path": f"doc:{old[0]}{old[1]}", "partition_file": output_relative.as_posix(),
                        "partition_record_path": f"doc:{new[0]}{new[1]}", "preview": preview}, ensure_ascii=False) + "\n")
            if number % 100 == 0 or number == len(selected_files):
                progress("partition", f"변경/미변경 분리 {number:,}/{len(selected_files):,}", number, len(selected_files))
    if sum(counts.values()) != summary["source_records"]:
        raise DataError("분리 저장한 레코드 수와 원본 레코드 수가 일치하지 않습니다.")
    summary["partition_status"] = "planned" if preview else "completed"
