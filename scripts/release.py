"""Explicit release workflow: prepare -> review/commit -> build -> publish.

Normal builds and commits never change versions or publish releases.
"""
from __future__ import annotations
import argparse
import datetime
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from label_porter.update_service import (API, FILES, MANIFEST_NAME, REPOSITORY, GitHub,
    UpdateError, digest, get_token, version_tuple, CREATE_NO_WINDOW, validate_manifest)

VERSION_FILES = ('label_porter/__init__.py', 'README.md', '사용설명.md', 'AI_규칙작성_가이드.md',
                 '후처리_지원범위.md', '아키텍처_및_규칙엔진.md')


def git(*args, root=ROOT, raw=False):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, check=True)
    return result.stdout if raw else result.stdout.decode('utf-8').strip()


def current_version(root=ROOT):
    text = (root / VERSION_FILES[0]).read_text(encoding='utf-8')
    value = re.search(r'__version__\s*=\s*[\"\']([^\"\']+)', text).group(1)
    version_tuple(value)
    return value


def require_clean(root=ROOT):
    if git('status', '--porcelain', '--untracked-files=no', root=root):
        raise UpdateError('추적 중인 변경 사항을 먼저 검토하고 커밋하세요. 릴리스 스크립트는 작업 내용을 자동 커밋하지 않습니다.')


def prepare(version, root=ROOT):
    require_clean(root)
    old = current_version(root)
    if version.startswith('v') or version_tuple(version) <= version_tuple(old):
        raise UpdateError('현재 버전보다 높은 숫자 버전을 입력하세요 (예: 2.26.0).')
    if git('tag', '--list', 'v' + version, root=root): raise UpdateError('이미 존재하는 버전 태그입니다.')
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
    updates = {}
    for name in VERSION_FILES:
        path = root / name
        text = path.read_text(encoding='utf-8')
        updates[path] = text.replace(old, version)
        if name == '아키텍처_및_규칙엔진.md':
            updates[path] = re.sub(r'(기준: \*\*v' + re.escape(version) + r' · schema_version 1 · )\d{4}-\d{2}-\d{2}', r'\g<1>' + today, updates[path])
    path = root / 'CHANGELOG.md'
    text = path.read_text(encoding='utf-8')
    if re.search(r'^## ' + re.escape(version) + r'\b', text, re.M): raise UpdateError('변경 내역에 같은 버전이 이미 있습니다.')
    heading = f'## {version} — {today}'
    updates[path] = text.replace('## 미배포', heading, 1) if '## 미배포' in text else text.replace('# 변경 내역', '# 변경 내역\n\n' + heading + '\n\n- 배포할 변경 내용을 작성하세요.', 1)
    for path, text in updates.items(): path.write_text(text, encoding='utf-8')
    print(f'{old} -> {version}: 버전과 문서를 갱신했습니다. 변경 내역 검토 후 커밋하고 build를 실행하세요.')


def release_notes(version, root=ROOT):
    text = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    match = re.search(r'^## ' + re.escape(version) + r'\b[^\n]*\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not match or not match.group(1).strip(): raise UpdateError('이번 버전의 변경 내역을 작성하세요.')
    return match.group(1).strip()


def make_package(directory, version, commit):
    package = directory / f'LabelPorter-v{version}-windows-x64.zip'
    files = {name: {'sha256': digest(directory / name), 'size': (directory / name).stat().st_size} for name in FILES}
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES: archive.write(directory / name, name)
    manifest = {'schema_version': 1, 'repository': REPOSITORY, 'version': version,
        'platform': 'windows-x64', 'asset': package.name, 'sha256': digest(package),
        'size': package.stat().st_size, 'files': files, 'commit': commit}
    validate_manifest(manifest, version)
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def build(root=ROOT):
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise UpdateError('Windows 64비트 Python 환경에서 빌드하세요.')
    require_clean(root)
    version, commit = current_version(root), git('rev-parse', 'HEAD', root=root)
    release_notes(version, root)
    output = root / 'dist' / ('v' + version)
    if output.exists(): raise UpdateError(f'기존 배포 폴더를 덮어쓰지 않습니다: {output}. 새 버전 번호를 준비하세요.')
    (root / 'build').mkdir(exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix='release-' + version + '-' + commit[:10] + '-', dir=root/'build'))
    staging_output = snapshot / '.dist'
    # Only committed sources enter the build: unrelated local datasets stay out.
    with zipfile.ZipFile(io.BytesIO(git('archive', '--format=zip', 'HEAD', root=root, raw=True))) as archive:
        archive.extractall(snapshot)
    logs = root / 'outputs' / ('release-v' + version)
    logs.mkdir(parents=True, exist_ok=True)
    print(f'1/4 커밋 소스·Mermaid 확인: {commit}', flush=True)
    subprocess.run([sys.executable, 'scripts/build_mermaid.py', '--check'], cwd=snapshot, check=True)
    print(f'2/4 전체 테스트 실행 · 로그: {logs / "tests.log"}', flush=True)
    with (logs / 'tests.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=snapshot,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    with (logs / 'build.log').open('w', encoding='utf-8') as log:
        print(f'3/4 실행 파일 빌드 · 로그: {logs / "build.log"}', flush=True)
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(staging_output),
            '--workpath', str(snapshot / '.build'), 'LabelPorter.spec'], cwd=snapshot,
            stdout=log, stderr=subprocess.STDOUT, check=True)
    import shutil
    for name in FILES[1:]: shutil.copy2(snapshot / name, staging_output / name)
    self_test = logs / 'frozen-self-test.json'
    print('4/4 실행 파일 자체 검사·업데이트 패키지 생성', flush=True)
    subprocess.run([str(staging_output / 'LabelPorter.exe'), '--self-test', str(self_test)],
                   creationflags=CREATE_NO_WINDOW, timeout=180, check=True)
    if not json.loads(self_test.read_text(encoding='utf-8')).get('ok'):
        raise UpdateError('실행 파일 자체 검사에 실패했습니다.')
    make_package(staging_output, version, commit)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Windows rename refuses an existing destination; failed builds never occupy a version.
    staging_output.rename(output)
    print(f'빌드·테스트·업데이트 패키지 준비 완료: {output}\n게시하려면 publish를 별도로 실행하세요.')


def publish(root=ROOT):
    require_clean(root)
    version, commit = current_version(root), git('rev-parse', 'HEAD', root=root)
    directory = root / 'dist' / ('v' + version)
    manifest = validate_manifest(json.loads((directory / MANIFEST_NAME).read_text(encoding='utf-8')), version)
    if manifest.get('commit') != commit: raise UpdateError('빌드 이후 커밋이 변경되었습니다. 새 버전으로 다시 준비하세요.')
    package = directory / manifest['asset']
    if package.stat().st_size != manifest['size'] or digest(package) != manifest['sha256']:
        raise UpdateError('배포 패키지가 변경되었습니다.')
    if git('branch', '--show-current', root=root) != 'main': raise UpdateError('main에서만 정식 배포할 수 있습니다.')
    remote = git('remote', 'get-url', '--push', 'origin', root=root)
    if remote not in (f'https://github.com/{REPOSITORY}.git', f'git@github.com:{REPOSITORY}.git'):
        raise UpdateError('origin 저장소가 업데이트 저장소와 다릅니다.')
    client = GitHub(get_token())
    if not client.json(API).get('private'): raise UpdateError('저장소가 비공개인지 확인하세요. 게시를 중단합니다.')
    tag = 'v' + version
    try: release = client.json(API + '/releases/tags/' + tag)
    except urllib.error.HTTPError as exc:
        if exc.code != 404: raise
        release = None
    if release and not release.get('draft'): raise UpdateError('이미 게시된 릴리스를 덮어쓰지 않습니다.')
    try:
        latest = client.json(API + '/releases/latest')
        if version_tuple(latest['tag_name']) >= version_tuple(version):
            raise UpdateError('최신 정식 릴리스보다 높은 버전만 게시할 수 있습니다.')
    except urllib.error.HTTPError as exc:
        if exc.code != 404: raise
    if git('tag', '--list', tag, root=root):
        if git('rev-list', '-n', '1', tag, root=root) != commit: raise UpdateError('기존 버전 태그의 커밋이 다릅니다.')
    else: git('tag', '-a', tag, '-m', 'Label Porter ' + tag, root=root)
    git('push', '--atomic', 'origin', 'HEAD:refs/heads/main', tag, root=root)
    if not release:
        release = client.json(API + '/releases', method='POST', data={'tag_name': tag,
            'target_commitish': commit, 'name': 'Label Porter ' + tag, 'body': release_notes(version, root), 'draft': True, 'prerelease': False})
    # A failed upload leaves an invisible draft. Retrying verifies existing assets.
    for path in (package, directory / MANIFEST_NAME):
        existing = next((a for a in release.get('assets', []) if a['name'] == path.name), None)
        if existing:
            if existing.get('digest') != 'sha256:' + digest(path):
                raise UpdateError('초안에 다른 파일이 있습니다. GitHub에서 초안을 점검하세요.')
            continue
        url = f'https://uploads.github.com/repos/{REPOSITORY}/releases/{release["id"]}/assets?name=' + urllib.parse.quote(path.name)
        request = urllib.request.Request(url, data=path.read_bytes(), method='POST', headers={
            'Authorization': 'Bearer ' + client.token, 'User-Agent': 'LabelPorter-Release',
            'Content-Type': 'application/octet-stream', 'X-GitHub-Api-Version': '2022-11-28'})
        with client.opener.open(request, timeout=120) as response: response.read()
    # Users see the release only after both uploads have succeeded.
    client.json(f'{API}/releases/{release["id"]}', method='PATCH',
                data={'draft': False, 'make_latest': 'true', 'body': release_notes(version, root)})
    print(f'정식 배포 완료: https://github.com/{REPOSITORY}/releases/tag/{tag}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='명시한 버전으로만 갱신'); p.add_argument('--version', required=True)
    sub.add_parser('build', help='커밋한 소스로 검사·빌드·패키지 생성 (게시하지 않음)')
    sub.add_parser('publish', help='main/태그 push 후 비공개 GitHub 정식 릴리스 게시')
    sub.add_parser('status', help='현재 버전과 작업 상태 확인')
    args = parser.parse_args()
    try:
        if args.command == 'prepare': prepare(args.version)
        elif args.command == 'build': build()
        elif args.command == 'publish': publish()
        else: print('현재 버전:', current_version(), '\n' + git('status', '--short'))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Captured Git output and tokens are deliberately not dumped.
        print('릴리스 중단:', str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
