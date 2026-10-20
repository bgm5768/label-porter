"""Read-only inspection of an AI-authored profile; never recompiles the DSL."""
from __future__ import annotations

import copy
import json
import tkinter as tk
from tkinter import font, scrolledtext, ttk

from .config import validate_config
from .ui import BG, INK, MUTED
from .pair_preview_ui import PairPreview

COLORS = {"key": "#2563eb", "field": "#087e6b", "items": "#8052ba", "guard": "#ad6c13", "rebuild_items": "#8052ba"}
LABELS = {"key": "같은 건 찾기", "field": "수정값 반영", "items": "반복 목록", "guard": "기존값·동일성 검사", "rebuild_items": "목록 전체 재구성"}
SCOPES = {"source": "현재 원본", "original": "보완 전 원본", "result": "플랫폼 결과",
          "root_source": "전체 원본 레코드", "root_original": "전체 보완 전 레코드", "root_result": "전체 결과 레코드"}
OPERATIONS = {"strip": "앞뒤 공백 제거", "string": "문자열 변환", "integer": "정수 변환", "number": "숫자 변환",
              "basename": "파일명 추출", "parse_json": "JSON 해석", "length": "길이", "regex": "정규식 추출",
              "scale": "배율", "offset": "값 더하기", "round": "반올림", "pluck": "목록 필드 추출",
              "join": "문자열 합치기", "map_values": "값 대응표"}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def describe_expression(expr, default_side="result", source_prefix="", result_prefix=""):
    spec = {"path": expr} if isinstance(expr, str) else expr
    scope = spec.get("scope", default_side)
    if "meta" in spec:
        path = {"file_name": "파일명", "file_stem": "확장자 없는 파일명", "relative_path": "원본 기준 폴더의 상대 경로",
                "result_file": "결과 파일 경로", "result_row": "결과 행 번호", "item_number": "새 목록의 순번 (1부터)"}[spec["meta"]]
    elif "literal" in spec:
        path = "고정값 " + json.dumps(spec["literal"], ensure_ascii=False)
    else:
        prefix = "" if scope.startswith("root_") else source_prefix if scope in ("source", "original") else result_prefix
        path = prefix + spec["path"] or "레코드 전체"
        if "scope" in spec:
            path = SCOPES[scope] + " · " + path
    changes = []
    for operation in spec.get("transforms", []):
        options = {k: v for k, v in operation.items() if k != "op"}
        changes.append(OPERATIONS.get(operation["op"], operation["op"]) + (" " + json.dumps(options, ensure_ascii=False) if options else ""))
    return path + ("\n변환: " + " → ".join(changes) if changes else "")


def profile_connections(profile):
    """Every key, nested mapping and guard is represented without mutating its rule."""
    profile = validate_config(profile)
    rows = []
    def add(kind, source, result, context, raw, notes=""):
        rows.append({"kind": kind, "source": source, "result": result, "context": context,
                     "raw": copy.deepcopy(raw), "notes": notes})
    add("key", describe_expression(profile["source"]["key"], "source"),
        describe_expression(profile["result"]["key"]), "레코드 연결",
        {"source": profile["source"], "result": profile["result"]}, "데이터를 찾는 기준이며 값을 바꾸지 않습니다.")
    def walk(rules, source_prefix="", result_prefix="", context="레코드"):
        for number, rule in enumerate(rules, 1):
            label = f"{context} / 규칙 {number}"
            source = source_prefix + rule["to"]
            result = describe_expression(rule["from"], "result", source_prefix, result_prefix)
            notes = []
            if rule.get("when"): notes.append(f"적용 조건 {len(rule['when'])}개")
            if rule.get("checks"): notes.append(f"사전 검사 {len(rule['checks'])}개")
            if rule["kind"] == "items":
                notes.append("목록 ID가 모두 같아야 반영" if rule.get("require_same_keys", True) else "결과에 없는 항목은 유지")
            elif rule["kind"] == "rebuild_items":
                notes.append("결과 목록으로 교체 · 추가/삭제/분할/병합 반영")
                if rule.get("sort_by"): notes.append("정렬: " + describe_expression(rule["sort_by"]))
                if rule.get("references"): notes.append("기존 태그는 ID·동일성·유일성 확인 후 재연결, 불확실 태그 별도 보관")
            else:
                if rule.get("type"): notes.append("자료형: " + rule["type"])
                notes.append("빈값 보류" if rule.get("nonempty", False) else "빈값 차단 설정 없음")
                if rule.get("allow_create"): notes.append("대상 필드 생성 허용")
                if rule.get("allow_container_replace"): notes.append("객체·배열 전체 교체 허용")
            add(rule["kind"], source, result, label, rule, " · ".join(notes))
            if "expected" in rule:
                add("guard", "보완 전 원본 · " + source, describe_expression(rule["expected"], "result", source_prefix, result_prefix),
                    label + " / 이전값", {"to": rule["to"], "expected": rule["expected"]}, "플랫폼의 이전값과 원본이 일치해야 합니다.")
            if rule["kind"] in ("items", "rebuild_items"):
                source_child = source + "[]"
                expr = rule["from"]
                # A computed list has no literal JSON location. Do not invent one.
                if isinstance(expr, str): result_child = result_prefix + expr + "[]"
                elif set(expr) <= {"path"}: result_child = result_prefix + expr["path"] + "[]"
                else: result_child = "[상위 목록 표현식의 결과][]"
                add("key", describe_expression(rule["source_key"], "source", source_child, result_child),
                    describe_expression(rule["result_key"], "result", source_child, result_child), label + " / 목록 항목 ID",
                    {"source_key": rule["source_key"], "result_key": rule["result_key"]},
                    "기존 태그 연결에만 사용합니다. 새 어절 반영은 막지 않습니다." if rule["kind"] == "rebuild_items" else "항목 순서가 아닌 ID로 연결합니다.")
                for guard in rule.get("identity_guards", []):
                    add("guard", describe_expression(guard["source"], "source", source_child, result_child),
                        describe_expression(guard["result"], "result", source_child, result_child), label + " / 동일성", guard)
                walk(rule["rules"], source_child, result_child, label + " 안쪽")
    walk(profile["rules"])
    return rows


class RuleReview:
    """Paired rows show only configured connections, with full JSON available on click."""
    def __init__(self, parent, profile, on_continue, on_close, *, source_hint="", result_hints=(), configured=False):
        self.profile = copy.deepcopy(validate_config(profile))
        self.rows = profile_connections(self.profile)
        self.on_continue, self.on_close = on_continue, on_close
        self.closed = False
        self.selected = None
        self.window = ttk.Frame(parent)
        self.window.pack(fill="both", expand=True)
        self.query = tk.StringVar()
        self.filter_kind = tk.StringVar(value="전체 연결")
        self.count = tk.StringVar()
        self.status = tk.StringVar(value="규칙과 선택한 한 쌍을 확인하는 화면입니다. 실제 저장 전 전체 입력을 검증하세요.")
        footer = ttk.Frame(self.window, padding=(0, 10, 0, 0)); footer.pack(side="bottom", fill="x")
        note = ttk.Label(footer, textvariable=self.status, wraplength=780, foreground=MUTED)
        note.pack(fill="x", pady=(0, 8))
        footer.bind("<Configure>", lambda e: note.configure(wraplength=max(280, e.width)))
        actions = ttk.Frame(footer); actions.pack(fill="x")
        ttk.Button(actions, text="입력·저장 위치로" if configured else "개요로 돌아가기", command=self._close).pack(side="left")
        self.apply_button = ttk.Button(actions, text="확인 완료 · 전체 검증 →" if configured else "연결 확인 완료 · 입력 선택 →", style="Primary.TButton", command=self._apply)
        self.apply_button.pack(side="right")
        self.pages = ttk.Notebook(self.window); self.pages.pack(fill="both", expand=True)
        visual, preview, raw = [ttk.Frame(self.pages, padding=10) for _ in range(3)]
        for page, name in ((visual, "필드 연결"), (preview, "필드 연결 프리뷰"), (raw, "규칙 JSON")):
            self.pages.add(page, text=name)
        ttk.Label(visual, text="보완 전 원본의 위치  ←  규칙에서 가져올 값", style="Heading.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(visual, text="읽기 전용 · 연결된 항목만 표시합니다. 행을 누르면 해당 규칙의 전체 설정이 아래에 보입니다.", foreground=MUTED, wraplength=750).pack(anchor="w", pady=(0, 10))
        ttk.Label(visual, text=f"레코드 기준 위치  원본: {self.profile['source'].get('records_path') or '파일 전체'}  /  결과: {self.profile['result'].get('records_path') or '파일 전체'}",
                  foreground=MUTED, wraplength=750).pack(anchor="w", pady=(0, 8))
        search = ttk.Frame(visual); search.pack(fill="x", pady=(0, 8))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="필드 찾기").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(search, textvariable=self.query).grid(row=0, column=1, sticky="ew")
        combo = ttk.Combobox(search, textvariable=self.filter_kind, values=["전체 연결", *LABELS.values()], state="readonly", width=20)
        combo.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(visual, textvariable=self.count, foreground=MUTED).pack(anchor="w", pady=(0, 5))
        # The detail pane is intentionally short; its own scrollbar exposes the complete rule.
        self.detail = scrolledtext.ScrolledText(visual, wrap="word", height=4, font=("맑은 고딕", 9), relief="flat", state="disabled")
        self.detail.pack(side="bottom", fill="x", pady=(8, 0))
        self._text(self.detail, "연결 행을 선택하면 조건·변환·검증을 포함한 전체 설정을 확인할 수 있습니다.")
        area = ttk.Frame(visual); area.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(area, background=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(area, command=self.canvas.yview); scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set); self.canvas.pack(fill="both", expand=True)
        self.text_font = font.Font(self.window, family="맑은 고딕", size=10)
        self.bold_font = font.Font(self.window, family="맑은 고딕", size=10, weight="bold")
        self.canvas.bind("<Configure>", self._draw)
        self.canvas.bind("<Button-1>", self._select)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.query.trace_add("write", self._filter)
        self.filter_kind.trace_add("write", self._filter)
        self.preview = PairPreview(preview, self.profile, source_hint, result_hints, configured=configured)
        self.preview.pack(fill="both", expand=True)
        self.raw_box = self._box(raw); self._text(self.raw_box, json_text(self.profile))
        self._draw()

    @staticmethod
    def _box(parent):
        box = scrolledtext.ScrolledText(parent, wrap="word", font=("맑은 고딕", 10), relief="flat", padx=10, pady=10, state="disabled")
        box.pack(fill="both", expand=True)
        return box

    @staticmethod
    def _text(box, value):
        box.configure(state="normal"); box.delete("1.0", "end"); box.insert("1.0", value); box.configure(state="disabled")

    def _filter(self, *_):
        self.canvas.yview_moveto(0)
        self._draw()

    def visible_rows(self):
        query = self.query.get().strip().casefold()
        return [(i, r) for i, r in enumerate(self.rows) if
                (self.filter_kind.get() == "전체 연결" or LABELS[r["kind"]] == self.filter_kind.get()) and
                (not query or query in (r["source"]+r["result"]+r["context"]+r["notes"]).casefold())]

    def _draw(self, event=None):
        if self.closed: return
        canvas = self.canvas
        canvas.delete("all")
        width = max(500, canvas.winfo_width())
        gap, padding = 120, 14
        cell = (width-gap-2*padding)/2
        right = padding+cell+gap
        y = 8
        visible = self.visible_rows()
        self.count.set(f"전체 {len(self.rows):,}개 연결 중 {len(visible):,}개 표시 · 수정값 / 기준 / 검사 포함")
        for index, row in visible:
            color = COLORS[row["kind"]]
            tag = f"connection:{index}"
            canvas.create_text(padding+10, y+8, text=f"{index+1:02d}  {LABELS[row['kind']]} · {row['context']}", anchor="nw",
                               width=width-40, font=self.bold_font, fill=color, tags=tag)
            heading = canvas.bbox(canvas.find_all()[-1])
            start = max(y+32, heading[3]+8)
            left_text = canvas.create_text(padding+12, start+12, text=row["source"], anchor="nw", width=cell-24, font=self.text_font, fill=INK, tags=tag)
            right_text = canvas.create_text(right+12, start+12, text=row["result"], anchor="nw", width=cell-24, font=self.text_font, fill=INK, tags=tag)
            bottom = max(canvas.bbox(left_text)[3], canvas.bbox(right_text)[3])+12
            for x in (padding, right):
                rect = canvas.create_rectangle(x, start, x+cell, bottom, fill="#eef5ff" if index == self.selected else "white",
                                               outline=color if index == self.selected else "#dce3ee", width=2 if index == self.selected else 1, tags=tag)
                canvas.tag_lower(rect, left_text)
            center = (start+bottom)/2
            canvas.create_line(padding+cell+7, center, right-7, center, fill=color, width=2,
                               arrow="first" if row["kind"] == "field" else "both", tags=tag)
            y = bottom+6
            if row["notes"]:
                note = canvas.create_text(padding+10, y, text=row["notes"], anchor="nw", width=width-40, font=self.text_font, fill=MUTED, tags=tag)
                y = canvas.bbox(note)[3]+8
            y += 12
        if not visible:
            canvas.create_text(width/2, 70, text="해당하는 연결이 없습니다. 검색어나 역할 필터를 바꿔 주세요.", width=width-40, fill=MUTED, font=self.text_font)
        canvas.configure(scrollregion=(0, 0, width, max(y, 150)))

    def _select(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x-2, y-2, x+2, y+2)):
            tag = next((t for t in self.canvas.gettags(item) if t.startswith("connection:")), None)
            if tag:
                self.selected = int(tag.split(":")[1])
                row = self.rows[self.selected]
                self._text(self.detail, f"{LABELS[row['kind']]} · {row['context']}\n원본: {row['source']}\n결과: {row['result']}\n{row['notes']}\n\n전체 설정\n{json_text(row['raw'])}")
                self._draw()
                break

    def _apply(self):
        self._close(notify=False)
        self.on_continue()

    def _close(self, notify=True):
        if self.closed: return
        self.closed = True
        self.preview.close()
        self.window.destroy()
        if notify: self.on_close()
