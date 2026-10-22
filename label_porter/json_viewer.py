"""Read-only JSON inspector with background loading and lazy tree expansion."""
import itertools
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext
from . import widgets as ttk

from .formats import escape_token, read_document
from .file_browser import FileBrowser
from .json_search import search_json, value_at
from .ui_lifecycle import UiEventQueue, release_variables


def value_description(value):
    if isinstance(value, list):
        return f"배열 · {len(value):,}개 항목"
    if isinstance(value, dict):
        return f"객체 · {len(value):,}개 필드"
    if isinstance(value, str):
        return f"문자열 · {len(value):,}자"
    return "null" if value is None else "참/거짓" if isinstance(value, bool) else "숫자"


def bounded_preview(value, depth=0, budget=None):
    budget = [1000] if budget is None else budget
    budget[0] -= 1
    if budget[0] <= 0:
        return "… 미리보기 한도 (트리에서 펼쳐 확인)"
    if depth >= 5 and isinstance(value, (dict, list)):
        return f"… {value_description(value)} (트리에서 펼쳐 확인)"
    if isinstance(value, dict):
        result = {key: bounded_preview(item, depth + 1, budget) for key, item in itertools.islice(value.items(), 20) if budget[0] > 0}
        if len(value) > 20:
            result["… 생략"] = f"나머지 {len(value)-20:,}개 필드"
        return result
    if isinstance(value, list):
        result = [bounded_preview(item, depth + 1, budget) for item in value[:10] if budget[0] > 0]
        if len(value) > 10:
            result.append(f"… 나머지 {len(value)-10:,}개 항목")
        return result
    return value[:2000] + "…" if isinstance(value, str) and len(value) > 2000 else value


class JsonViewer:
    PAGE = 100

    def __init__(self, parent, initial=None, paths=(), *, on_close=None):
        self.on_close = on_close
        self.window = ttk.create_window(parent)
        self.window.title("JSON 뷰어 · 파일 검색과 데이터 미리보기")
        width, height = ttk.window_size(self.window, (1280, 780))
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(min(900, width), min(600, height))
        self.events = UiEventQueue()
        self.cancel = threading.Event()
        self.alive, self.loading = True, False
        self.generation = 0
        self.values, self.more = {}, {}
        self.document_value = None
        self.search_cancel = threading.Event()
        self.search_revision = 0
        self.search_hits = []
        self.search_page = 1
        self.search_base = ()
        self.content_query = tk.StringVar()
        self.content_scope = tk.StringVar(value='전체 JSON')
        self.search_info = tk.StringVar(value='키·문자열·숫자 검색 · Enter로 실행')
        self.path = tk.StringVar(value="JSON 파일을 선택하세요.")
        self.status = tk.StringVar(value="읽기 전용 · 파일과 프로젝트 규칙을 변경하지 않습니다.")
        self.selection_info = tk.StringVar(value="배열을 선택하면 정확한 항목 수를 확인할 수 있습니다.")
        toolbar = ttk.Frame(self.window, padding=12); toolbar.pack(fill="x")
        self.open_button = ttk.Button(toolbar, text="데이터 열기 ▾", command=self.open_menu)
        self.open_button.pack(side="left")
        self.input_menu = tk.Menu(self.window, tearoff=False)
        self.input_menu.add_command(label='폴더 선택…', command=self.choose_folder)
        self.input_menu.add_command(label='파일 선택… (여러 개 가능)', command=self.choose)
        ttk.Label(toolbar, text='폴더·파일을 다시 선택하면 목록과 JSON을 함께 교체합니다.').pack(side='left', padx=12)
        self.stop_button = ttk.Button(toolbar, text="읽기 중단", command=lambda: self.cancel.set(), state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(self.window, textvariable=self.path, padding=(12, 0), wraplength=920).pack(fill="x")
        ttk.Label(self.window, textvariable=self.status, padding=12, wraplength=920).pack(fill="x")
        self.progress = ttk.Progressbar(self.window); self.progress.pack(fill="x", padx=12, pady=(0, 10))
        workspace = ttk.Panedwindow(self.window, orient='horizontal'); workspace.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.browser = FileBrowser(workspace, self.load, self.reset_input); workspace.add(self.browser, weight=1)
        content = ttk.Frame(workspace); workspace.add(content, weight=3)
        search_bar = ttk.Frame(content); search_bar.pack(fill='x', pady=(0, 8))
        ttk.Label(search_bar, text='JSON 내부 검색').pack(side='left', padx=(0, 8))
        ttk.Combobox(search_bar, textvariable=self.content_scope, values=['전체 JSON', '선택 항목 내부'], state='readonly', width=15).pack(side='left')
        search_entry = ttk.Entry(search_bar, textvariable=self.content_query)
        search_entry.pack(side='left', fill='x', expand=True, padx=8)
        search_entry.bind('<Return>', lambda e: self.start_search())
        ttk.Button(search_bar, text='검색', command=self.start_search, width=6).pack(side='left')
        ttk.Button(search_bar, text='초기화', command=self.reset_search, width=7).pack(side='left', padx=(4, 0))
        ttk.Label(content, textvariable=self.search_info).pack(fill='x', pady=(0, 6))
        self.results_area = ttk.Frame(content)
        result_nav = ttk.Frame(self.results_area); result_nav.pack(side='bottom', fill='x')
        self.search_previous = ttk.Button(result_nav, text='이전', width=6, command=lambda: self.move_search(-1)); self.search_previous.pack(side='left')
        self.search_page_info = tk.StringVar()
        ttk.Label(result_nav, textvariable=self.search_page_info).pack(side='left', fill='x', expand=True)
        self.search_next = ttk.Button(result_nav, text='다음', width=6, command=lambda: self.move_search(1)); self.search_next.pack(side='right')
        self.results = ttk.Treeview(self.results_area, columns=('path', 'value'), show='headings', height=5, selectmode='browse')
        self.results.heading('path', text='검색된 JSON 경로'); self.results.heading('value', text='값 / 개수')
        self.results.column('path', width=350); self.results.column('value', width=200)
        result_scroll = ttk.Scrollbar(self.results_area, command=self.results.yview); result_scroll.pack(side='right', fill='y')
        self.results.configure(yscrollcommand=result_scroll.set); self.results.pack(fill='both', expand=True)
        self.results.bind('<<TreeviewSelect>>', self.select_search_result)
        body = ttk.Panedwindow(content, orient="horizontal"); body.pack(fill='both', expand=True)
        self.body = body
        left, right = ttk.Frame(body), ttk.Frame(body)
        left.configure(width=320); left.pack_propagate(False)
        right.configure(width=450); right.pack_propagate(False)
        body.add(left, weight=1); body.add(right, weight=1)
        workspace.bind('<Configure>', lambda e: workspace.sashpos(0, int(e.width*.28)) if self.alive and len(workspace.panes()) > 1 else None)
        body.bind('<Configure>', lambda e: body.sashpos(0, int(e.width*.45)) if self.alive and len(body.panes()) > 1 else None)
        self.tree = ttk.Treeview(left, columns=("type",), show="tree headings")
        self.tree.heading("#0", text="필드 / 배열 인덱스"); self.tree.heading("type", text="자료형 · 개수")
        self.tree.column("#0", width=180, minwidth=70); self.tree.column("type", width=130, minwidth=80)
        left.bind('<Configure>', lambda e: self.alive and self.tree.winfo_exists() and (self.tree.column('#0', width=max(70, int((e.width-18)*.55))),
                                           self.tree.column('type', width=max(80, int((e.width-18)*.45)))))
        scroll = ttk.Scrollbar(left, command=self.tree.yview); scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(fill="both", expand=True)
        ttk.Label(right, textvariable=self.selection_info, wraplength=440).pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(right, text="값 미리보기 · 큰 값은 일부만 표시합니다.", foreground=ttk.MUTED).pack(anchor="w", padx=10)
        self.preview = scrolledtext.ScrolledText(right, wrap="word", font=("Consolas", 10), state="disabled")
        self.preview.pack(fill="both", expand=True, padx=(10, 0), pady=(8, 0))
        self.tree.bind("<<TreeviewOpen>>", self.expand)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Double-1>", self.load_more)
        self.tree.bind("<Return>", self.load_more)
        self.window.bind("<Destroy>", self.destroyed)
        self.job = self.window.after(80, self.poll)
        targets = list(paths) if paths else [initial] if initial else []
        if targets: self.browser.set_inputs(targets, initial)

    def choose(self):
        paths = filedialog.askopenfilenames(parent=self.window, title="미리 볼 파일 다중 선택",
            filetypes=[("JSON / CSV", "*.json *.jsonl *.ndjson *.csv"), ("모든 파일", "*.*")])
        if paths: self.browser.set_inputs(paths, initial=paths[0])

    def open_menu(self):
        try:
            self.input_menu.tk_popup(self.open_button.winfo_rootx(), self.open_button.winfo_rooty() + self.open_button.winfo_height())
        finally:
            self.input_menu.grab_release()

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.window, title="JSON / CSV 파일이 있는 폴더")
        if path: self.browser.set_inputs([path])

    def reset_input(self):
        """Invalidate file reads and searches together before changing selection."""
        self.cancel.set(); self.generation += 1; self.loading = False
        self.clear_search()
        self.document_value = None
        self.tree.delete(*self.tree.get_children()); self.values.clear(); self.more.clear()
        self.path.set('선택된 JSON 파일이 없습니다.')
        self.status.set('왼쪽 폴더에서 파일을 선택하세요.')
        self.selection_info.set('선택된 항목이 없습니다.'); self._preview('')
        self.progress.configure(value=0); self.stop_button.configure(state='disabled')

    def clear_search(self):
        self.search_cancel.set(); self.search_revision += 1
        self.search_hits.clear(); self.search_base = (); self.search_page = 1
        self.results.delete(*self.results.get_children()); self.results_area.pack_forget()
        self.search_info.set('키·문자열·숫자 검색 · Enter로 실행')

    def reset_search(self):
        self.content_query.set(''); self.clear_search()

    def pointer_tokens(self, pointer):
        value = self.document_value
        tokens = []
        for part in pointer.split('/')[1:]:
            part = part.replace('~1', '/').replace('~0', '~')
            token = int(part) if isinstance(value, list) else part
            tokens.append(token); value = value[token]
        return tuple(tokens)

    def start_search(self):
        self.clear_search()
        if self.loading or not self.values:
            self.search_info.set('JSON 파일을 읽은 뒤 검색할 수 있습니다.'); return
        term = self.content_query.get().strip()
        if not term: return
        value = self.document_value
        if self.content_scope.get() == '선택 항목 내부':
            selection = self.tree.selection()
            if not selection or selection[0] not in self.values:
                self.search_info.set('검색할 배열이나 객체를 먼저 선택하세요.'); return
            value, pointer = self.values[selection[0]]
            self.search_base = self.pointer_tokens(pointer)
        self.search_term = term
        self.search_cancel = stop = threading.Event()
        generation, revision, base, events = self.generation, self.search_revision, self.search_base, self.events
        self.search_info.set(f'“{term}” 검색 중… 다른 파일을 선택하면 검색을 취소합니다.')
        def checkpoint():
            if stop.is_set(): raise InterruptedError()
        def worker():
            try:
                hits = [base + tokens for tokens in search_json(value, term, checkpoint)]
                checkpoint(); events.put((generation, 'search', (revision, hits)))
            except InterruptedError: pass
            except Exception as exc: events.put((generation, 'search_error', (revision, str(exc))))
        threading.Thread(target=worker, daemon=True, name='json-content-search').start()

    def render_search(self):
        self.results.delete(*self.results.get_children())
        pages = max(1, (len(self.search_hits) + self.PAGE - 1) // self.PAGE)
        self.search_page = max(1, min(self.search_page, pages))
        for i in range((self.search_page-1)*self.PAGE, min(self.search_page*self.PAGE, len(self.search_hits))):
            tokens = self.search_hits[i]
            value = value_at(self.document_value, tokens)
            path = ''.join('/' + escape_token(token) for token in tokens)
            summary = value_description(value) if isinstance(value, (dict, list)) else str(value)[:160]
            self.results.insert('', 'end', iid=str(i), values=(path or '최상위', summary))
        self.search_page_info.set(f'{self.search_page if self.search_hits else 0} / {pages if self.search_hits else 0}쪽 · 페이지당 {self.PAGE}개')
        self.search_previous.configure(state='normal' if self.search_page > 1 else 'disabled')
        self.search_next.configure(state='normal' if self.search_page < pages else 'disabled')
        self.results_area.pack(fill='x', before=self.body, pady=(0, 8))

    def move_search(self, delta):
        self.search_page += delta; self.render_search()

    def select_search_result(self, _event=None):
        selected = self.results.selection()
        if not selected: return
        index = int(selected[0])
        if index >= len(self.search_hits): return
        tokens = self.search_hits[index]
        root = self.tree.get_children()
        if not root: return
        iid, pointer = root[0], ''
        for token in tokens:
            self.tree.item(iid, open=True)
            children = self.tree.get_children(iid)
            if children and children[0] not in self.values and children[0] not in self.more:
                self.populate(iid)
            pointer += '/' + escape_token(token)
            child = next((c for c in self.tree.get_children(iid) if c in self.values and self.values[c][1] == pointer), None)
            if child is None:
                value = self.values[iid][0][token]
                # Reveal a distant array item without rendering every preceding item.
                child = self.add(iid, str(token), value, pointer)
                self.order_children(iid)
            iid = child
        self.tree.selection_set(iid); self.tree.focus(iid); self.tree.see(iid); self.select()

    def load(self, path):
        if not self.alive: return
        self.reset_input()
        path = Path(path)
        self.cancel.set()
        self.cancel = threading.Event()
        stop = self.cancel
        self.generation += 1
        generation = self.generation
        self.loading = True
        self.progress.configure(value=0)
        self.path.set(str(path)); self.status.set("JSON을 읽고 있습니다.")
        self.stop_button.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.values.clear(); self.more.clear()
        self.selection_info.set("새 파일을 읽고 있습니다."); self._preview("")
        events = self.events
        def worker():
            last = -1
            def checkpoint():
                if stop.is_set():
                    raise InterruptedError("읽기를 중단했습니다.")
            def progress(done, total, count):
                nonlocal last
                now = time.monotonic()
                if now - last >= 0.15:
                    last = now
                    events.put((generation, "progress", (done, total, count)))
            try:
                document = read_document(path, fmt='auto', progress=progress, checkpoint=checkpoint)
                checkpoint()
                events.put((generation, "done", document))
            except Exception as exc:
                events.put((generation, "error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="json-viewer").start()

    def poll(self):
        if not self.alive:
            return
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation:
                    continue
                if kind in ('search', 'search_error'):
                    revision, payload = value
                    if revision != self.search_revision: continue
                    if kind == 'search_error': self.search_info.set(payload)
                    else:
                        self.search_hits = payload
                        self.search_info.set(f'“{self.search_term}” · {len(payload):,}개 일치 · 결과를 누르면 해당 항목으로 이동합니다.')
                        self.search_page = 1; self.render_search()
                elif kind == "progress":
                    done, total, count = value
                    self.status.set(f"읽기·해석 {done/1048576:,.1f}/{total/1048576:,.1f} MB · {count:,}건 해석")
                    self.progress.configure(value=100 * done / total if total else 0)
                else:
                    self.loading = False
                    self.stop_button.configure(state="disabled")
                    if kind == "error":
                        self.status.set(value); self.progress.configure(value=0)
                    else:
                        self.display(value)
        except queue.Empty:
            pass
        self.job = self.window.after(80, self.poll)

    def display(self, document):
        self.clear_search()
        self.tree.delete(*self.tree.get_children()); self.values.clear(); self.more.clear()
        self.progress.configure(value=100)
        docs = document.docs
        total = sum(len(doc) if isinstance(doc, list) else 1 for doc in docs)
        self.status.set(f"JSON 문서 {len(docs):,}개 · 최상위 항목 {total:,}개"
                        " · 내부 배열 개수는 해당 필드를 선택해 확인하세요.")
        value = docs[0] if len(docs) == 1 else docs
        self.document_value = value
        root = self.add("", "문서 1" if len(docs) == 1 else "JSON 문서 모음", value, "")
        self.tree.item(root, open=True); self.populate(root)
        self.tree.selection_set(root); self.tree.focus(root); self.select()

    def add(self, parent, label, value, path):
        iid = self.tree.insert(parent, "end", text=label, values=(value_description(value),))
        self.values[iid] = (value, path)
        if isinstance(value, (dict, list)) and value:
            self.tree.insert(iid, "end", text="펼치기")
        return iid

    def populate(self, iid, start=0):
        value, path = self.values[iid]
        if start == 0:
            self.tree.delete(*self.tree.get_children(iid))
        if isinstance(value, dict):
            items = itertools.islice(value.items(), start, start + self.PAGE)
        elif isinstance(value, list):
            items = ((i, value[i]) for i in range(start, min(start + self.PAGE, len(value))))
        else:
            return
        existing_paths = {self.values[c][1] for c in self.tree.get_children(iid) if c in self.values}
        for key, item in items:
            item_path = path + "/" + escape_token(key)
            if item_path not in existing_paths: self.add(iid, str(key), item, item_path)
        if start + self.PAGE < len(value):
            marker = self.tree.insert(iid, "end", text=f"다음 {min(self.PAGE,len(value)-start-self.PAGE):,}개 보기 (더블클릭 / Enter)")
            self.more[marker] = (iid, start + self.PAGE)
        self.order_children(iid)

    def order_children(self, iid):
        value = self.values[iid][0]
        order = {str(key): i for i, key in enumerate(value)} if isinstance(value, dict) else None
        children = self.tree.get_children(iid)
        def sort_key(child):
            if child not in self.values: return len(value) + 1
            label = self.tree.item(child, 'text')
            return order[label] if order is not None else int(label)
        for position, child in enumerate(sorted(children, key=sort_key)):
            self.tree.move(child, iid, position)

    def expand(self, _event=None):
        iid = self.tree.focus()
        children = self.tree.get_children(iid)
        if iid in self.values and children and children[0] not in self.values and children[0] not in self.more:
            self.populate(iid)

    def load_more(self, _event=None):
        iid = self.tree.focus()
        if iid in self.more:
            parent, start = self.more.pop(iid)
            self.tree.delete(iid); self.populate(parent, start)
            return "break"

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection or selection[0] not in self.values:
            return
        value, path = self.values[selection[0]]
        self.selection_info.set(f"{path or '최상위'}\n{value_description(value)}")
        self._preview(json.dumps(bounded_preview(value), ensure_ascii=False, indent=2)[:50000])

    def _preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end"); self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def destroyed(self, event):
        if event.widget == self.window and self.alive:
            self.alive = False
            self.loading = False
            self.generation += 1
            self.cancel.set()
            self.window.after_cancel(self.job)
            self.job = None
            self.events.close()
            self.browser.close()
            self.values.clear(); self.more.clear()
            self.search_cancel.set(); self.search_hits.clear(); self.document_value = None
            release_variables(self)
            callback, self.on_close = self.on_close, None
            if callback: callback(self)
