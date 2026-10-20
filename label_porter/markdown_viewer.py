"""Offline Markdown reader shared by all help pages; original text stays intact."""
from __future__ import annotations

import html
import re
import tkinter as tk
from tkinter import ttk
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from tkinterweb import HtmlFrame

DOCUMENTS = {"AI_규칙작성_가이드.md": 0, "사용설명.md": 1, "후처리_지원범위.md": 2}
STYLE = """
body { margin: 0; padding: 26px 32px; background: #fff; color: #253044;
       font-family: "Malgun Gothic", sans-serif; font-size: 16px; line-height: 1.75; }
h1 { font-size: 29px; color: #182a45; margin: 0 0 22px; line-height: 1.4; }
h2 { font-size: 23px; color: #182a45; margin: 34px 0 16px;
     padding-bottom: 10px; border-bottom: 2px solid #dce5ef; line-height: 1.5; }
h3 { font-size: 19px; color: #304b70; margin: 25px 0 12px; line-height: 1.5; }
h4,h5,h6 { font-size: 17px; margin: 20px 0 10px; }
p { margin: 0 0 16px; }
ul,ol { margin: 10px 0 20px; padding-left: 28px; }
li { margin: 6px 0; }
li p { margin: 5px 0; }
strong { color: #182a45; font-weight: bold; }
a { color: #1b609c; text-decoration: underline; }
code { font-family: Consolas, monospace; font-size: 14px; background: #edf2f7; color: #254b6c; }
pre { background: #f3f6fa; padding: 18px; border: 1px solid #d9e2ed;
      white-space: pre-wrap; font-family: Consolas, monospace; font-size: 14px; line-height: 1.6; }
pre code { background: #f3f6fa; }
blockquote { margin: 20px 0; padding: 14px 20px; background: #eef5fc;
             border-left: 4px solid #6e91b6; }
blockquote p { margin: 0; }
table { width: 100%; border-collapse: collapse; margin: 18px 0 26px; font-size: 15px; }
th { background: #eaf0f7; color: #203c5e; font-weight: bold; text-align: left; }
th,td { border: 1px solid #d5dfea; padding: 11px 13px; vertical-align: top; }
tr:nth-child(even) td { background: #f8fafc; }
hr { border: 0; border-top: 1px solid #dce5ef; margin: 28px 0; }
"""


def render_document(source):
    """Render only Markdown, with local document links and no active HTML/images."""
    md = MarkdownIt("js-default", {"html": False, "linkify": False, "typographer": False})
    md.renderer.rules["image"] = lambda tokens, idx, options, env: html.escape(tokens[idx].content)
    headings = []
    tokens = md.parse(source)
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            anchor = f"section-{len(headings)}"
            token.attrSet("id", anchor)
            inline = tokens[i + 1]
            title = "".join(t.content for t in (inline.children or []) if t.type in ("text", "code_inline"))
            headings.append((anchor, title, int(token.tag[1:])))
        for child in token.children or []:
            if child.type == "link_open":
                link = child.attrGet("href") or ""
                parsed = urlsplit(link)
                # Render unknown links as inert text. The help reader never navigates the network/filesystem.
                if not (link.startswith("#") or (not parsed.scheme and not parsed.netloc and unquote(parsed.path) in DOCUMENTS)):
                    child.attrSet("href", "#")
    body = md.renderer.render(tokens, md.options, {})
    return "<!doctype html><html><head><style>" + STYLE + "</style></head><body>" + body + "</body></html>", headings


class MarkdownViewer(ttk.Frame):
    def __init__(self, parent, source, navigate=None):
        super().__init__(parent)
        self.source = source
        self.html_source, self.headings = render_document(source)
        self.navigate = navigate
        self.query = tk.StringVar()
        self.match_label = tk.StringVar(value="검색")
        self.scale = tk.StringVar(value="100%")
        self.match_count = self.match_index = 0
        self.search_job = None
        toolbar = ttk.Frame(self); toolbar.pack(fill="x", pady=(0, 8))
        toolbar.columnconfigure(1, weight=3); toolbar.columnconfigure(3, weight=2)
        ttk.Label(toolbar, text="목차").grid(row=0, column=0, padx=(0, 6))
        self.toc = ttk.Combobox(toolbar, state="readonly", width=24,
                               values=[("  " if level > 2 else "") + title for _, title, level in self.headings],
                               style="Compact.TCombobox")
        self.toc.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        if self.headings: self.toc.current(0)
        self.toc.bind("<<ComboboxSelected>>", self._jump)
        ttk.Label(toolbar, text="찾기").grid(row=0, column=2, padx=(0, 6))
        self.search_entry = ttk.Entry(toolbar, textvariable=self.query, width=16, style="Compact.TEntry")
        self.search_entry.grid(row=0, column=3, sticky="ew")
        self.search_entry.bind("<Return>", lambda _: self.find(1))
        self.search_entry.bind("<Shift-Return>", lambda _: self.find(-1))
        ttk.Button(toolbar, text="‹", width=2, command=lambda: self.find(-1), style="Compact.TButton").grid(row=0, column=4, padx=(5, 2))
        ttk.Button(toolbar, text="›", width=2, command=lambda: self.find(1), style="Compact.TButton").grid(row=0, column=5)
        ttk.Label(toolbar, textvariable=self.match_label, width=8, anchor="center").grid(row=0, column=6)
        size = ttk.Combobox(toolbar, textvariable=self.scale, state="readonly", width=6,
                            values=("85%", "100%", "115%", "130%", "150%"), style="Compact.TCombobox")
        size.grid(row=0, column=7); size.bind("<<ComboboxSelected>>", self._scale)
        self.browser = HtmlFrame(self, messages_enabled=False, javascript_enabled=False, images_enabled=False,
                                 objects_enabled=False, forms_enabled=False, threading_enabled=False,
                                 horizontal_scrollbar="auto", on_link_click=self._link,
                                 request_func=self._deny_request)
        self.browser.pack(fill="both", expand=True)
        self.browser.load_html(self.html_source, base_url="about:blank")
        self.query.trace_add("write", self._schedule_find)
        self.bind("<Destroy>", self._destroy, add="+")
        self.browser.bind("<Control-f>", self._focus_search)
        self.search_entry.bind("<Escape>", lambda _: self.query.set(""))
        self.pack(fill="both", expand=True)

    @staticmethod
    def _deny_request(*args):
        raise ValueError("도움말은 내장 문서만 표시합니다.")

    def _focus_search(self, _=None):
        self.search_entry.focus_set(); self.search_entry.selection_range(0, "end")
        return "break"

    def _jump(self, _=None):
        index = self.toc.current()
        if index >= 0: self.browser.document.getElementById(self.headings[index][0]).scrollIntoView()

    def _link(self, url):
        parsed = urlsplit(url)
        path = unquote(parsed.path)
        if path in DOCUMENTS and self.navigate:
            self.navigate(DOCUMENTS[path])
        elif parsed.fragment:
            try: self.browser.document.getElementById(unquote(parsed.fragment)).scrollIntoView()
            except (tk.TclError, LookupError): pass

    def _schedule_find(self, *_):
        if self.search_job is not None: self.after_cancel(self.search_job)
        self.search_job = self.after(250, self._new_search)

    def _new_search(self):
        self.search_job = None
        self.match_count = self.match_index = 0
        self.find(1)

    def find(self, step=1):
        if self.search_job is not None:
            self.after_cancel(self.search_job); self.search_job = None
            self.match_count = self.match_index = 0
        text = self.query.get().strip()
        if not text:
            self.browser.find_text("")
            self.match_count = self.match_index = 0; self.match_label.set("검색"); return
        requested = self.match_index + step
        if requested < 1: requested = self.match_count or 1
        if self.match_count and requested > self.match_count: requested = 1
        self.match_count = self.browser.find_text(re.escape(text), select=requested)
        self.match_index = min(requested, self.match_count)
        self.match_label.set(f"{self.match_index}/{self.match_count}" if self.match_count else "결과 없음")

    def _scale(self, _=None):
        self.browser.configure(fontscale=int(self.scale.get().rstrip("%")) / 100)

    def _destroy(self, event):
        if event.widget is self and self.search_job is not None:
            self.after_cancel(self.search_job); self.search_job = None
