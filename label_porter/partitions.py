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


def _single(document, spec, address):
    """Extract one record without copying/scanning the other array records."""
    doc_index, record_pointer = address
    pointer = spec.get("records_path", "")
    original = document.docs[doc_index]
    container = get_pointer(original, pointer)
    mode = spec.get("record_mode", "auto")
    if mode == "array" or (mode == "auto" and isinstance(container, list)):
        index = int(record_pointer[len(pointer) + 1:])
        selected = [container[index]]
        # JSON input has no object aliases. Skip the large container during copy.
        root = copy.deepcopy(original, {id(container): selected}) if pointer else selected
        new_pointer = pointer + "/0"
    else:
        root, new_pointer = original, pointer
    return Document([root], "", document.jsonl, document.bom, document.encoding), [(address, (0, new_pointer))]


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
                      preview, checkpoint, progress, examine_positions=None, examine_files=None, split_output=True, examine_categories=None,
                      original_source=None):
    """A record goes to exactly one group; unparseable files remain unclassified."""
    from .examine import label_path
    examine_positions, examine_files = examine_positions or {}, examine_files or set()
    examine_categories = examine_categories or {}
    summary["partition_status"] = "in_progress"
    counts = summary["partition_records"] = {"changed": 0, "unchanged": 0, "examine": 0}
    file_counts = summary["partition_files"] = {"changed": 0, "unchanged": 0, "examine": 0, "unclassified": 0}
    outputs = manifest["partition_outputs"] = []
    if not preview:
        for group in (("changed", "unchanged", "examine") if split_output else ("examine",)):
            (run_dir / group).mkdir(exist_ok=True)
    with (run_dir / "reports" / ("partition_index.jsonl" if split_output else "examine_index.jsonl")).open("w", encoding="utf-8") as index_file:
        for number, relative in enumerate(sorted(selected_files), 1):
            checkpoint()
            positions = record_positions.get(relative)
            changed = changed_positions.get(relative, set())
            documents = {}
            if positions is None:
                groups = {"unclassified": None}
            else:
                positions_set = set(positions)
                if not changed.issubset(positions_set):
                    raise DataError(f"분리 대상 레코드 위치를 확인할 수 없습니다: {relative}")
                examined = set(positions) if relative in examine_files else examine_positions.get(relative, set())
                if not examined.issubset(positions_set):
                    raise DataError(f"검토 레코드 위치를 확인할 수 없습니다: {relative}")
                groups = {"examine": [item for item in positions if item in examined]}
                if split_output:
                    groups.update(changed=[item for item in positions if item in changed and item not in examined],
                                  unchanged=[item for item in positions if item not in changed and item not in examined])
            exports = []
            for group, members in groups.items():
                if group == "examine" and members:
                    # One record per file, even if its input was a large JSON array.
                    for ordinal, address in enumerate(positions, 1):
                        if address not in examined: continue
                        category = "file_errors" if relative in examine_files else examine_categories.get(relative, {}).get(address, "review")
                        target = label_path(category, relative, ordinal)
                        exports.append((group, [address], target, category))
                elif group == "unclassified" or (group == "examine" and positions == [] and relative in examine_files):
                    exports.append((group, members, Path("examine/file_errors/files/source") / relative, "file_errors"))
                elif members:
                    exports.append((group, members, Path(group) / "labels" / relative, ""))
            for group, members, output_relative, category in exports:
                checkpoint()
                if members is not None:
                    counts[group] += len(members)
                file_counts[group] += 1
                destination = run_dir / output_relative
                input_base = original_source if original_source is not None and group in ("examine", "unclassified") else source_base
                file_entry = file_manifest.get(relative, {})
                expected_hash = file_entry.get("source_sha256") if input_base == original_source else file_entry.get("output_sha256", file_entry.get("source_sha256"))
                whole_file = positions is None or len(members) == len(positions)
                if whole_file:
                    addresses = [(address, address) for address in members or []]
                else:
                    document = documents.get(input_base)
                    if document is None:
                        document = read_document(input_base / relative, spec.get("encoding", "utf-8-sig"), "json")
                        documents[input_base] = document
                        if document.sha256 != expected_hash:
                            raise DataError(f"분리 저장 중 파일 내용이 달라졌습니다: {relative}")
                    subset, addresses = _single(document, spec, members[0]) if group == "examine" else _subset(document, spec, set(members))
                    if len(addresses) != len(members):
                        raise DataError(f"분리 저장할 레코드 개수가 맞지 않습니다: {relative}")
                entry = {"group": group, "category": category, "source_file": relative.as_posix(),
                         "path": output_relative.as_posix(), "records": len(members) if members is not None else None}
                if not preview:
                    if whole_file:
                        digest = _copy_with_hash(input_base / relative, destination, checkpoint)
                        if digest != expected_hash:
                            raise DataError(f"분리 복사본 해시가 일치하지 않습니다: {relative}")
                    else:
                        write_document(destination, subset)
                        with destination.open("rb") as handle:
                            digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    entry["sha256"] = digest
                outputs.append(entry)
                for old, new in addresses:
                    index_file.write(json.dumps({"group": group, "category": category, "source_file": relative.as_posix(),
                        "source_record_path": f"doc:{old[0]}{old[1]}", "partition_file": output_relative.as_posix(),
                        "partition_record_path": f"doc:{new[0]}{new[1]}", "preview": preview}, ensure_ascii=False) + "\n")
            if number % 100 == 0 or number == len(selected_files):
                progress("partition", f"변경/미변경/검토 분리 {number:,}/{len(selected_files):,}", number, len(selected_files))
    if split_output and sum(counts.values()) != summary["source_records"]:
        raise DataError("분리 저장한 레코드 수와 원본 레코드 수가 일치하지 않습니다.")
    summary["partition_status"] = "planned" if preview else "completed"
    summary["examine_records"] = counts["examine"]
