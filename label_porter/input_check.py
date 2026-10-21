"""Filesystem checks on captured inputs; never read Tk variables in a worker."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os

from .input_files import folder_files, source_paths


def selection_paths(paths):
    """Normalize dialog selections without touching each selected file."""
    unique = {}
    for path in paths:
        value = os.path.abspath(os.path.expanduser(path))
        unique.setdefault(os.path.normcase(value), value)
    return list(unique.values())


@dataclass(frozen=True)
class InputRequest:
    config: dict
    source: str | tuple
    result_paths: tuple
    result_folder: str
    output: str
    split: bool


def inspect_inputs(request, checkpoint=lambda: None, *, stamp=False, exclude_result_directories=()):
    checkpoint()
    sources = source_paths(request.source, request.config['source'].get('glob', '**/*.json'), checkpoint)
    if not sources:
        raise ValueError('규칙의 파일 패턴에 맞는 원본 JSON이 없습니다.')
    results = folder_files(request.result_folder, checkpoint=checkpoint, exclude_directories=exclude_result_directories) if request.result_folder else list(request.result_paths)
    if not results:
        raise ValueError('실제 처리할 작업 결과 파일을 선택하세요.')
    output = Path(request.output).resolve()
    source = None if isinstance(request.source, tuple) else Path(request.source).resolve()
    if source and source.is_dir() and (output == source or output.is_relative_to(source)):
        raise ValueError('저장 위치는 원본 폴더 밖으로 선택하세요.')
    if output.exists() and not output.is_dir():
        raise ValueError('저장 위치가 파일입니다. 폴더를 선택하세요.')
    result_stats = []
    for value in results:
        checkpoint()
        path = Path(value).resolve()
        stat = path.stat()
        if not path.is_file():
            raise ValueError(f'작업 결과 파일을 찾을 수 없습니다: {path}')
        result_stats.append((str(path), stat.st_size, stat.st_mtime_ns))
    digest = None
    if stamp:
        # Include non-label source files too: they are copied during a real run.
        # One stat per file, instead of is_file + stat(size) + stat(mtime).
        source_stats = []
        if source and source.is_dir():
            def walk(folder):
                with os.scandir(folder) as entries:
                    for entry in entries:
                        checkpoint()
                        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                            raise ValueError(f'원본 내부의 링크/정션은 복사하지 않습니다: {entry.path}')
                        if entry.is_dir(follow_symlinks=False):
                            yield from walk(entry.path)
                        elif entry.is_file():
                            yield entry.path, entry.stat()
            entries = walk(source)
        else:
            entries = ((str(p), p.stat()) for p in sources)
        for path, stat in entries:
            checkpoint()
            source_stats.append((str(path), stat.st_size, stat.st_mtime_ns))
        payload = [request.config, request.source, sorted(source_stats), result_stats, str(output), request.split]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    checkpoint()
    return [str(p) for p in results], digest
