import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock

from label_porter.update_service import (API, FILES, MANIFEST_NAME, REPOSITORY, SafeRedirect,
    UpdateError, check_release, digest, validate_manifest, verify_package, version_tuple, _protect)
from label_porter.update_install import replace_files, helper_main, _parent_handle
from scripts.release import VERSION_FILES, current_version, make_package, prepare, publish, build


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        # Model the old installed executable independently of this source release.
        old_version = patch('label_porter.update_install.__version__', '2.25.4')
        old_version.start(); self.addCleanup(old_version.stop)
        self.base = Path(self.temp.name)
        self.package_dir = self.base / 'release'; self.package_dir.mkdir()
        for name in FILES: (self.package_dir / name).write_bytes(('new:' + name).encode())
        self.manifest = make_package(self.package_dir, '2.26.0', 'a' * 40)
        self.package = self.package_dir / self.manifest['asset']

    def stage(self):
        stage = self.base / 'install-test'; stage.mkdir()
        verify_package(self.package, self.manifest, stage / 'payload')
        return stage

    def test_semver_is_numeric_and_rejects_channels(self):
        self.assertGreater(version_tuple('2.10.0'), version_tuple('2.9.99'))
        for value in ('2.3', '2.3.0-beta', '../2.3.4', '02.3.4', None):
            with self.assertRaises(UpdateError): version_tuple(value)

    def test_private_api_release_assets_and_package(self):
        manifest = self.manifest
        release = {'tag_name': 'v2.26.0', 'draft': False, 'prerelease': False, 'body': 'release notes',
            'assets': [{'name': MANIFEST_NAME, 'id': 1}, {'name': manifest['asset'], 'id': 2,
                'size': manifest['size'], 'digest': 'sha256:' + manifest['sha256']}]}
        class Client:
            def json(self, url): return {'private': True} if url == API else release
            def asset(self, asset_id, target, limit):
                self_outer.assertEqual(asset_id, 1)
                Path(target).write_text(json.dumps(manifest), encoding='utf-8')
        self_outer = self
        result = check_release(Client(), '2.25.4')
        self.assertEqual(result['version'], '2.26.0')
        self.assertEqual(result['asset_id'], 2)
        stage = self.stage()
        self.assertEqual((stage/'payload/LabelPorter.exe').read_bytes(), b'new:LabelPorter.exe')
        self.assertIsNone(check_release(Client(), '2.26.0'))
        release['draft'] = True
        self.assertIsNone(check_release(Client(), '2.25.4'))

    def test_no_release_is_distinct_from_no_private_access(self):
        class Client:
            def __init__(self, deny): self.deny = deny
            def json(self, url):
                if url != API or self.deny: raise urllib.error.HTTPError(url, 404, 'not found', None, None)
                return {'private': True}
        self.assertIsNone(check_release(Client(False), '2.25.4'))
        with self.assertRaisesRegex(UpdateError, '404'): check_release(Client(True), '2.25.4')

    def test_redirect_does_not_leak_authorization(self):
        request = urllib.request.Request(API + '/releases/assets/1', headers={'Authorization': 'Bearer test-secret'})
        result = SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/file')
        self.assertFalse(result.has_header('Authorization'))
        for url in ('http://api.github.com/path', 'https://evil.example/file', 'https://api.github.com.evil.example/file', 'https://api.github.com:444/file'):
            with self.assertRaises(UpdateError): SafeRedirect().redirect_request(request, None, 302, '', {}, url)

    def test_manifest_checksum_and_zip_paths_are_validated(self):
        wrong = copy.deepcopy(self.manifest); wrong['repository'] = 'other/repo'
        with self.assertRaises(UpdateError): validate_manifest(wrong, '2.26.0')
        with self.package.open('ab') as output: output.write(b'bad')
        with self.assertRaises(UpdateError): verify_package(self.package, self.manifest, self.base/'out')
        with zipfile.ZipFile(self.package, 'w') as archive: archive.writestr('../outside.exe', 'bad')
        wrong = copy.deepcopy(self.manifest)
        wrong.update(size=self.package.stat().st_size, sha256=digest(self.package))
        with self.assertRaises(UpdateError): verify_package(self.package, wrong, self.base/'out')
        self.assertFalse((self.base/'outside.exe').exists())

    def test_install_preserves_data_and_backup(self):
        stage = self.stage(); target = self.base/'program'; target.mkdir()
        for name in FILES: (target/name).write_bytes(b'old')
        (target/'results').mkdir(); (target/'results/data.json').write_text('original data')
        (target/'settings.json').write_text('settings')
        old = {name: digest(target/name) for name in FILES}
        backup = replace_files(stage, target, self.manifest, old)
        self.assertEqual((target/'LabelPorter.exe').read_bytes(), b'new:LabelPorter.exe')
        self.assertEqual((backup/'LabelPorter.exe').read_bytes(), b'old')
        self.assertEqual((target/'results/data.json').read_text(), 'original data')
        self.assertEqual((target/'settings.json').read_text(), 'settings')

    def test_partial_copy_rolls_back_and_changed_input_is_refused(self):
        stage = self.stage(); target = self.base/'program'; target.mkdir()
        for name in FILES: (target/name).write_bytes(b'old')
        old = {name: digest(target/name) for name in FILES}
        replace = os.replace
        def fail(source, destination):
            if Path(destination).name == FILES[1]: raise OSError('simulated disk failure')
            return replace(source, destination)
        with patch('label_porter.update_install.os.replace', side_effect=fail):
            with self.assertRaises(OSError): replace_files(stage, target, self.manifest, old)
        self.assertTrue(all((target/name).read_bytes() == b'old' for name in FILES))
        (target/'LabelPorter.exe').write_bytes(b'user changed executable')
        with self.assertRaises(UpdateError): replace_files(stage, target, self.manifest, old)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_windows_token_encryption(self):
        raw = b'fake-token-for-test-only'
        encrypted = _protect(raw)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(_protect(encrypted, decrypt=True), raw)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handle')
    def test_real_windows_process_identity_and_wait_handle(self):
        kernel, handle = _parent_handle(os.getpid(), Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        try: self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
        finally: kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == 'nt', 'Windows helper')
    def test_helper_waits_for_parent_then_installs_and_restarts(self):
        stage = self.stage(); target = self.base/'program'; target.mkdir()
        for name in FILES: (target/name).write_bytes(b'old')
        old = {name: digest(target/name) for name in FILES}
        (stage/'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        (stage/'install.json').write_text(json.dumps({'target': str(target/'LabelPorter.exe'), 'pid':123,'old_files':old}), encoding='utf-8')
        kernel = Mock()
        def wait(*_):
            self.assertTrue((stage/'ready.json').exists())
            self.assertEqual((target/'LabelPorter.exe').read_bytes(), b'old')
            return 0
        kernel.WaitForSingleObject.side_effect=wait
        with patch('label_porter.update_install.state_directory', return_value=self.base), patch('label_porter.update_install.sys.executable',str(stage/'UpdateHelper.exe')), patch('label_porter.update_install._parent_handle',return_value=(kernel,1)), patch('label_porter.update_install.subprocess.Popen') as launch:
            self.assertEqual(helper_main(stage/'install.json'),0)
        self.assertEqual((target/'LabelPorter.exe').read_bytes(), b'new:LabelPorter.exe')
        self.assertEqual(launch.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'],'1')
        self.assertTrue(json.loads((stage/'result.json').read_text())['ok'])

    @unittest.skipUnless(os.name == 'nt', 'Windows helper')
    def test_helper_timeout_never_replaces_or_launches(self):
        stage = self.stage(); target = self.base/'program'; target.mkdir()
        for name in FILES: (target/name).write_bytes(b'old')
        (stage/'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        (stage/'install.json').write_text(json.dumps({'target': str(target/'LabelPorter.exe'), 'pid':123,'old_files':{name:digest(target/name) for name in FILES}}), encoding='utf-8')
        kernel=Mock(); kernel.WaitForSingleObject.return_value=258
        with patch('label_porter.update_install.state_directory', return_value=self.base), patch('label_porter.update_install.sys.executable',str(stage/'UpdateHelper.exe')), patch('label_porter.update_install._parent_handle',return_value=(kernel,1)), patch('label_porter.update_install.subprocess.Popen') as launch:
            self.assertEqual(helper_main(stage/'install.json'),1)
        launch.assert_not_called()
        self.assertTrue(all((target/name).read_bytes()==b'old' for name in FILES))

    def test_publishing_finishes_uploads_before_making_release_visible(self):
        root=self.base/'repo'; root.mkdir()
        (root/'CHANGELOG.md').write_text('## 2.26.0 — date\n\n- updater\n', encoding='utf-8')
        directory=root/'dist/v2.26.0'; directory.mkdir(parents=True)
        import shutil
        for path in self.package_dir.iterdir(): shutil.copy2(path,directory/path.name)
        events=[]
        client=Mock(); client.token='test-token'
        def api(url, **kwargs):
            if url==API: return {'private':True}
            if '/tags/' in url or url.endswith('/latest'): raise urllib.error.HTTPError(url,404,'missing',None,None)
            events.append((kwargs['method'], kwargs['data']))
            return {'id':123,'assets':[]}
        client.json.side_effect=api
        response=MagicMock(); client.opener.open.return_value=response
        client.opener.open.side_effect=lambda *args,**kwargs: (events.append(('upload',args[0].full_url)),response)[1]
        def git(*args,**_):
            if args[:2]==('rev-parse','HEAD'): return 'a'*40
            if args[:2]==('branch','--show-current'): return 'main'
            if args[:2]==('remote','get-url'): return f'https://github.com/{REPOSITORY}.git'
            return ''
        with patch('scripts.release.require_clean'),patch('scripts.release.current_version',return_value='2.26.0'),patch('scripts.release.git',side_effect=git),patch('scripts.release.get_token',return_value='test-token'),patch('scripts.release.GitHub',return_value=client):
            publish(root)
            self.assertEqual([event[0] for event in events],['POST','upload','upload','PATCH'])
            self.assertTrue(events[0][1]['draft']); self.assertFalse(events[-1][1]['draft'])
            events.clear()
            client.opener.open.side_effect=OSError('simulated upload failure')
            with self.assertRaises(OSError): publish(root)
            self.assertFalse(any(event[0]=='PATCH' for event in events))

    def test_version_changes_only_on_explicit_prepare_and_requires_commit(self):
        root = self.base/'repo'; root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        for name in VERSION_FILES:
            path=root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('__version__ = "2.25.4"\n' if name.endswith('__init__.py') else 'version 2.25.4\n', encoding='utf-8')
        (root/'CHANGELOG.md').write_text('# 변경 내역\n\n## 미배포\n\n- updater\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=root, check=True, capture_output=True)
        subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','initial'],cwd=root,check=True)
        self.assertEqual(current_version(root), '2.25.4')
        prepare('2.26.0', root)
        self.assertEqual(current_version(root), '2.26.0')
        self.assertIn('## 2.26.0', (root/'CHANGELOG.md').read_text(encoding='utf-8'))
        with self.assertRaises(UpdateError): prepare('2.27.0', root)
        with self.assertRaises(UpdateError): publish(root)

    @unittest.skipUnless(os.name == 'nt', 'Windows release build')
    def test_release_build_excludes_untracked_data_and_failed_build_can_retry(self):
        root=self.base/'repo'; root.mkdir()
        subprocess.run(['git','init','-q',str(root)],check=True)
        for name in VERSION_FILES:
            path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('__version__ = "3.0.0"\n' if name.endswith('__init__.py') else 'version 3.0.0\n',encoding='utf-8')
        (root/'THIRD_PARTY_NOTICES.txt').write_text('notices')
        (root/'CHANGELOG.md').write_text('# changes\n\n## 3.0.0\n\n- test\n')
        subprocess.run(['git','add','.'],cwd=root,check=True,capture_output=True)
        subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','source'],cwd=root,check=True)
        (root/'private-input.json').write_text('private data')
        real_run=subprocess.run; failure=[True]; snapshots=[]
        def simulate(command, **kwargs):
            if command[0]=='git': return real_run(command,**kwargs)
            cwd=kwargs.get('cwd')
            if cwd:
                self.assertFalse((Path(cwd)/'private-input.json').exists())
                snapshots.append(Path(cwd))
            if 'PyInstaller' in command:
                target=Path(command[command.index('--distpath')+1]);target.mkdir()
                (target/'LabelPorter.exe').write_bytes(b'fake-exe')
            if '--self-test' in command:
                if failure[0]: raise subprocess.CalledProcessError(1,command)
                Path(command[-1]).write_text('{"ok":true}')
            return subprocess.CompletedProcess(command,0)
        with patch('scripts.release.subprocess.run',side_effect=simulate):
            with self.assertRaises(subprocess.CalledProcessError): build(root)
            self.assertFalse((root/'dist/v3.0.0').exists())
            failure[0]=False
            build(root)
        self.assertEqual(len(set(snapshots)),2)
        manifest=json.loads((root/'dist/v3.0.0'/MANIFEST_NAME).read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'],'3.0.0')
        with zipfile.ZipFile(root/'dist/v3.0.0'/manifest['asset']) as archive:
            self.assertEqual(set(archive.namelist()),set(FILES))


class UpdateUiTests(unittest.TestCase):
    def test_background_check_navigation_and_install_guard(self):
        from label_porter.gui import App
        root = tk.Tk(); root.withdraw()
        with tempfile.TemporaryDirectory() as temp, patch('label_porter.update_ui.state_directory', return_value=Path(temp)), patch.object(App,'_load_settings'), patch.object(App,'_save_settings'):
            app=App(root)
            try:
                app._show_updates()
                self.assertEqual(app.page_title.get(), '프로그램 업데이트')
                app._back(); self.assertEqual(app.route, 'workflow')
                gate=threading.Event()
                with patch('label_porter.update_ui.get_token', return_value='fake'), patch('label_porter.update_ui.check_release', side_effect=lambda *_: (gate.wait(2), None)[1]):
                    app.updater.check(); self.assertTrue(app.updater.busy)
                    root.update(); gate.set()
                    deadline=time.monotonic()+3
                    while app.updater.busy and time.monotonic()<deadline: root.update(); time.sleep(.01)
                    self.assertFalse(app.updater.busy)
                app.updater.stage=Path(temp); app.running=True
                app.updater.install()
                self.assertIn('데이터 처리가 끝난 뒤', app.updater.message.get())
                app.running=False
                self.assertEqual(len(app.tabs.tabs()),4)
            finally:
                app.running=False
                app._close()


if __name__ == '__main__': unittest.main()
