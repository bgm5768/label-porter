"""Date/sequence result folders and one reusable, exclusively locked preview."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .formats import DataError, atomic_text

KST = timezone(timedelta(hours=9))
PREVIEW_NAME = "미리보기"
MARKER_NAME = ".label_porter_preview"
MARKER_CONTENT = "LabelPorter preview v1\n"


def now():
    return datetime.now(KST)


def _safe_path(path: Path, base: Path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise DataError(f"저장 위치에 링크/정션을 사용할 수 없습니다: {path}")
    if not path.resolve().is_relative_to(base.resolve()):
        raise DataError(f"저장 폴더 밖의 경로에 쓸 수 없습니다: {path}")


@contextmanager
def _preview_lock(directory):
    """OS locks are released on exit/crash; the small lock file is reusable."""
    lock_path = directory / ".lock"
    _safe_path(lock_path, directory)
    with lock_path.open("a+b") as handle:
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DataError("같은 저장 폴더에서 미리보기가 실행 중입니다. 완료 후 다시 실행하세요.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def output_session(output: Path, preview: bool, source: Path, results: list[Path]):
    output.mkdir(parents=True, exist_ok=True)
    if not preview:
        date_dir = output / now().strftime("%Y-%m-%d")
        _safe_path(date_dir, output)
        date_dir.mkdir(exist_ok=True)
        numbers = [int(path.name) for path in date_dir.iterdir() if path.name.isascii() and path.name.isdigit()]
        sequence = max(numbers, default=0) + 1
        while True:
            directory = date_dir / f"{sequence:03d}"
            try:
                directory.mkdir(exist_ok=False)
                break
            except FileExistsError:
                sequence += 1
        yield directory
        return

    directory = output / PREVIEW_NAME
    _safe_path(directory, output)
    if source.is_relative_to(directory.resolve()) or any(path.is_relative_to(directory.resolve()) for path in results):
        raise DataError("미리보기 폴더 내부의 파일을 입력으로 사용할 수 없습니다. 다른 저장 폴더를 선택하세요.")
    directory.mkdir(exist_ok=True)
    with _preview_lock(directory):
        # Existing user folders are never adopted or cleared implicitly.
        marker = directory / MARKER_NAME
        _safe_path(marker, directory)
        if marker.exists():
            if marker.read_text(encoding="utf-8") != MARKER_CONTENT:
                raise DataError("미리보기 폴더의 관리 정보를 확인할 수 없습니다. 다른 저장 폴더를 선택하세요.")
        elif any(path.name != ".lock" for path in directory.iterdir()):
            raise DataError("기존 ‘미리보기’ 폴더에 다른 파일이 있습니다. 다른 저장 폴더를 선택하세요.")
        else:
            atomic_text(marker, MARKER_CONTENT)
        for current, directories, names in os.walk(directory, followlinks=False):
            for name in directories + names:
                path = Path(current) / name
                _safe_path(path, directory)
                if path.is_file() and path.stat().st_nlink > 1:
                    raise DataError(f"미리보기 폴더에 하드 링크를 사용할 수 없습니다: {path}")
        # Remove only this program's previous optional report, not the folder.
        partition_index = directory / "reports" / "partition_index.jsonl"
        if partition_index.is_file():
            partition_index.unlink()
        atomic_text(directory / "reports" / "report.html",
                    '<!doctype html><meta charset="utf-8"><title>검증 중</title>'
                    '<h1>미리보기 검증 중</h1><p>완료 후 이 보고서가 갱신됩니다. 중단된 경우 다시 실행하세요.</p>')
        yield directory
