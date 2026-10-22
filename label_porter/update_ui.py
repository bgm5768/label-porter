"""Non-blocking update controls; workers never touch Tk or project data."""
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext

from . import __version__, widgets as ttk
from .ui_lifecycle import UiEventQueue, release_variables
from .update_service import GitHub, check_release, download_release, get_token, save_token, state_directory
from .update_install import start_helper


class UpdatePanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=18)
        self.app, self.closed, self.busy = app, False, False
        self.installing = False
        self.events, self.cancel = UiEventQueue(), threading.Event()
        self.release, self.stage = None, None
        self.message = tk.StringVar(value='정식 GitHub 릴리스만 확인합니다. 개발 중인 변경과 커밋은 자동 배포되지 않습니다.')
        self.automatic = tk.BooleanVar(value=True)
        try:
            self.automatic.set(json.loads((state_directory() / 'preferences.json').read_text(encoding='utf-8')).get('automatic', True))
        except (OSError, ValueError, TypeError): pass
        ttk.Label(self, text=f'현재 버전  {__version__}', style='CardTitle.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Checkbutton(self, text='프로그램 시작 시 새 버전 자동 확인', variable=self.automatic, command=self._preferences).pack(anchor='w')
        ttk.Label(self, text='새 버전은 알림으로 표시합니다. 다운로드 후 “설치하고 다시 시작”을 눌러 적용하세요.\n입력 위치와 현재 규칙은 유지하며, 프리뷰·검증 세션은 재시작 후 다시 불러옵니다.', wraplength=800).pack(anchor='w', pady=12)
        self.status_label = ttk.Label(self, textvariable=self.message, wraplength=800)
        self.status_label.pack(fill='x', pady=10)
        self.progress = ttk.Progressbar(self, mode='determinate'); self.progress.pack(fill='x', pady=(0, 12))
        row = ttk.Frame(self); row.pack(fill='x')
        self.check_button = ttk.Button(row, text='업데이트 확인', command=self.check); self.check_button.pack(side='left')
        self.download_button = ttk.Button(row, text='새 버전 다운로드', command=self.download, state='disabled')
        self.download_button.pack(side='left', padx=8)
        self.install_button = ttk.Button(row, text='설치하고 다시 시작', command=self.install, state='disabled', style='Primary.TButton')
        self.install_button.pack(side='left')
        ttk.Label(self, text='릴리스 변경 내용', style='CardTitle.TLabel').pack(anchor='w', pady=(20, 8))
        self.notes = scrolledtext.ScrolledText(self, height=7, wrap='word', state='disabled')
        self.notes.pack(fill='both', expand=True)
        ttk.Label(self, text='비공개 저장소 연결', style='CardTitle.TLabel').pack(anchor='w', pady=(18, 6))
        ttk.Label(self, text='이 PC의 Git 인증을 사용할 수 있습니다. 다른 PC는 bgm5768/label-porter 저장소의\nContents 읽기 권한 토큰을 입력하세요. 토큰은 Windows 사용자별 암호화로 저장됩니다.', wraplength=800).pack(anchor='w')
        row = ttk.Frame(self); row.pack(fill='x', pady=8)
        self.token = tk.StringVar()
        ttk.Entry(row, textvariable=self.token, show='*').pack(side='left', fill='x', expand=True)
        self.token_button = ttk.Button(row, text='인증 저장', command=self._save_token); self.token_button.pack(side='left', padx=8)
        self.bind('<Configure>', self._resize, add='+')
        self.bind('<Destroy>', self._destroyed, add='+')
        self.job = self.after(100, self._poll)
        self.startup_job = None
        try:
            last = json.loads((state_directory() / 'last-install.json').read_text(encoding='utf-8'))
            if not last.get('ok'): self.message.set('이전 업데이트를 완료하지 못했습니다. ' + last.get('error', '') + '\n백업: ' + last.get('backup', ''))
        except (OSError, ValueError): pass

    def _resize(self, event):
        for child in self.winfo_children():
            if isinstance(child, ttk.Label): child.configure(wraplength=max(240, event.width-36))

    def startup(self):
        def check():
            self.startup_job = None
            self.check(silent=True)
        self.startup_job = self.after(1500, check)

    def _preferences(self):
        try:
            state_directory().mkdir(parents=True, exist_ok=True)
            (state_directory() / 'preferences.json').write_text(json.dumps({'automatic': self.automatic.get()}), encoding='utf-8')
        except OSError: self.message.set('업데이트 설정을 저장하지 못했습니다.')

    def _save_token(self):
        try:
            save_token(self.token.get()); self.token.set('')
            self.message.set('인증 정보를 보호 저장했습니다. 업데이트를 확인해 주세요.')
        except Exception as exc: self.message.set(str(exc))

    def _work(self, function):
        if self.busy or self.closed: return
        self.busy = True
        for button in (self.check_button, self.download_button, self.install_button, self.token_button): button.configure(state='disabled')
        events = self.events
        def worker():
            try: events.put(('done', function()))
            except Exception as exc: events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def check(self, silent=False):
        if self.busy or (silent and not self.automatic.get()): return
        self.message.set('GitHub 정식 릴리스를 확인하고 있습니다…')
        self.release, self.stage = None, None
        self._work(lambda: ('checked', check_release(GitHub(get_token()), __version__)))

    def download(self):
        if not self.release or self.busy: return
        release, cancel, events = self.release, self.cancel, self.events
        self.message.set('업데이트 파일을 다운로드하고 검증합니다…')
        self._work(lambda: ('downloaded', download_release(GitHub(get_token()), release, cancel,
            lambda n, total: events.put(('progress', (n, total))))))

    def install(self):
        if self.busy or not self.stage: return
        if self.app.running or (self.app.builder and self.app.builder.busy):
            self.message.set('데이터 처리가 끝난 뒤 업데이트를 설치해 주세요.'); return
        if self.app.builder:
            self.message.set('작성 중인 규칙을 완료하고 작업 화면으로 돌아온 뒤 설치해 주세요.'); return
        if not getattr(sys, 'frozen', False) or os.name != 'nt':
            self.message.set('자동 설치는 Windows 배포 실행 파일에서 사용할 수 있습니다.'); return
        if self.app._save_settings() is False:
            self.message.set('현재 작업 설정을 저장하지 못해 설치를 중단했습니다. 저장 공간과 권한을 확인하세요.')
            return
        self.installing = True
        self.app.running = True; self.app._sync_actions()
        self.message.set('안전하게 교체할 준비를 하고 있습니다. 준비가 끝나면 재시작합니다…')
        stage = self.stage
        self._work(lambda: ('installing', start_helper(stage)))

    def _poll(self):
        if self.closed: return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    n, total = value; self.progress.configure(value=100*n/total)
                    continue
                self.busy = False
                if kind == 'error':
                    self.message.set('업데이트를 완료하지 못했습니다. ' + value)
                    if self.installing:
                        self.installing = False
                        self.app.running = False; self.app._sync_actions()
                elif value[0] == 'checked':
                    self.release = value[1]
                    self.message.set(f'새 버전 {self.release["version"]}을 사용할 수 있습니다.' if self.release else '현재 버전보다 새로운 정식 업데이트가 없습니다.')
                    self.app.update_button.configure(text='새 업데이트 있음' if self.release else '프로그램 업데이트')
                    self.notes.configure(state='normal'); self.notes.delete('1.0', 'end')
                    self.notes.insert('end', self.release['notes'] if self.release else 'GitHub에 게시된 정식 릴리스가 업데이트 기준입니다.')
                    self.notes.configure(state='disabled')
                elif value[0] == 'downloaded':
                    self.stage = value[1]; self.progress.configure(value=100)
                    self.message.set('다운로드와 파일 검증이 끝났습니다. 설치하면 프로그램을 다시 시작합니다.')
                elif value[0] == 'installing':
                    self.app.running = False
                    self.app._close()
                    return
                self.check_button.configure(state='normal'); self.token_button.configure(state='normal')
                self.download_button.configure(state='normal' if self.release else 'disabled')
                self.install_button.configure(state='normal' if self.stage else 'disabled')
        except queue.Empty: pass
        self.job = self.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is not self: return
        self.closed = True; self.cancel.set(); self.events.close()
        if self.job: self.after_cancel(self.job)
        if self.startup_job: self.after_cancel(self.startup_job)
        self.release = self.stage = self.app = None
        release_variables(self)
