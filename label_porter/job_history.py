"""Small persistent job index. Register references, never copy labeling data."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path


def storage_directory():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'LabelPorter' / 'workspace'


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


class JobHistory:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else storage_directory()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'history.sqlite3'
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS hidden_jobs (id TEXT PRIMARY KEY)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def put(self, job):
        job = {**job, 'updated': job.get('updated') or _now()}
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?)',
                       (job['id'], job['kind'], job['title'], job['updated'], json.dumps(job, ensure_ascii=False)))
            db.execute('DELETE FROM hidden_jobs WHERE id=?', (job['id'],))
        return job['id']

    def get(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, query='', kind='', page=0, size=25, reports_only=False):
        where, args = [], []
        if query:
            where.append("(title LIKE ? ESCAPE '\\' OR payload LIKE ? ESCAPE '\\')")
            pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            args.extend((pattern, pattern))
        if kind: where.append('kind=?'); args.append(kind)
        if reports_only: where.append("kind != 'draft'")
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with self._db() as db:
            count = db.execute('SELECT COUNT(*) FROM jobs' + clause, args).fetchone()[0]
            page = min(max(0, page), max(0, (count - 1) // size))
            rows = db.execute('SELECT payload FROM jobs' + clause + ' ORDER BY updated DESC, id LIMIT ? OFFSET ?', [*args, size, page * size]).fetchall()
        return [json.loads(row[0]) for row in rows], count, page

    def draft(self, job_id, settings):
        previous = self.get(job_id)
        if previous and previous.get('settings') == settings: return job_id
        renamed = (previous or {}).get('renamed', False)
        return self.put({'id': job_id, 'kind': 'draft', 'title': previous['title'] if renamed else (settings.get('config') or {}).get('name', '진행 중 작업'),
                         'status': '설정 자동 저장', 'settings': settings, 'renamed': renamed})

    def register_run(self, directory, summary=None, settings=None, *, restore=False):
        directory = Path(directory).resolve()
        job_id = 'run-' + hashlib.sha256(os.path.normcase(str(directory)).encode()).hexdigest()
        if not restore and self._hidden(job_id): return None
        summary = summary or json.loads((directory / 'reports/summary.json').read_text(encoding='utf-8-sig'))
        previous = self.get(job_id)
        if settings is None:
            settings = (previous or {}).get('settings') or self._recover_settings(directory, summary)
        return self.put({'id': job_id, 'kind': 'preview' if summary.get('preview') else 'run',
                         'title': (previous or {}).get('title') or summary.get('rule', directory.name),
                         'status': summary.get('status', 'unknown'), 'directory': str(directory),
                         'report': str(directory / 'reports/summary.json'), 'summary': summary,
                         'settings': settings, 'updated': summary.get('finished_at') or summary.get('started_at') or _now()})

    @staticmethod
    def _recover_settings(directory, summary):
        def read(name):
            try: return json.loads((directory/name).read_text(encoding='utf-8-sig'))
            except (OSError, ValueError): return {}
        manifest = read('manifest.json')
        config = read('rule.json') or read('profile.json')
        results = [v if isinstance(v, str) else v.get('path', v.get('file', '')) for v in manifest.get('result_files', [])]
        return {'config': config, 'source': summary.get('source_directory', ''),
                'source_files': list(manifest.get('source_inputs', {}).values()) if manifest.get('source_mode') == 'files' else [],
                'results': [p for p in results if p], 'result_folder': '', 'output_mode': 'default',
                'split_output': summary.get('split_output', True), 'rule_file': str(directory/'rule.json')}

    def register_merge(self, output, result, inputs, title):
        output = Path(output).resolve()
        return self.put({'id': 'merge-' + hashlib.sha256(os.path.normcase(str(output)).encode()).hexdigest(),
                         'kind': 'merge', 'title': title or '통합 보완 결과서', 'status': 'completed',
                         'directory': str(output.parent), 'report': str(output), 'inputs': list(map(str, inputs)), 'summary': result})

    def rename(self, job_id, title):
        job = self.get(job_id)
        if not job or not title.strip(): raise ValueError('작업 이름을 입력하세요.')
        job['title'] = title.strip(); job['renamed'] = True; self.put(job)

    def _hidden(self, job_id):
        with self._db() as db:
            return db.execute('SELECT 1 FROM hidden_jobs WHERE id=?', (job_id,)).fetchone() is not None

    def delete(self, job_id):
        """Forget a job without touching any input or output file."""
        with self._db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None: return None
            db.execute('INSERT OR IGNORE INTO hidden_jobs VALUES (?)', (job_id,))
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        return json.loads(row[0])

    def managed_target(self, job):
        """Return only a result directory created under the program-owned workspace."""
        if job.get('kind') not in ('preview', 'run', 'merge') or not job.get('directory'):
            return None
        folder = Path(job['directory']).absolute()
        root = self.directory.absolute()
        if os.path.normcase(str(root.resolve())) != os.path.normcase(str(root)):
            return None
        try: parts = folder.relative_to(root).parts
        except ValueError: return None
        if job['kind'] == 'preview':
            valid = len(parts) == 4 and parts[:2] == ('results', '검증') and re.fullmatch(r'[0-9a-f]{32}', parts[2]) and parts[3] == '미리보기'
            target = folder
        elif job['kind'] == 'run':
            valid = len(parts) == 3 and parts[0] == 'results' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[1]) and re.fullmatch(r'\d+', parts[2])
            target = folder
        else:
            valid = len(parts) == 2 and parts[0] == 'merged' and re.fullmatch(r'[0-9a-f]{32}', parts[1])
            target = folder
        if not valid or not target.is_dir(): return None
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()): return None
        if os.path.normcase(str(target.resolve())) != os.path.normcase(str(target)): return None
        report = Path(job.get('report', '')).absolute()
        expected = target/'reports'/'summary.json' if job['kind'] != 'merge' else None
        if job['kind'] == 'merge':
            if not report.is_file() or report.parent != target or report.suffix.lower() != '.xlsx': return None
        elif report != expected or not report.is_file(): return None
        return target

    def delete_managed_files(self, job_id, checkpoint=lambda: None):
        job = self.get(job_id)
        if job is None: raise ValueError('보관함에서 작업을 찾을 수 없습니다.')
        target = self.managed_target(job)
        if target is None: raise ValueError('프로그램 보관함 내부의 결과만 파일까지 삭제할 수 있습니다.')
        for current, folders, files in os.walk(target, followlinks=False):
            checkpoint()
            for name in (*folders, *files):
                path = Path(current, name)
                if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                    raise ValueError('결과 폴더에 외부 연결이 있어 파일 삭제를 중단했습니다.')
        checkpoint()
        if self.managed_target(job) != target: raise ValueError('결과 폴더 위치가 변경되었습니다.')
        shutil.rmtree(target)
        self.delete(job_id)
        return target

    def merge_destination(self):
        return self.directory / 'merged' / uuid.uuid4().hex / '통합_보완결과서.xlsx'

    def discover(self, roots, checkpoint=lambda: None, *, restore=False):
        """Scan only known output roots, pruning generated data and all links."""
        found, errors = 0, []
        seen = set()
        skip = {'data', 'changed', 'unchanged', 'examine', '.git', '.venv', 'node_modules', '__pycache__'}
        for root in roots:
            root = Path(root)
            if not root.is_dir(): continue
            for current, folders, files in os.walk(root, followlinks=False):
                checkpoint()
                folders[:] = [n for n in folders if n not in skip and not Path(current, n).is_symlink() and not (hasattr(Path(current, n), 'is_junction') and Path(current, n).is_junction())]
                if Path(current).name == 'reports' and 'summary.json' in files:
                    folder = Path(current).parent.resolve()
                    if folder in seen: continue
                    seen.add(folder)
                    try:
                        if self.register_run(folder, restore=restore) is not None: found += 1
                    except (OSError, ValueError, TypeError, KeyError) as exc: errors.append(f'{folder}: {exc}')
                    folders[:] = []
        return found, errors


def export_job(job, target, *, excel=False, checkpoint=lambda: None):
    """Only explicit Save As creates a physical copy, atomically and off the UI thread."""
    target = Path(target).resolve()
    directory = Path(job['directory']).resolve()
    source = Path(job['report']) if job['kind'] == 'merge' else directory/'reports/report.xlsx'
    if not directory.is_dir(): raise FileNotFoundError('결과 폴더가 없습니다. 이동된 위치를 다시 등록해 주세요.')
    if excel and (target == source.resolve() or target.suffix.lower() != '.xlsx'):
        raise ValueError('기존 결과서와 다른 .xlsx 이름을 선택하세요.')
    if not excel and (target == directory or target.is_relative_to(directory)):
        raise ValueError('결과 폴더 밖에 ZIP을 저장하세요.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='porter-export-') as tmp:
        staged = Path(tmp)/'export'
        if excel:
            with source.open('rb') as src, staged.open('wb') as out:
                while block := src.read(1024*1024): checkpoint(); out.write(block)
        else:
            with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for current, folders, files in os.walk(directory, followlinks=False):
                    checkpoint()
                    folders[:] = [n for n in folders if not Path(current,n).is_symlink() and not (hasattr(Path(current,n),'is_junction') and Path(current,n).is_junction())]
                    for name in files:
                        path = Path(current)/name
                        if path.is_symlink(): continue
                        with path.open('rb') as src, archive.open(path.relative_to(directory).as_posix(), 'w', force_zip64=True) as out:
                            while block := src.read(1024*1024): checkpoint(); out.write(block)
        checkpoint(); os.replace(staged, target)
    return str(target)
