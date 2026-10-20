"""A read-only paired-data preview. File I/O and evaluation stay off the Tk thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

from .pair_preview import display, fingerprint, load_preview, match_preview, matching_indices
from .ui import BG, INK, MUTED


class PairPreview(ttk.Frame):
    def __init__(self, parent, profile, source_hint="", result_hints=(), *, configured=False):
        super().__init__(parent)
        self.configured, self.example_index = configured, 0
        self.auto_job = None
        self.profile, self.closed, self.busy = profile, False, False
        self.cache = self.pair = None
        self.generation = 0
        self.cancel_event = threading.Event()
        self.queue = queue.Queue()
        hint = Path(source_hint) if source_hint else None
        self.source = tk.StringVar(value=str(hint) if hint and (configured or hint.is_file()) else "")
        self.root_path = tk.StringVar(value=str(hint if hint.is_dir() else hint.parent) if hint else "")
        self.results = list(result_hints)
        self.result_label = tk.StringVar(value=self._result_label())
        self.number = tk.StringVar(value="1")
        self.status = tk.StringVar(value="원본 JSON과 보완 결과를 선택하면 규칙의 KEY가 같은 한 쌍을 보여줍니다.")
        self.controls = []
        self._build()
        self.source.trace_add("write", lambda *_: self._invalidate())
        self.root_path.trace_add("write", lambda *_: self._invalidate())
        self.number.trace_add("write", lambda *_: self._invalidate(cache=False))
        self.poll_job = self.after(80, self._poll)
        if configured:
            self.auto_job = self.after_idle(self._autoload)

    def _autoload(self):
        self.auto_job = None
        if not self.closed: self.load()

    def _next_example(self):
        self.example_index += 1
        self.load()

    def _build(self):
        inputs = ttk.Frame(self); inputs.pack(fill="x")
        inputs.columnconfigure(1, weight=1)
        for row, label, variable, command in (
            (0, "원본 폴더" if self.configured else "원본 JSON", self.source, self._choose_source),
            (1, "플랫폼 / 보완 결과", self.result_label, self._choose_results),
        ):
            ttk.Label(inputs, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(inputs, textvariable=variable, state="readonly").grid(row=row, column=1, sticky="ew", pady=3)
            if not self.configured:
                button = ttk.Button(inputs, text="파일 선택…", command=command)
                button.grid(row=row, column=2, padx=(8, 0), pady=3)
                self.controls.append(button)
        source_key = self.profile["source"]["key"]
        if not self.configured and isinstance(source_key, dict) and source_key.get("meta") == "relative_path":
            ttk.Label(inputs, text="원본 기준 폴더").grid(row=2, column=0, sticky="w")
            ttk.Entry(inputs, textvariable=self.root_path, state="readonly").grid(row=2, column=1, sticky="ew")
            button = ttk.Button(inputs, text="폴더 선택…", command=self._choose_root)
            button.grid(row=2, column=2, padx=(8, 0)); self.controls.append(button)
        actions = ttk.Frame(self); actions.pack(fill="x", pady=6)
        if not self.configured:
            ttk.Label(actions, text="원본 항목").pack(side="left")
        self.spin = ttk.Spinbox(actions, from_=1, to=1, textvariable=self.number, width=7)
        if not self.configured: self.spin.pack(side="left", padx=8)
        self.controls.append(self.spin)
        if self.configured:
            ttk.Label(actions, text="입력 단계의 데이터에서 자동 연결", foreground=MUTED).pack(side="left", padx=(0, 12))
            self.next_button = ttk.Button(actions, text="다른 예시", command=self._next_example, state="disabled")
            self.next_button.pack(side="left", padx=(0, 8))
        self.load_button = ttk.Button(actions, text="다시 불러오기" if self.configured else "KEY로 한 쌍 불러오기", command=self.load)
        self.load_button.pack(side="left"); self.controls.append(self.load_button)
        self.cancel_button = ttk.Button(actions, text="중단", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        note = ttk.Label(self, textvariable=self.status, foreground=MUTED, wraplength=780)
        note.pack(fill="x", pady=(0, 6))
        self.bind("<Configure>", lambda e: note.configure(wraplength=max(280, e.width)))
        self.views = ttk.Notebook(self); self.views.pack(fill="both", expand=True)
        graph, raw = ttk.Frame(self.views), ttk.Frame(self.views)
        self.views.add(graph, text="실제 값 연결")
        self.views.add(raw, text="JSON 한 쌍")
        self.canvas = tk.Canvas(graph, background=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(graph, command=self.canvas.yview); scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set); self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda *_: self._draw())
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-1>", self._select_row)
        panes = ttk.Panedwindow(raw, orient="horizontal"); panes.pack(fill="both", expand=True)
        self.json_boxes = []
        for label in ("원본 라벨 JSON · 읽기 전용", "플랫폼 / 보완 JSON · 읽기 전용"):
            panel = ttk.Frame(panes, padding=5); panes.add(panel, weight=1)
            ttk.Label(panel, text=label).pack(anchor="w", pady=5)
            box = scrolledtext.ScrolledText(panel, wrap="word", font=("Consolas", 10), width=35, height=6, state="disabled")
            box.pack(fill="both", expand=True); self.json_boxes.append(box)
        self._draw()

    def _result_label(self):
        return str(self.results[0]) if len(self.results) == 1 else f"{len(self.results):,}개 파일 선택" if self.results else ""

    def _choose_source(self):
        path = filedialog.askopenfilename(parent=self, title="예시로 볼 원본 JSON",
                                         initialdir=self.root_path.get() or None,
                                         filetypes=[("JSON", "*.json *.jsonl *.ndjson")])
        if path:
            self.source.set(path)
            if not self.root_path.get() or not Path(path).resolve().is_relative_to(Path(self.root_path.get()).resolve()):
                self.root_path.set(str(Path(path).parent))
            self.number.set("1")

    def _choose_results(self):
        paths = filedialog.askopenfilenames(parent=self, title="플랫폼 최종 결과 또는 보완 데이터",
                                           filetypes=[("JSON / CSV", "*.json *.jsonl *.ndjson *.csv")])
        if paths:
            self.results = list(paths); self.result_label.set(self._result_label()); self._invalidate()

    def _choose_root(self):
        path = filedialog.askdirectory(parent=self, title="상대 경로 KEY의 원본 기준 폴더")
        if path:
            self.root_path.set(path)

    @staticmethod
    def _text(box, text):
        box.configure(state="normal"); box.delete("1.0", "end"); box.insert("1.0", text); box.configure(state="disabled")

    def _set_busy(self, value):
        self.busy = value
        for control in self.controls:
            control.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        if self.configured:
            self.next_button.configure(state="disabled" if value or not self.pair or self.pair.get("example_total", 0) < 2 else "normal")

    def _invalidate(self, cache=True):
        self.cancel_event.set(); self.generation += 1
        if cache:
            self.cache = None
        self.pair = None
        self._set_busy(False)
        for box in self.json_boxes:
            self._text(box, "")
        self.status.set("선택한 입력에서 같은 KEY의 예시를 자동으로 찾습니다." if self.configured else
                        "‘KEY로 한 쌍 불러오기’를 눌러 선택한 원본의 실제 값을 확인하세요.")
        self._draw()

    def cancel(self):
        self._invalidate(cache=False)
        self.status.set("프리뷰 불러오기를 중단했습니다. 파일은 수정하지 않았습니다.")

    def load(self):
        if self.busy or self.closed:
            return
        if not self.source.get() or not self.results:
            self.status.set("원본 JSON과 플랫폼 결과 또는 보완 데이터 파일을 선택하세요.")
            return
        try:
            index = int(self.number.get()) - 1
            if index < 0:
                raise ValueError
        except ValueError:
            self.status.set("원본 항목 번호는 1 이상의 정수로 입력하세요.")
            return
        self._invalidate(cache=False)
        self.cancel_event = cancel = threading.Event()
        generation, cached = self.generation, self.cache
        example_index = self.example_index
        source, results = self.source.get(), list(self.results)
        root = self.root_path.get() or str(Path(source).parent)
        self.status.set("원본과 보완 결과를 읽고 KEY를 확인하고 있습니다…")
        self._set_busy(True)
        def send(kind, value):
            self.queue.put((generation, kind, value))
        def work():
            try:
                def checkpoint():
                    if cancel.is_set(): raise ValueError("프리뷰 불러오기를 중단했습니다.")
                stamp = fingerprint(source, results, root, self.profile["source"].get("glob", "**/*.json"), checkpoint) if cached else None
                data = cached if cached and cached.fingerprint == stamp else load_preview(
                    self.profile, source, results, root, progress=lambda text: send("progress", text), cancel=cancel.is_set)
                if cancel.is_set():
                    return
                send("loaded", data)
                selected = matching_indices(data) if self.configured else []
                if self.configured and not selected:
                    raise ValueError("유일하게 연결되는 KEY가 없습니다. 필드 연결을 확인하거나 전체 검증에서 누락·중복 사유를 확인하세요.")
                pair = match_preview(data, selected[example_index % len(selected)] if self.configured else index)
                if self.configured:
                    pair.update(example_position=example_index % len(selected) + 1, example_total=len(selected))
                if not cancel.is_set():
                    send("pair", pair)
            except Exception as exc:
                send("error", str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, kind, value = self.queue.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "progress":
                    self.status.set(value)
                elif kind == "loaded":
                    self.cache = value; self.spin.configure(to=len(value.originals))
                elif kind == "error":
                    self._set_busy(False); self.status.set("프리뷰 확인: " + value)
                elif kind == "pair":
                    self._set_busy(False); self.show_pair(value)
        except queue.Empty:
            pass
        self.poll_job = self.after(80, self._poll)

    def show_pair(self, pair):
        self.pair = pair
        extra = f" · KEY 해석 불가 {pair['invalid_result_keys']:,}건" if pair["invalid_result_keys"] else ""
        example = f"예시 {pair['example_position']:,} / {pair['example_total']:,} · " if self.configured else ""
        self.status.set(f"{example}KEY {display(pair['key'], 100)} · {pair['message']}{extra}\n"
                        f"{pair['source_location']} ↔ {pair['result_location']}\n"
                        "선택한 자료의 한 쌍 예시입니다. 실제 저장 전 전체 데이터를 검증하세요.")
        for box, name in zip(self.json_boxes, ("source", "result")):
            self._text(box, display(pair[name], 200000))
        self.canvas.yview_moveto(0); self._draw()
        self._set_busy(False)

    def _draw(self):
        if self.closed:
            return
        from .rule_review import COLORS, LABELS, describe_expression
        canvas = self.canvas; canvas.delete("all")
        width = max(500, canvas.winfo_width())
        if not self.pair:
            canvas.create_text(width / 2, 55, text="규칙의 KEY가 같은 원본과 보완 결과 한 쌍을 불러오세요.\n데이터와 규칙은 수정하지 않습니다.",
                               width=width - 40, fill=MUTED, font=("맑은 고딕", 10))
            canvas.configure(scrollregion=(0, 0, width, 150))
            return
        gap, pad, y = 90, 12, 10
        cell = (width - gap - pad * 2) / 2
        right = pad + cell + gap
        def short(text):
            return text if len(text) <= 450 else text[:450] + "… (클릭하여 더 보기)"
        for index, row in enumerate(self.pair["rows"]):
            color, tag = COLORS[row["kind"]], f"pair:{index}"
            title = canvas.create_text(pad, y, text=f"{index + 1:02d}  {LABELS[row['kind']]}",
                                       anchor="nw", fill=color, font=("맑은 고딕", 10, "bold"), width=width - 24, tags=tag)
            y = canvas.bbox(title)[3] + 8
            source_path = describe_expression(row["source_path"], "source") if row["kind"] == "key" else row["source_path"]
            result_path = describe_expression(row["result_expr"])
            if row.get("nested"):
                result_path = "해당 목록 항목 · " + result_path
            left = source_path + "\n\n원본 값\n" + short(row["source"])
            target = result_path + "\n\n보완 값\n" + short(row["result"])
            if row["evaluated"] and row["evaluated"] != row["result"]:
                target += "\n\n변환 후 가져올 값\n" + short(row["evaluated"])
            a = canvas.create_text(pad + 10, y + 10, text=left, anchor="nw", width=cell - 20, fill=INK, font=("맑은 고딕", 10), tags=tag)
            b = canvas.create_text(right + 10, y + 10, text=target, anchor="nw", width=cell - 20, fill=INK, font=("맑은 고딕", 10), tags=tag)
            bottom = max(canvas.bbox(a)[3], canvas.bbox(b)[3]) + 10
            for x in (pad, right):
                rect = canvas.create_rectangle(x, y, x + cell, bottom, fill="white", outline="#dce3ee", tags=tag)
                canvas.tag_lower(rect, a)
            canvas.create_line(pad + cell + 6, (y + bottom) / 2, right - 6, (y + bottom) / 2, fill=color, width=2,
                               arrow="both" if row["kind"] == "key" else "first", tags=tag)
            note = canvas.create_text(pad, bottom + 6, text=row["note"], anchor="nw", width=width - 24,
                                      fill=MUTED, font=("맑은 고딕", 9), tags=tag)
            y = canvas.bbox(note)[3] + 18
        if self.pair["total_connections"] > len(self.pair["rows"]):
            canvas.create_text(pad, y, text=f"연결값은 앞 {len(self.pair['rows'])}개까지 표시합니다. 전체 한 건은 ‘JSON 한 쌍’에서 확인하세요.",
                               anchor="nw", width=width - 24, fill=MUTED)
            y += 60
        canvas.configure(scrollregion=(0, 0, width, y))

    def _select_row(self, event):
        if not self.pair:
            return
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x + 1, y + 1)):
            tag = next((t for t in self.canvas.gettags(item) if t.startswith("pair:")), None)
            if tag:
                row = self.pair["rows"][int(tag.split(":")[1])]
                dialog = tk.Toplevel(self)
                dialog.title("연결된 실제 값 · 읽기 전용"); dialog.geometry("800x600"); dialog.transient(self.winfo_toplevel())
                box = scrolledtext.ScrolledText(dialog, wrap="word", font=("Consolas", 10))
                box.pack(fill="both", expand=True)
                self._text(box, f"{row['note']}\n\n원본 값\n{row['source']}\n\n보완 값\n{row['result']}\n\n변환 후 가져올 값\n{row['evaluated']}")
                break

    def close(self):
        if self.closed:
            return
        self.closed = True; self.cancel_event.set(); self.generation += 1
        if self.auto_job is not None:
            self.after_cancel(self.auto_job)
            self.auto_job = None
        self.after_cancel(self.poll_job)
        self.cache = self.pair = None
