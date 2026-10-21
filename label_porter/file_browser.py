"""Paged, searchable local file list. Enumerating folders never parses JSON."""
import queue
import threading
from pathlib import Path
from tkinter import StringVar
from . import widgets as ttk

from .input_files import folder_files
from .ui_lifecycle import UiEventQueue, remove_traces, release_variables


class FileBrowser(ttk.Frame):
    PAGE = 100

    def __init__(self, parent, on_select):
        super().__init__(parent)
        self.configure(width=330)
        self.pack_propagate(False)
        self.on_select = on_select
        self.paths, self.filtered = [], []
        self.page, self.generation = 1, 0
        self.closed = False
        self.cancel = threading.Event()
        self.events = UiEventQueue()
        self.query = StringVar()
        self.info = StringVar(value='폴더 또는 파일을 선택하세요.')
        self.page_info = StringVar(value='0 / 0')
        self.search_job = None
        ttk.Label(self, text='파일 목록', style='Heading.TLabel').pack(anchor='w', pady=(0, 8))
        ttk.Label(self, text='파일명 · 경로 검색').pack(anchor='w')
        self.search = ttk.Entry(self, textvariable=self.query); self.search.pack(fill='x', pady=(4, 8))
        ttk.Label(self, textvariable=self.info, wraplength=310).pack(fill='x', pady=(0, 6))
        nav = ttk.Frame(self); nav.pack(side='bottom', fill='x', pady=(8, 0))
        self.previous = ttk.Button(nav, text='이전', width=6, command=lambda: self.move(-1)); self.previous.pack(side='left')
        ttk.Label(nav, textvariable=self.page_info, anchor='center').pack(side='left', fill='x', expand=True)
        self.next = ttk.Button(nav, text='다음', width=6, command=lambda: self.move(1)); self.next.pack(side='right')
        area = ttk.Frame(self); area.pack(fill='both', expand=True)
        self.table = ttk.Treeview(area, columns=('name', 'folder'), show='headings', selectmode='browse')
        self.table.heading('name', text='파일명'); self.table.column('name', width=220, minwidth=100)
        self.table.heading('folder', text='폴더'); self.table.column('folder', width=160, minwidth=80)
        scroll = ttk.Scrollbar(area, command=self.table.yview); scroll.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scroll.set); self.table.pack(fill='both', expand=True)
        area.bind('<Configure>', lambda e: (self.table.column('name', width=max(100, int((e.width-18)*.58))),
                                           self.table.column('folder', width=max(80, int((e.width-18)*.42)))))
        self.table.bind('<<TreeviewSelect>>', self.select)
        self.traces = [(self.query, self.query.trace_add('write', self.schedule_filter))]
        self.bind('<Destroy>', self.destroyed)
        self.job = self.after(80, self.poll)

    def set_inputs(self, targets, initial=None, append=False):
        if self.closed: return
        self.query.set('')
        self.cancel.set(); self.cancel = stop = threading.Event()
        self.generation += 1
        generation = self.generation
        existing = list(self.paths) if append else []
        events = self.events
        self.info.set('파일 목록을 읽고 있습니다…')
        def checkpoint():
            if stop.is_set(): raise InterruptedError()
        def work():
            try:
                found = list(existing)
                for target in targets:
                    checkpoint()
                    path = Path(target).expanduser().resolve()
                    if path.is_dir(): found.extend(folder_files(path, checkpoint=checkpoint))
                    elif path.is_file(): found.append(path)
                    else: raise ValueError(f'파일 또는 폴더를 찾을 수 없습니다: {path}')
                checkpoint()
                events.put((generation, 'files', (sorted(set(found)), initial)))
            except InterruptedError: pass
            except Exception as exc: events.put((generation, 'error', str(exc)))
        threading.Thread(target=work, daemon=True, name='file-browser').start()

    def poll(self):
        if self.closed: return
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation: continue
                if kind == 'error': self.info.set(value); continue
                self.paths, initial = value
                self.filter()
                if initial:
                    path = Path(initial).resolve()
                    if path in self.paths:
                        index = self.paths.index(path)
                        if index in self.filtered:
                            self.page = self.filtered.index(index) // self.PAGE + 1; self.render()
                            self.table.selection_set(str(index)); self.table.see(str(index))
                            continue
                if self.filtered: self.table.selection_set(str(self.filtered[0]))
        except queue.Empty: pass
        self.job = self.after(80, self.poll)

    def schedule_filter(self, *_):
        if self.closed: return
        if self.search_job: self.after_cancel(self.search_job)
        self.search_job = self.after(120, self.filter)

    def filter(self):
        if self.search_job: self.after_cancel(self.search_job)
        self.search_job = None
        terms = self.query.get().casefold().split()
        self.filtered = [i for i, path in enumerate(self.paths) if all(term in str(path).casefold() for term in terms)]
        self.page = 1; self.render()

    def render(self):
        self.table.delete(*self.table.get_children())
        pages = max(1, (len(self.filtered) + self.PAGE - 1) // self.PAGE)
        self.page = max(1, min(pages, self.page))
        for i in self.filtered[(self.page-1)*self.PAGE:self.page*self.PAGE]:
            path = self.paths[i]
            self.table.insert('', 'end', iid=str(i), values=(path.name, str(path.parent)))
        self.info.set(f'전체 {len(self.paths):,}개 · 검색 {len(self.filtered):,}개 · 페이지당 {self.PAGE}개')
        self.page_info.set(f'{self.page if self.filtered else 0} / {pages if self.filtered else 0}')
        self.previous.configure(state='normal' if self.page > 1 else 'disabled')
        self.next.configure(state='normal' if self.page < pages else 'disabled')

    def move(self, delta):
        self.page += delta; self.render()

    def select(self, _event=None):
        if self.closed: return
        selected = self.table.selection()
        if selected: self.on_select(self.paths[int(selected[0])])

    def destroyed(self, event):
        if event.widget != self: return
        self.close()

    def close(self):
        if self.closed: return
        self.closed = True; self.cancel.set(); self.generation += 1
        remove_traces(self.traces)
        self.events.close()
        for name in ('job', 'search_job'):
            job = getattr(self, name)
            if job is not None: self.after_cancel(job)
            setattr(self, name, None)
        self.on_select = None
        self.paths.clear(); self.filtered.clear()
        release_variables(self)
