"""Offline Markdown reader shared by all help pages; original text stays intact."""
from __future__ import annotations

import html
import re
import weakref
from io import BytesIO
import tkinter as tk
from PIL import Image, ImageTk
from . import widgets as ttk
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from tkinterweb import HtmlFrame
from .ui_lifecycle import remove_traces, release_variables
from .mermaid_assets import ASSET_PREFIX, diagram_info, read_diagram

DOCUMENTS = {"AI_규칙작성_가이드.md": 0, "사용설명.md": 1, "후처리_지원범위.md": 2,
             "아키텍처_및_규칙엔진.md": 3}
_READERS = weakref.WeakSet()
STYLE = """
body { margin: 0; padding: 26px 32px; background: #fff; color: #1f1f1f;
       font-family: "Malgun Gothic", sans-serif; font-size: 16px; line-height: 1.75; }
h1 { font-size: 29px; color: #1f1f1f; margin: 0 0 22px; line-height: 1.4; }
h2 { font-size: 23px; color: #1f1f1f; margin: 34px 0 16px;
     padding-bottom: 10px; border-bottom: 2px solid #dadce0; line-height: 1.5; }
h3 { font-size: 19px; color: #1f1f1f; margin: 25px 0 12px; line-height: 1.5; }
h4,h5,h6 { font-size: 17px; margin: 20px 0 10px; }
p { margin: 0 0 16px; }
ul,ol { margin: 10px 0 20px; padding-left: 28px; }
li { margin: 6px 0; }
li p { margin: 5px 0; }
strong { color: #1f1f1f; font-weight: bold; }
a { color: #0b57d0; text-decoration: underline; }
code { font-family: Consolas, monospace; font-size: 14px; background: #f1f3f4; color: #1f1f1f; }
pre { background: #f8f9fa; padding: 18px; border: 1px solid #dadce0;
      white-space: pre-wrap; font-family: Consolas, monospace; font-size: 14px; line-height: 1.6; }
pre code { background: #f8f9fa; }
blockquote { margin: 20px 0; padding: 14px 20px; background: #f1f3f4;
             border-left: 4px solid #80868b; }
blockquote p { margin: 0; }
table { width: 100%; border-collapse: collapse; margin: 18px 0 26px; font-size: 15px; }
th { background: #e8eaed; color: #1f1f1f; font-weight: bold; text-align: left; }
th,td { border: 1px solid #dadce0; padding: 11px 13px; vertical-align: top; }
tr:nth-child(even) td { background: #f8f9fa; }
hr { border: 0; border-top: 1px solid #dadce0; margin: 28px 0; }
.mermaid-figure { margin: 16px 0 26px; padding: 12px; border: 1px solid #dadce0; background: #fff; }
.mermaid-figure img { max-width: 100%; height: auto; }
.mermaid-caption { color: #444746; font-size: 13px; margin: 10px 0 0; }
"""


def render_document(source):
    """Render inert Markdown and source-matched, bundled Mermaid images."""
    md = MarkdownIt("js-default", {"html": False, "linkify": False, "typographer": False})
    md.renderer.rules["image"] = lambda tokens, idx, options, env: html.escape(tokens[idx].content)
    default_fence = md.renderer.rules['fence']
    def fence(tokens, idx, options, env):
        token = tokens[idx]
        if token.info.strip().split(' ', 1)[0] != 'mermaid':
            return default_fence(tokens, idx, options, env)
        identity, info = diagram_info(token.content)
        if not info:
            return '<p class="mermaid-caption">아직 준비되지 않은 다이어그램입니다. Mermaid 원문을 표시합니다.</p>' + default_fence(tokens, idx, options, env)
        title = html.escape(info['title'], quote=True)
        width = min(960, info['width'] // 2)
        return (f'<div class="mermaid-figure" id="mermaid-slot-{identity}"><img id="mermaid-{identity}" src="{ASSET_PREFIX}{identity}.png" '
                f'alt="{title}" title="{title}" width="{width}"><p class="mermaid-caption">{title}</p></div>')
    md.renderer.rules['fence'] = fence
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
        self.diagram_scale = tk.StringVar(value='화면에 맞춤')
        self.diagrams = {identity: info for identity, info in
                         (diagram_info(token.content) for token in MarkdownIt().parse(source)
                          if token.type == 'fence' and token.info.strip().split(' ', 1)[0] == 'mermaid') if info}
        self.diagrams_loaded = False
        self.diagram_widgets = {}
        self.diagram_job = None
        self.match_count = self.match_index = 0
        self.search_job = None
        toolbar = ttk.Frame(self); toolbar.pack(fill="x", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        searchbar = ttk.Frame(self); searchbar.pack(fill="x", pady=(0, 10))
        searchbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="목차").grid(row=0, column=0, padx=(0, 6))
        self.toc = ttk.Combobox(toolbar, state="readonly", width=24,
                               values=[("  " if level > 2 else "") + title for _, title, level in self.headings],
                               style="Compact.TCombobox")
        self.toc.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        if self.headings: self.toc.current(0)
        self.toc.bind("<<ComboboxSelected>>", self._jump)
        ttk.Label(searchbar, text="문서 검색").grid(row=0, column=0, padx=(0, 6))
        self.search_entry = ttk.Entry(searchbar, textvariable=self.query, width=16, style="Compact.TEntry")
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_entry.bind("<Return>", lambda _: self.find(1))
        self.search_entry.bind("<Shift-Return>", lambda _: self.find(-1))
        ttk.Button(searchbar, text="‹", width=2, command=lambda: self.find(-1), style="Compact.TButton").grid(row=0, column=2, padx=(5, 2))
        ttk.Button(searchbar, text="›", width=2, command=lambda: self.find(1), style="Compact.TButton").grid(row=0, column=3)
        ttk.Label(searchbar, textvariable=self.match_label, width=8, anchor="center").grid(row=0, column=4)
        size = ttk.Combobox(toolbar, textvariable=self.scale, state="readonly", width=6,
                            values=("85%", "100%", "115%", "130%", "150%"), style="Compact.TCombobox")
        size.grid(row=0, column=2); size.bind("<<ComboboxSelected>>", self._scale)
        if self.diagrams:
            ttk.Label(toolbar, text='도식').grid(row=0, column=3, padx=(10, 4))
            diagram_size = ttk.Combobox(toolbar, textvariable=self.diagram_scale, state='readonly', width=12,
                                       values=('화면에 맞춤', '100%', '150%', '200%'), style='Compact.TCombobox')
            diagram_size.grid(row=0, column=4)
            diagram_size.bind('<<ComboboxSelected>>', self._scale_diagrams)
        root_commands_before = set(self._root()._tclCommands or [])
        self.previous_html_bindings = {sequence: self._root().bind_class('Html', sequence)
                                       for sequence in self._root().bind_class('Html') or ()}
        self.browser = HtmlFrame(self, messages_enabled=False, javascript_enabled=False, images_enabled=bool(self.diagrams),
                                 objects_enabled=False, forms_enabled=False, threading_enabled=False,
                                 horizontal_scrollbar="auto", on_link_click=self._link,
                                 request_func=self._request_resource)
        self.browser.pack(fill="both", expand=True)
        self.browser.bind('<Configure>', self._schedule_diagrams, add='+')
        # Hidden help tabs must not decode several large PNGs at app startup.
        initial_html = re.sub(r'<img\b[^>]*>', '', self.html_source)
        self.browser.load_html(initial_html, base_url="about:blank")
        # TkinterWeb binds private HTML classes through the Tk root. Tk itself
        # doesn't delete these callbacks when the HTML widget is destroyed.
        self.browser_root_commands = set(self._root()._tclCommands or []) - root_commands_before
        # Loading HTML pumps idle work; independently scheduled CTk DPI polling
        # may register a root callback during that interval. It isn't HTML-owned.
        pending_commands = {str(self.tk.call('after', 'info', job)[0]).split()[0]
                            for job in self.tk.call('after', 'info')}
        self.browser_root_commands.difference_update(pending_commands)
        widget = self.browser._html
        self.browser_bindings = []
        for tag in (widget.node_tag, widget.tkinterweb_tag, widget.scrollable_node_tag, f'{widget}.document', 'Html'):
            for sequence in self._root().bind_class(tag) or ():
                script = self._root().bind_class(tag, sequence)
                if any(command in script for command in self.browser_root_commands):
                    self.browser_bindings.append((tag, sequence, script))
        self.traces = [(self.query, self.query.trace_add("write", self._schedule_find))]
        self.bind("<Destroy>", self._destroy, add="+")
        self.bind('<Map>', self._mapped, add='+')
        self.browser.bind("<Control-f>", self._focus_search)
        self.search_entry.bind("<Escape>", lambda _: self.query.set(""))
        _READERS.add(self)
        self.pack(fill="both", expand=True)

    @staticmethod
    def _deny_request(*args):
        raise ValueError("도움말은 내장 문서만 표시합니다.")

    @staticmethod
    def _request_resource(*args):
        return read_diagram(*args)

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

    def _scale_diagrams(self, _=None):
        if self.diagram_job is not None:
            self.after_cancel(self.diagram_job)
            self.diagram_job = None
        if not self.diagrams_loaded: return
        fitted = self.diagram_scale.get() == '화면에 맞춤'
        # CSS body padding (64) + figure padding/border (26), in physical pixels.
        viewport = self.browser._html.winfo_width()
        if viewport <= 1: viewport = max(self.winfo_width() - 16, 170)
        available = max(80, viewport - 90)
        for identity, info in self.diagrams.items():
            width = min(960, info['width'] // 2, available) if fitted else round(info['width'] / 2 * int(self.diagram_scale.get().rstrip('%')) / 100)
            node = self.browser.document.getElementById('mermaid-' + identity)
            node.setAttribute('width', str(width))
            node.setAttribute('style', f'width:{width}px;max-width:{"100%" if fitted else "none"};height:auto')
            label = self.diagram_widgets[identity]
            if width == getattr(label, '_diagram_width', None): continue
            with Image.open(BytesIO(label._diagram_raw)) as source:
                resized = source.convert('RGB').resize((width, max(1, round(width * info['height'] / info['width']))), Image.Resampling.LANCZOS)
            label._diagram_image = ImageTk.PhotoImage(resized, master=label)
            label.configure(image=label._diagram_image)
            label._diagram_width = width

    def _schedule_diagrams(self, _=None):
        if not self.diagrams_loaded: return
        if self.diagram_job is not None: self.after_cancel(self.diagram_job)
        self.diagram_job = self.after(150, self._scale_diagrams)

    def _mapped(self, event):
        if event.widget is self: self._load_diagrams()

    def _load_diagrams(self):
        if self.diagrams_loaded: return
        self.diagrams_loaded = True
        for identity, info in self.diagrams.items():
            slot = self.browser.document.getElementById('mermaid-slot-' + identity)
            slot.innerHTML = f'<span id="mermaid-{identity}" style="display:block"></span>' + slot.innerHTML
            label = tk.Label(self.browser, background='white', borderwidth=0, padx=0, pady=0)
            label._diagram_raw = self._request_resource(f'{ASSET_PREFIX}{identity}.png')[1]
            self.diagram_widgets[identity] = label
            self.browser.document.getElementById('mermaid-' + identity).widget = label
        self._scale_diagrams()
        # Map can precede the HTML viewport's first geometry allocation.
        self.diagram_job = self.after_idle(self._scale_diagrams)

    def _destroy(self, event):
        if event.widget is self:
            if self.diagram_job is not None:
                self.after_cancel(self.diagram_job); self.diagram_job = None
            for label in self.diagram_widgets.values():
                label._diagram_image = None
                label._diagram_raw = b''
            self.diagram_widgets.clear()
            root = self._root()
            _READERS.discard(self)
            for tag, sequence, script in self.browser_bindings:
                if tag.startswith('.'): continue  # Tk already removed this destroyed widget's bindings.
                # Shared Html wheel bindings may now belong to another reader.
                if root.bind_class(tag, sequence) == script:
                    previous = self.previous_html_bindings.get(sequence, '') if tag == 'Html' else ''
                    commands = re.findall(r'\[([^\s\]]+)', previous)
                    if previous and all(root.tk.call('info', 'commands', command) for command in commands):
                        root.bind_class(tag, sequence, previous)
                    else:
                        root.unbind_class(tag, sequence)
            for command in self.browser_root_commands:
                root.deletecommand(command)
            self.browser_root_commands.clear(); self.browser_bindings.clear()
            self.previous_html_bindings.clear()
            # TkinterWeb's DOM creates two untracked, interpreter-wide Python
            # commands. Keep live readers usable without retaining a closed one.
            replacement = next((reader for reader in _READERS if reader._root() is root), None)
            for name, callback in (('parse_fragment', replacement.browser._html.parse_fragment if replacement else None),
                                   ('node_to_html', replacement.browser.document._node_to_html if replacement else None)):
                if callback is None:
                    if root.tk.call('info', 'commands', name): root.tk.deletecommand(name)
                else:
                    target = weakref.WeakMethod(callback)
                    def dispatch(*args, target=target):
                        method = target()
                        if method is None: raise tk.TclError('문서가 닫혔습니다.')
                        return method(*args)
                    root.tk.createcommand(name, dispatch)
            remove_traces(self.traces)
            if self.search_job is not None:
                self.after_cancel(self.search_job); self.search_job = None
            self.navigate = None
            self.source = self.html_source = ""
            self.headings.clear()
            self.diagrams.clear()
            release_variables(self)
