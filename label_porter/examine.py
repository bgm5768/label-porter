"""Exclusive follow-up categories and collision-safe export paths."""
import hashlib
from pathlib import Path

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


def result_path(category, file, number=None):
    identity = hashlib.sha256(str(file).encode("utf-8")).hexdigest()[:16]
    base = Path("examine") / category
    if number is None:
        return base / "files" / "results" / identity / Path(file).name
    return base / "results" / identity / f"{int(number):08d}.json"
