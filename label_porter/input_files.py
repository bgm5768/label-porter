"""File selections shared by runs, previews, and the read-only file browser."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .formats import DataError

JSON_SUFFIXES = {'.json', '.jsonl', '.ndjson'}
RESULT_SUFFIXES = JSON_SUFFIXES | {'.csv'}


def folder_files(folder, suffixes=RESULT_SUFFIXES, checkpoint=None, exclude_directories=()):
    folder = Path(folder).expanduser().resolve()
    excluded = {Path(p).resolve() for p in exclude_directories}
    if not folder.is_dir():
        raise DataError(f"폴더를 찾을 수 없습니다: {folder}")
    found = []
    for current, directories, names in os.walk(folder, followlinks=False):
        if checkpoint: checkpoint()
        directories[:] = sorted(d for d in directories if (not excluded or (Path(current) / d).resolve() not in excluded) and not (Path(current) / d).is_symlink()
                                and not (hasattr(Path, 'is_junction') and (Path(current) / d).is_junction()))
        for name in sorted(names):
            path = Path(current) / name
            if path.suffix.lower() in suffixes and path.is_file() and not path.is_symlink():
                found.append(path)
    return sorted(found)


def source_paths(source, pattern="**/*.json", checkpoint=None):
    if isinstance(source, (list, tuple)):
        paths = [Path(p).expanduser().resolve() for p in source]
        if len(set(paths)) != len(paths):
            raise DataError("같은 원본 파일이 두 번 선택되었습니다.")
        for path in paths:
            if checkpoint: checkpoint()
            if not path.is_file(): raise DataError(f"원본 파일을 찾을 수 없습니다: {path}")
        return sorted(paths)
    source = Path(source).expanduser().resolve()
    directory = source.is_dir()
    if not directory and not source.is_file():
        raise DataError(f"원본 입력을 찾을 수 없습니다: {source}")
    paths, parents = [], {}
    for path in source.glob(pattern) if directory else [source]:
        if checkpoint: checkpoint()
        if not path.is_file(): continue
        if directory:
            if path.parent not in parents:
                parents[path.parent] = path.parent.resolve().is_relative_to(source)
            if path.is_symlink() or not parents[path.parent]:
                raise DataError("원본 폴더 밖의 파일을 사용할 수 없습니다.")
        paths.append(path)
    return sorted(paths)


@dataclass
class SourceLayout:
    root: Path | None
    files: dict[Path, Path]  # output-relative path -> actual selected input
    directory: bool

    @property
    def description(self):
        return str(self.root) if self.root else "여러 드라이브의 선택 파일"


def source_layout(source, pattern="**/*.json", root="", checkpoint=None):
    paths = source_paths(source, pattern, checkpoint)
    directory = not isinstance(source, (list, tuple)) and Path(source).is_dir()
    if root:
        base = Path(root).expanduser().resolve()
        if not base.is_dir() or any(not p.is_relative_to(base) for p in paths):
            raise DataError("원본 기준 폴더 안의 JSON 파일을 선택하세요.")
    elif directory:
        base = Path(source).expanduser().resolve()
    else:
        try: base = Path(os.path.commonpath([str(p.parent) for p in paths])) if paths else None
        except ValueError: base = None  # Different Windows drives / shares.
    mapped = {}
    for path in paths:
        if base is not None:
            relative = path.relative_to(base)
        else:
            drive = path.drive.rstrip(':').replace('\\', '_').replace('/', '_')
            prefix = ('drive_' + drive) if drive else ('root_' + hashlib.sha256(path.anchor.encode()).hexdigest()[:8])
            relative = Path(prefix) / path.relative_to(path.anchor)
        if relative in mapped: raise DataError(f"저장할 상대 경로가 중복됩니다: {relative}")
        mapped[relative] = path
    return SourceLayout(base, mapped, directory)
