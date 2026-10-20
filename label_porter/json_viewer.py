"""Read-only JSON inspector with background loading and lazy tree expansion."""
import itertools
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

from .formats import escape_token, read_document


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

    def __init__(self, parent, initial=None):
        self.window = tk.Toplevel(parent)
        self.window.title("JSON 뷰어 · 항목 수와 구조 확인")
        self.window.geometry("1000x720")
        self.window.minsize(700, 480)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.alive, self.loading = True, False
        self.generation = 0
        self.values, self.more = {}, {}
        self.path = tk.StringVar(value="JSON 파일을 선택하세요.")
        self.status = tk.StringVar(value="읽기 전용 · 파일과 프로젝트 규칙을 변경하지 않습니다.")
        self.selection_info = tk.StringVar(value="배열을 선택하면 정확한 항목 수를 확인할 수 있습니다.")
        toolbar = ttk.Frame(self.window, padding=12); toolbar.pack(fill="x")
        ttk.Button(toolbar, text="JSON 파일 열기…", command=self.choose).pack(side="left")
        self.stop_button = ttk.Button(toolbar, text="읽기 중단", command=lambda: self.cancel.set(), state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(self.window, textvariable=self.path, padding=(12, 0), wraplength=920).pack(fill="x")
        ttk.Label(self.window, textvariable=self.status, padding=12, wraplength=920).pack(fill="x")
        self.progress = ttk.Progressbar(self.window); self.progress.pack(fill="x", padx=12, pady=(0, 10))
        body = ttk.Panedwindow(self.window, orient="horizontal"); body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=1); body.add(right, weight=1)
        self.tree = ttk.Treeview(left, columns=("type",), show="tree headings")
        self.tree.heading("#0", text="필드 / 배열 인덱스"); self.tree.heading("type", text="자료형 · 개수")
        self.tree.column("#0", width=220); self.tree.column("type", width=180)
        scroll = ttk.Scrollbar(left, command=self.tree.yview); scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(fill="both", expand=True)
        ttk.Label(right, textvariable=self.selection_info, wraplength=440).pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(right, text="값 미리보기 · 큰 값은 일부만 표시합니다.", foreground="#69717e").pack(anchor="w", padx=10)
        self.preview = scrolledtext.ScrolledText(right, wrap="word", font=("Consolas", 10), state="disabled")
        self.preview.pack(fill="both", expand=True, padx=(10, 0), pady=(8, 0))
        self.tree.bind("<<TreeviewOpen>>", self.expand)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Double-1>", self.load_more)
        self.tree.bind("<Return>", self.load_more)
        self.window.bind("<Destroy>", self.destroyed)
        self.job = self.window.after(80, self.poll)
        if initial:
            self.load(Path(initial))

    def choose(self):
        path = filedialog.askopenfilename(parent=self.window, title="구조를 확인할 JSON 파일",
            filetypes=[("JSON", "*.json *.jsonl *.ndjson"), ("모든 파일", "*.*")])
        if path:
            self.load(Path(path))

    def load(self, path):
        self.cancel.set()
        self.cancel = threading.Event()
        stop = self.cancel
        self.generation += 1
        generation = self.generation
        self.loading = True
        self.path.set(str(path)); self.status.set("JSON을 읽고 있습니다.")
        self.stop_button.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.values.clear(); self.more.clear()
        self.selection_info.set("새 파일을 읽고 있습니다."); self._preview("")
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
                    self.events.put((generation, "progress", (done, total, count)))
            try:
                document = read_document(path, progress=progress, checkpoint=checkpoint)
                checkpoint()
                self.events.put((generation, "done", document))
            except Exception as exc:
                self.events.put((generation, "error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="json-viewer").start()

    def poll(self):
        if not self.alive:
            return
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "progress":
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
        self.progress.configure(value=100)
        docs = document.docs
        total = sum(len(doc) if isinstance(doc, list) else 1 for doc in docs)
        self.status.set(f"JSON 문서 {len(docs):,}개 · 최상위 항목 {total:,}개"
                        " · 내부 배열 개수는 해당 필드를 선택해 확인하세요.")
        value = docs[0] if len(docs) == 1 else docs
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
        for key, item in items:
            self.add(iid, str(key), item, path + "/" + escape_token(key))
        if start + self.PAGE < len(value):
            marker = self.tree.insert(iid, "end", text=f"다음 {min(self.PAGE,len(value)-start-self.PAGE):,}개 보기 (더블클릭 / Enter)")
            self.more[marker] = (iid, start + self.PAGE)

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
        if event.widget == self.window:
            self.alive = False
            self.cancel.set()
            self.window.after_cancel(self.job)
