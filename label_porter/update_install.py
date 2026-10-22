"""Separate-process Windows updater with validation and transactional rollback."""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .update_service import FILES, UpdateError, digest, state_directory, validate_manifest, version_tuple, CREATE_NO_WINDOW


def validate_payload(stage, manifest):
    validate_manifest(manifest, manifest.get('version'))
    for name, expected in manifest['files'].items():
        path = stage / 'payload' / name
        if path.is_symlink() or path.stat().st_size != expected['size'] or digest(path) != expected['sha256']:
            raise UpdateError('설치 대기 파일이 변경되었습니다. 다시 다운로드하세요.')


def replace_files(stage, target, manifest, old_files):
    """Only the fixed program/doc filenames are touched; data/settings stay put."""
    stage, target = Path(stage), Path(target)
    validate_payload(stage, manifest)
    for name in FILES:
        old = target / name
        if old.is_symlink() or old.is_dir(): raise UpdateError('프로그램 경로에 링크 또는 폴더가 있습니다.')
        actual = digest(old) if old.exists() else None
        if actual != old_files.get(name): raise UpdateError('기존 프로그램이 변경되어 설치를 중단했습니다.')
    backup = stage / 'backup'
    backup.mkdir(exist_ok=False)
    replaced = []
    try:
        for name in FILES:
            old, payload = target / name, stage / 'payload' / name
            if old.exists(): shutil.copy2(old, backup / name)
            # Copy beside the destination so replace stays atomic across volumes.
            temporary = target / ('.labelporter-' + stage.name + '-' + name)
            shutil.copy2(payload, temporary)
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        os.replace(temporary, old)
                        break
                    except PermissionError:
                        if time.monotonic() >= deadline: raise
                        time.sleep(.25)
            finally:
                temporary.unlink(missing_ok=True)
            replaced.append(name)
    except Exception:
        for name in reversed(replaced):
            old, saved = target / name, backup / name
            if saved.exists(): shutil.copy2(saved, old)
            else: old.unlink(missing_ok=True)
        raise
    return backup


def _parent_handle(pid, executable):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle: raise UpdateError('실행 중인 프로그램을 확인할 수 없습니다.')
    buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) or Path(buffer.value).resolve() != executable:
        kernel.CloseHandle(handle)
        raise UpdateError('업데이트 요청 프로그램이 일치하지 않습니다.')
    return kernel, handle


def helper_main(plan_path):
    stage = Path(plan_path).resolve().parent
    if os.name != 'nt' or stage.parent != state_directory().resolve() or not stage.name.startswith('install-'):
        return 1
    target, ready = None, False
    try:
        if Path(sys.executable).resolve() != stage / 'UpdateHelper.exe':
            raise UpdateError('업데이트 도우미 실행 위치가 다릅니다.')
        plan = json.loads((stage / 'install.json').read_text(encoding='utf-8'))
        executable = Path(plan['target']).resolve()
        if executable.name != 'LabelPorter.exe' or executable.parent == stage or executable.is_symlink():
            raise UpdateError('잘못된 설치 대상입니다.')
        target = executable.parent
        manifest = json.loads((stage / 'manifest.json').read_text(encoding='utf-8'))
        if version_tuple(manifest.get('version')) <= version_tuple(__version__):
            raise UpdateError('현재보다 높은 버전만 설치할 수 있습니다.')
        validate_payload(stage, manifest)
        kernel, handle = _parent_handle(plan['pid'], executable)
        try:
            (stage / 'ready.json').write_text('{"ready":true}', encoding='utf-8')
            ready = True
            if kernel.WaitForSingleObject(handle, 120000) != 0:
                raise UpdateError('프로그램 종료를 기다리다 설치를 중단했습니다.')
        finally:
            kernel.CloseHandle(handle)
        if (stage / 'abort').exists(): raise UpdateError('설치 준비가 취소되어 기존 프로그램을 유지합니다.')
        replace_files(stage, target, manifest, plan['old_files'])
        # Do not use stale _MEIPASS from the updater's frozen parent.
        environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        try:
            subprocess.Popen([str(executable)], cwd=str(target), env=environment)
        except OSError:
            for name in FILES:
                old = stage / 'backup' / name
                if old.exists(): shutil.copy2(old, target / name)
                elif plan['old_files'].get(name) is None: (target / name).unlink(missing_ok=True)
            raise
        result = {'ok': True, 'version': manifest['version'], 'backup': str(stage / 'backup')}
    except Exception as exc:
        result = {'ok': False, 'error': str(exc), 'backup': str(stage / 'backup')}
        # Parent may still be alive on preflight/timeout failure: never start a duplicate.
        if ready and target and (stage / 'backup').exists():
            try:
                subprocess.Popen([str(target / 'LabelPorter.exe')], cwd=str(target),
                    env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
            except OSError: pass
    (stage / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    (state_directory() / 'last-install.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0 if result['ok'] else 1


def start_helper(stage):
    if not getattr(sys, 'frozen', False) or os.name != 'nt':
        raise UpdateError('자동 설치는 Windows 배포 실행 파일에서 사용할 수 있습니다.')
    stage = Path(stage).resolve()
    if stage.parent != state_directory().resolve(): raise UpdateError('잘못된 설치 대기 경로입니다.')
    target = Path(sys.executable).resolve()
    if target.name != 'LabelPorter.exe': raise UpdateError('실행 파일 이름을 LabelPorter.exe로 유지하세요.')
    if (stage / 'install.json').exists(): raise UpdateError('이미 설치를 요청한 패키지입니다. 다시 다운로드하세요.')
    manifest = json.loads((stage / 'manifest.json').read_text(encoding='utf-8'))
    if version_tuple(manifest.get('version')) <= version_tuple(__version__):
        raise UpdateError('현재보다 높은 버전만 설치할 수 있습니다.')
    validate_payload(stage, manifest)
    old_files = {name: digest(target.parent / name) if (target.parent / name).exists() else None for name in FILES}
    # Preflight write access before asking the running app to close.
    with tempfile_probe(target.parent): pass
    shutil.copy2(target, stage / 'UpdateHelper.exe')
    plan = stage / 'install.json'
    plan.write_text(json.dumps({'target': str(target), 'pid': os.getpid(), 'old_files': old_files}), encoding='utf-8')
    process = subprocess.Popen([str(stage / 'UpdateHelper.exe'), '--apply-update', str(plan)],
        cwd=str(stage), creationflags=CREATE_NO_WINDOW, env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if (stage / 'ready.json').exists(): return
        if process.poll() is not None:
            result = stage / 'result.json'
            raise UpdateError(json.loads(result.read_text(encoding='utf-8'))['error'] if result.exists() else '업데이트 도우미를 시작할 수 없습니다.')
        time.sleep(.1)
    (stage / 'abort').write_text('preflight timeout', encoding='utf-8')
    raise UpdateError('업데이트 준비 시간이 초과되었습니다. 기존 프로그램을 유지합니다. 다시 다운로드하세요.')


def tempfile_probe(directory):
    import tempfile
    return tempfile.TemporaryFile(dir=directory)
