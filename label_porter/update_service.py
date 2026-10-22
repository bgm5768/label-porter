"""Authenticated, bounded GitHub Release downloads. No dataset access."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY = 'bgm5768/label-porter'
API = 'https://api.github.com/repos/' + REPOSITORY
MANIFEST_NAME = 'LabelPorter-update.json'
FILES = ('LabelPorter.exe', '사용설명.md', 'AI_규칙작성_가이드.md', '후처리_지원범위.md',
         '아키텍처_및_규칙엔진.md', 'THIRD_PARTY_NOTICES.txt')
MAX_PACKAGE = 512 * 1024 * 1024
MAX_JSON = 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class UpdateError(ValueError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise UpdateError('버전은 2.26.0 형태여야 합니다.')
    return tuple(map(int, value.removeprefix('v').split('.')))


def state_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'LabelPorter' / 'updates'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _protect(raw, decrypt=False):
    """DPAPI is bound to the current Windows user; never store plaintext tokens."""
    if os.name != 'nt':
        raise UpdateError('인증 정보 보호 저장은 Windows에서 지원됩니다.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, output = Blob(len(raw), buffer), Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise UpdateError('Windows 인증 정보 보호 저장/읽기에 실패했습니다.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


def save_token(token):
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise UpdateError('공백이 없는 GitHub 접근 토큰을 입력하세요.')
    path = state_directory() / 'github.dpapi'
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _protect(token.encode('utf-8'))
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)


def get_token():
    token = os.environ.get('LABEL_PORTER_GITHUB_TOKEN', '').strip()
    if token:
        return token
    path = state_directory() / 'github.dpapi'
    if path.exists():
        return _protect(path.read_bytes(), decrypt=True).decode('utf-8')
    # Reuse Git Credential Manager on developer PCs without triggering login UI.
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    try:
        result = subprocess.run(['git', '-c', 'credential.interactive=false', 'credential', 'fill'],
            input='protocol=https\nhost=github.com\n\n', text=True, capture_output=True,
            timeout=8, env=env, creationflags=CREATE_NO_WINDOW)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode == 0 and values.get('password'):
            return values['password']
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise UpdateError('비공개 저장소 인증이 필요합니다. 이 저장소의 Contents 읽기 권한 토큰을 등록하세요.')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if url.scheme != 'https' or url.hostname not in ('api.github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com', 'github.com') or url.username or url.password or url.port not in (None, 443):
            raise UpdateError('허용되지 않은 업데이트 다운로드 주소입니다.')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and url.hostname != 'api.github.com':
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, *, method='GET', data=None, binary=False):
        if not path.startswith(API + '/') and path != API:
            raise UpdateError('잘못된 저장소 API 주소입니다.')
        headers = {'Authorization': 'Bearer ' + self.token, 'User-Agent': 'LabelPorter-Updater',
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if data is not None:
            data = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            return self.opener.open(urllib.request.Request(path, data=data, headers=headers, method=method), timeout=30)
        except urllib.error.HTTPError as exc:
            # Keep HTTP status for release-not-found handling. Never expose headers/tokens.
            raise exc
        except (OSError, urllib.error.URLError) as exc:
            raise UpdateError('GitHub에 연결할 수 없습니다. 네트워크를 확인하고 다시 시도하세요.') from exc

    def json(self, path, **kwargs):
        with self.request(path, **kwargs) as response:
            data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise UpdateError('업데이트 안내 파일이 너무 큽니다.')
        return json.loads(data)

    def asset(self, asset_id, destination, limit, cancel=None, progress=lambda *_: None):
        if type(asset_id) is not int or asset_id <= 0:
            raise UpdateError('잘못된 릴리스 파일 ID입니다.')
        with self.request(f'{API}/releases/assets/{asset_id}', binary=True) as response, Path(destination).open('wb') as out:
            total = 0
            while block := response.read(256 * 1024):
                if cancel and cancel.is_set(): raise UpdateError('업데이트 다운로드가 중단되었습니다.')
                total += len(block)
                if total > limit: raise UpdateError('업데이트 파일 크기가 안내와 다릅니다.')
                out.write(block); progress(total, limit)


def validate_manifest(manifest, version):
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('repository') != REPOSITORY:
        raise UpdateError('업데이트 명세의 형식 또는 저장소가 다릅니다.')
    if manifest.get('version') != version or version_tuple(version) != version_tuple(manifest['version']):
        raise UpdateError('릴리스와 파일 버전이 다릅니다.')
    if manifest.get('platform') != 'windows-x64' or manifest.get('asset') != f'LabelPorter-v{version}-windows-x64.zip':
        raise UpdateError('이 프로그램에서 지원하지 않는 업데이트 패키지입니다.')
    if type(manifest.get('size')) is not int or not 0 < manifest['size'] <= MAX_PACKAGE:
        raise UpdateError('업데이트 패키지 크기가 유효하지 않습니다.')
    files = manifest.get('files')
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise UpdateError('업데이트 파일 구성이 유효하지 않습니다.')
    for item in (manifest, *files.values()):
        if not isinstance(item, dict) or not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256', ''))):
            raise UpdateError('업데이트 체크섬이 유효하지 않습니다.')
        if type(item.get('size')) is not int or not 0 < item['size'] <= MAX_PACKAGE:
            raise UpdateError('업데이트 파일 크기가 유효하지 않습니다.')
    return manifest


def check_release(client, current):
    try:
        client.json(API)  # A private repo 404 is an access failure, not "no updates".
        try:
            release = client.json(API + '/releases/latest')
        except urllib.error.HTTPError as exc:
            if exc.code == 404: return None
            raise
        if release.get('draft') or release.get('prerelease'):
            return None
        version = release['tag_name'].removeprefix('v')
        if version_tuple(version) <= version_tuple(current): return None
        assets = {asset['name']: asset for asset in release.get('assets', [])}
        if MANIFEST_NAME not in assets:
            raise UpdateError('새 릴리스에 자동 업데이트 파일이 없습니다. 배포 담당자에게 확인하세요.')
        with tempfile.TemporaryDirectory(prefix='label_porter_manifest_') as temp:
            path = Path(temp) / 'manifest.json'
            client.asset(assets[MANIFEST_NAME]['id'], path, MAX_JSON)
            manifest = validate_manifest(json.loads(path.read_text(encoding='utf-8')), version)
        asset = assets.get(manifest['asset'])
        if not asset or asset.get('size') != manifest['size']:
            raise UpdateError('릴리스 패키지가 없거나 크기가 일치하지 않습니다.')
        if asset.get('digest') and asset['digest'] != 'sha256:' + manifest['sha256']:
            raise UpdateError('GitHub 파일 체크섬이 업데이트 명세와 다릅니다.')
        return {'version': version, 'notes': (release.get('body') or '')[:16000],
                'manifest': manifest, 'asset_id': asset['id']}
    except urllib.error.HTTPError as exc:
        raise UpdateError(f'GitHub 접근 실패 ({exc.code}). 저장소 권한·토큰 만료·요청 한도를 확인하세요.') from exc


def verify_package(package, manifest, destination):
    validate_manifest(manifest, manifest.get('version'))
    package, destination = Path(package), Path(destination)
    if package.stat().st_size != manifest['size'] or digest(package) != manifest['sha256']:
        raise UpdateError('다운로드 파일 검증에 실패했습니다. 기존 프로그램은 변경하지 않았습니다.')
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        if len(infos) != len(FILES) or {i.filename for i in infos} != set(FILES):
            raise UpdateError('압축 파일에 허용되지 않은 경로나 중복 파일이 있습니다.')
        destination.mkdir(parents=True, exist_ok=False)
        for info in infos:
            expected = manifest['files'][info.filename]
            if info.is_dir() or info.file_size != expected['size'] or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError('압축 파일의 구성/크기가 다릅니다.')
            target = destination / info.filename
            with archive.open(info) as source, target.open('xb') as out:
                while block := source.read(256 * 1024): out.write(block)
            if digest(target) != expected['sha256']:
                raise UpdateError('압축 해제한 업데이트 파일 검증에 실패했습니다.')
    return destination


def download_release(client, release, cancel, progress):
    state_directory().mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='install-', dir=state_directory()))
    manifest = release['manifest']
    package = stage / 'package.zip'
    client.asset(release['asset_id'], package, manifest['size'], cancel, progress)
    if cancel.is_set(): raise UpdateError('업데이트가 중단되었습니다.')
    verify_package(package, manifest, stage / 'payload')
    (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return stage
