"""One rule execution, a disk-backed preview, and publication of that exact result."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from .engine import Cancelled, RunResult, run
from .formats import DataError, atomic_text
from .input_check import inspect_inputs
from .reports import STATUS
from .result_dashboard import write_dashboard
from .excel_report import write_excel_report
from .storage import now, output_session


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _digest(path, checkpoint):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


class PreviewWriter:
    """Only metadata stays in memory; JSON and precomputed field values live on disk."""
    def __init__(self, path, checkpoint):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE pairs (id INTEGER PRIMARY KEY, payload TEXT, original TEXT, incoming TEXT)')
        self.db.execute('CREATE TABLE connections (id INTEGER PRIMARY KEY, payload TEXT)')
        self.catalog = []
        self.checkpoint = checkpoint

    def stash_connections(self, rows, count):
        # A merged input may contain every record. Do not retain all its display rows
        # in engine.pending while waiting for that one output file to be written.
        cursor = self.db.execute('INSERT INTO connections(payload) VALUES (?)', (_json([rows, count]),))
        return cursor.lastrowid

    def record(self, detail, connections):
        self.checkpoint()
        if isinstance(connections, int):
            stored = self.db.execute('SELECT payload FROM connections WHERE id=?', (connections,)).fetchone()
            self.db.execute('DELETE FROM connections WHERE id=?', (connections,))
            connections = json.loads(stored[0])
        original = detail.get('source_record')
        incoming = detail.get('platform_record')
        if incoming is None and detail['scope'] == 'result':
            incoming = next((v['row'] for v in detail.get('platform_candidates', [])
                             if v['file'] == detail['result_file'] and v['number'] == detail['result_row']), None)
        status = detail['status']
        state = ('KEY 중복' if status in ('duplicate_result', 'duplicate_source_key') else
                 '파일 오류' if detail['scope'] == 'source_file' or (detail['scope'] == 'result' and not detail['result_row']) else
                 'KEY 해석 불가' if status in ('invalid_source', 'invalid_result') else
                 '원본만 있음' if status == 'no_result' else '보완만 있음' if status == 'orphan_result' else '연결됨')
        source_location = (detail['source_file'] + ' · ' + detail['record_path']) if detail['source_file'] else ''
        result_location = (Path(detail['result_file']).name + ' · ' + str(detail['result_row'])) if detail['result_file'] else ''
        key = str(detail['key'])
        rows, count = connections or ([], 0)
        message = f"변경 {detail['changed_fields']:,}곳 · 저장 준비 완료" if status == 'updated' else STATUS.get(status, status)
        if detail.get('issues'): message += f" · 검토 사유 {len(detail['issues']):,}개"
        pair = {'key': detail['key'], 'source': original, 'result': incoming,
                'candidate': detail.get('candidate_record'), 'output': detail.get('final_record', original),
                'source_location': source_location, 'result_location': result_location,
                'status': status, 'message': message, 'rows': rows, 'total_connections': count,
                'issues': detail.get('issues', []), 'reason': detail['reason']}
        index = len(self.catalog)
        self.db.execute('INSERT INTO pairs VALUES (?, ?, ?, ?)',
                        (index, _json(pair), _json(original).casefold() if original is not None else '',
                         _json(incoming).casefold() if incoming is not None else ''))
        self.catalog.append({'key': key, 'state': state, 'source_location': source_location,
                             'result_location': result_location, 'status': status,
                             'search': (key.casefold(), (source_location + ' ' + result_location).casefold(),
                                        '', '', (state + ' ' + message + ' ' + detail['reason']).casefold())})
        if index % 256 == 0: self.db.commit()

    __call__ = record

    def close(self):
        if self.db is not None:
            try: self.db.commit()
            finally:
                self.db.close()
                self.db = None


class PreparedSession:
    def __init__(self, request):
        self.request = request
        self.temp = tempfile.TemporaryDirectory(prefix='label-porter-session-', ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'preview.sqlite'
        self.lock = threading.RLock()
        self.closed = False
        self.ready = False
        self.catalog = []
        self.search_index = True  # Content was indexed during processing, not at first UI search.
        self.originals = range(0)  # UI needs the count, not another copy of every input object.
        self.result_total = self.invalid_result_keys = 0
        self.fingerprint = None
        self.owned_outputs = []

    @classmethod
    def prepare(cls, request, progress, cancel):
        session = cls(request)
        def checkpoint():
            if cancel.is_set(): raise Cancelled('전체 처리를 중단했습니다. 다시 처리한 뒤 저장하세요.')
        writer = None
        try:
            results, session.fingerprint = inspect_inputs(request, checkpoint, stamp=True)
            writer = PreviewWriter(session.db_path, checkpoint)
            result = run(request.source, results, session.root / 'output', request.config,
                         progress=progress, cancel=cancel, split_output=request.split, record_sink=writer)
            writer.close(); session.catalog = writer.catalog; writer = None
            checkpoint()
            if not result.summary['status'].startswith('completed'):
                raise DataError(result.summary.get('error', '전체 처리를 완료하지 못했습니다.'))
            _, current = inspect_inputs(request, checkpoint, stamp=True)
            if current != session.fingerprint:
                raise DataError('처리 도중 입력이 변경되었습니다. 데이터를 새로 읽어 주세요.')
            session.result = result
            session.originals = range(result.summary['source_records'])
            session.result_total = result.summary['result_records']
            session.invalid_result_keys = sum(e['state'] == 'KEY 해석 불가' and not e['source_location'] for e in session.catalog)
            session.manifest = json.loads((result.directory / 'manifest.json').read_text(encoding='utf-8'))
            session.input_hashes = {session.manifest['source_inputs'][e['path']]: e['source_sha256']
                                    for e in session.manifest['files']}
            for entry in session.manifest['result_files']:
                session.input_hashes[entry['path']] = entry.get('sha256') or _digest(entry['path'], checkpoint)
            # Seal staged artifacts, including reports, so external edits cannot silently be published.
            session.artifacts = {}
            progress({'phase': 'seal', 'message': '처리 결과와 검색 목록을 세션에 보관하고 있습니다.', 'done': 0, 'total': 0})
            for path in result.directory.rglob('*'):
                checkpoint()
                if path.is_file():
                    session.artifacts[path.relative_to(result.directory)] = _digest(path, checkpoint)
                    if len(session.artifacts) % 250 == 0:
                        progress({'phase': 'seal', 'message': f'임시 결과 확인 · {len(session.artifacts):,}개 파일 보관', 'done': 0, 'total': 0})
            session.ready = True
            return session
        except BaseException:
            try:
                if writer is not None: writer.close()
            finally: session.close()
            raise

    def prepared_pair(self, index):
        with self.lock:
            if self.closed: raise DataError('종료된 처리 세션입니다.')
            db = sqlite3.connect(self.db_path)
            try:
                value = db.execute('SELECT payload FROM pairs WHERE id=?', (index,)).fetchone()
                if value is None: raise DataError('레코드를 찾을 수 없습니다.')
                pair = json.loads(value[0])
                pair.update(source_total=len(self.originals), result_total=self.result_total,
                            invalid_result_keys=self.invalid_result_keys)
                return pair
            finally: db.close()

    def matches(self, request):
        # Moving the output folder does not change any field value or partition.
        return replace(request, output=self.request.output) == self.request

    def prepared_search(self, query, scope, state, checkpoint):
        terms = query.casefold().split()
        found = []
        with self.lock:
            if self.closed: raise DataError('종료된 처리 세션입니다.')
            db = sqlite3.connect(self.db_path)
            try:
                content = bool(terms) and scope in ('전체', '원본 JSON', '보완 JSON')
                rows = db.execute('SELECT id, original, incoming FROM pairs ORDER BY id') if content else (
                    (i, '', '') for i in range(len(self.catalog)))
                for index, original, incoming in rows:
                    checkpoint()
                    entry = self.catalog[index]
                    if state != '전체 상태' and entry['state'] != state: continue
                    fields = {'KEY': (entry['search'][0],), '파일명 / 위치': (entry['search'][1],),
                              '원본 JSON': (original,), '보완 JSON': (incoming,)}.get(
                                  scope, (*entry['search'], original, incoming))
                    if all(any(term in field for field in fields) for term in terms): found.append(index)
                return found
            finally: db.close()

    def publish(self, request, progress, cancel):
        """Copy sealed artifacts. No JSON decoding of inputs or rule evaluation here."""
        def checkpoint():
            if cancel.is_set(): raise Cancelled('복사본 저장을 중단했습니다. 부분 결과를 사용하지 마세요.')
        with self.lock:
            if self.closed or not self.ready: raise DataError('3단계 전체 처리를 먼저 완료하세요.')
            if not self.matches(request): raise DataError('규칙 또는 입력 설정이 바뀌었습니다. 3단계에서 다시 처리하세요.')
            try:
                inspect_inputs(request, checkpoint, exclude_result_directories=self.owned_outputs)
                _, current = inspect_inputs(self.request, checkpoint, stamp=True, exclude_result_directories=self.owned_outputs)
                if current != self.fingerprint:
                    self.ready = False
                    raise DataError('입력 파일이 바뀌었습니다. 3단계에서 데이터를 새로 읽어 주세요.')
                progress({'phase': 'check', 'message': '저장 전 입력 파일의 내용 변경 여부를 확인하고 있습니다.', 'done': 0, 'total': 0})
                for i, (path, digest) in enumerate(self.input_hashes.items(), 1):
                    checkpoint()
                    if _digest(path, checkpoint) != digest:
                        self.ready = False
                        raise DataError(f'입력 내용이 바뀌었습니다. 3단계에서 다시 처리하세요: {path}')
                    if i % 100 == 0:
                        progress({'phase': 'check', 'message': f'입력 변경 확인 {i:,}/{len(self.input_hashes):,}', 'done': i, 'total': len(self.input_hashes)})
            except (OSError, ValueError):
                self.ready = False
                raise
            with output_session(Path(request.output).resolve(), False, Path(self.manifest['source_directory']), []) as destination:
                # Only folders newly allocated by this session are excluded. A result
                # folder may contain the chosen output location; our own copies are
                # not new input files. Other additions must still invalidate the run.
                self.owned_outputs.append(destination)
                summary = {**self.result.summary, 'run_directory': str(destination), 'prepared': False,
                           'prepared_at': self.result.summary['finished_at'], 'status': 'saving'}
                def status(): atomic_text(destination / 'run_status.json', json.dumps(summary, ensure_ascii=False, indent=2))
                status()
                try:
                    def copy_artifact(relative, expected):
                        checkpoint()
                        target = destination / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        with (self.result.directory / relative).open('rb') as src, target.open('xb') as dst:
                            while chunk := src.read(1024 * 1024):
                                checkpoint(); digest.update(chunk); dst.write(chunk)
                        if digest.hexdigest() != expected:
                            self.ready = False
                            raise DataError(f'준비된 결과가 변경되었습니다. 3단계에서 다시 처리하세요: {relative}')
                        shutil.copystat(self.result.directory / relative, target)
                    artifacts = [(relative, expected) for relative, expected in self.artifacts.items()
                                 if relative.as_posix() not in ('run_status.json', 'reports/report.html', 'reports/summary.json')]
                    completed, next_update = 0, 100
                    pending = set()
                    def collect():
                        nonlocal pending, completed, next_update
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in finished:
                            future.result()
                            completed += 1
                        if completed >= next_update or completed == len(artifacts):
                            progress({'phase': 'publish', 'message': f'준비된 결과 복사 {completed:,}/{len(artifacts):,}',
                                      'done': completed, 'total': len(artifacts)})
                            next_update = completed + 100
                    # Bound disk concurrency and outstanding references. Publication
                    # is complete only after every worker has closed and verified its file.
                    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='publish-copy') as pool:
                        for relative, expected in artifacts:
                            checkpoint()
                            pending.add(pool.submit(copy_artifact, relative, expected))
                            if len(pending) >= 16: collect()
                        while pending:
                            checkpoint()
                            collect()
                    # Preserve empty input directories as well as their files.
                    for folder in (self.result.directory / 'data').rglob('*'):
                        checkpoint()
                        if folder.is_dir(): (destination / 'data' / folder.relative_to(self.result.directory / 'data')).mkdir(parents=True, exist_ok=True)
                    _, current = inspect_inputs(self.request, checkpoint, stamp=True, exclude_result_directories=self.owned_outputs)
                    if current != self.fingerprint:
                        self.ready = False
                        raise DataError('저장 도중 입력이 변경되었습니다. 3단계에서 다시 처리하세요.')
                    checkpoint()
                    summary.update(status=self.result.summary['status'], finished_at=now().isoformat(timespec='seconds'))
                    catalog = json.loads((destination / 'reports/catalog.json').read_text(encoding='utf-8'))
                    write_excel_report(destination / 'reports', summary, catalog)
                    write_dashboard(destination / 'reports', summary, catalog, [])
                    atomic_text(destination / 'reports/summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
                    checkpoint()
                except Exception as exc:
                    summary.update(status='cancelled' if isinstance(exc, Cancelled) else 'failed', error=str(exc))
                    atomic_text(destination / 'reports/report.html', '<!doctype html><meta charset="utf-8"><h1>저장 미완료</h1><p>부분 결과입니다. 프로그램에서 사유를 확인하고 다시 저장하세요.</p>')
                    atomic_text(destination / 'reports/summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
                    manifest = {**self.manifest, 'status': summary['status']}
                    atomic_text(destination / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                status()
                return RunResult(destination, summary)

    def close(self):
        # Deleting thousands of staged files must not block Tk's event loop.
        self.ready = False
        def cleanup():
            with self.lock:
                self.closed = True
                self.temp.cleanup()
        threading.Thread(target=cleanup, daemon=True, name='session-cleanup').start()
