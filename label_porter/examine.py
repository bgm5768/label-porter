"""Exclusive follow-up categories and collision-safe export paths."""
import hashlib
import csv
import io
from pathlib import Path

from .formats import DataError, atomic_text, read_document, write_document
from .partitions import _single, _copy_with_hash


CATEGORIES = {
    "unmatched": "짝이 없는 데이터",
    "review": "검토 / 오류",
    "excluded": "조건 제외",
    "file_errors": "파일 단위 오류",
}


def category_for(scope, status, result_row="", issues=False):
    if scope == "source_file" or (scope == "result" and not result_row):
        return "file_errors"
    if status in ("no_result", "orphan_result"):
        return "unmatched"
    if status == "excluded":
        return "excluded"
    if status in ("review_required", "duplicate_source_key", "duplicate_result",
                  "invalid_result", "invalid_source") or issues:
        return "review"
    return ""


def result_path(category, file, number=None, suffix=".json"):
    identity = hashlib.sha256(str(file).encode("utf-8")).hexdigest()[:16]
    base = Path("examine") / category
    if number is None:
        return base / "files" / "results" / identity / Path(file).name
    return base / "results" / identity / f"{int(number):08d}{suffix}"


def label_path(category, file, ordinal):
    suffix = Path(file).suffix.lower()
    return Path("examine") / category / "labels" / file / f"{ordinal:08d}{suffix if suffix in ('.jsonl', '.ndjson') else '.json'}"


class ResultArchive:
    """Keep addresses, not duplicate JSON trees; read each requested file once."""
    def __init__(self, spec):
        self.spec = spec
        self.files = {}
        self.requests = {}

    def register(self, path, document, rows):
        fmt = self.spec.get("format", "auto")
        csv_input = fmt == "csv" or (fmt == "auto" and path.suffix.lower() == ".csv")
        suffix = ".csv" if csv_input else path.suffix.lower() if document.jsonl else ".json"
        self.files[str(path)] = {"sha256": document.sha256, "addresses": [(d, p) for d, p, _ in rows],
                                 "csv": csv_input, "suffix": suffix}

    def request(self, category, file, number):
        info = self.files.get(file)
        if info is None or not number:
            return None
        number = int(number)
        if not 1 <= number <= len(info["addresses"]):
            raise DataError("검토할 보완 레코드의 위치가 유효하지 않습니다.")
        target = result_path(category, file, number, info["suffix"])
        self.requests.setdefault(file, {})[(category, number)] = target
        return target.as_posix()

    def export(self, directory, preview, checkpoint, progress):
        if preview:
            return
        for count, (file, requests) in enumerate(self.requests.items(), 1):
            checkpoint()
            info = self.files[file]
            progress("partition", f"검토용 보완 데이터 읽기 {count:,}/{len(self.requests):,} · {Path(file).name}")
            # CSV cells must retain their input text, even when rules parse JSON columns.
            document = read_document(Path(file), self.spec.get("encoding", "utf-8-sig"),
                                     "csv" if info["csv"] else "json", checkpoint=checkpoint)
            if document.sha256 != info["sha256"]:
                raise DataError(f"검토 저장 중 보완 파일이 변경되었습니다: {file}")
            for saved, ((_, number), target) in enumerate(requests.items(), 1):
                checkpoint()
                destination = directory / target
                if len(info["addresses"]) == 1:
                    if _copy_with_hash(Path(file), destination, checkpoint) != info["sha256"]:
                        raise DataError(f"검토 복사본 해시가 일치하지 않습니다: {file}")
                elif info["csv"]:
                    address = info["addresses"][number - 1]
                    row = document.docs[0][int(address[1].split("/")[1])]
                    buffer = io.StringIO(newline="")
                    writer = csv.DictWriter(buffer, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerow(row)
                    encoding = document.encoding
                    if encoding.lower().replace("_", "-") in ("utf-8", "utf-8-sig", "utf8"):
                        encoding = "utf-8-sig" if document.bom else "utf-8"
                    atomic_text(destination, buffer.getvalue(), encoding)
                else:
                    subset, _ = _single(document, self.spec, info["addresses"][number - 1])
                    write_document(destination, subset)
                if saved % 100 == 0:
                    progress("partition", f"검토용 보완 데이터 저장 · {saved:,}/{len(requests):,}건", saved, len(requests))
            del document
            progress("partition", f"검토용 보완 데이터 저장 {count:,}/{len(self.requests):,}", count, len(self.requests))
