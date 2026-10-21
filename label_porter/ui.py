"""Shared desktop surfaces, typography and scroll layout."""
import tkinter as tk
from tkinter import ttk
from . import widgets

BG, INK, MUTED, BLUE = widgets.BG, widgets.INK, widgets.MUTED, widgets.ACCENT


def setup_style(root):
    root.configure(background=BG)
    root.option_add('*Text.background', '#ffffff')
    root.option_add('*Text.foreground', INK)
    root.option_add('*Text.selectBackground', widgets.SELECTED)
    root.option_add('*Text.selectForeground', INK)
    root.option_add('*Text.insertBackground', BLUE)
    root.option_add('*Text.padX', 12)
    root.option_add('*Text.padY', 10)
    root.option_add('*Listbox.font', '{맑은 고딕} 10')
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=INK, font=("맑은 고딕", 10))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Title.TLabel", font=("맑은 고딕", 21, "bold"))
    style.configure("Toolbar.TFrame", background="#ffffff")
    style.configure("Toolbar.TLabel", background="#ffffff", foreground=MUTED)
    style.configure("Toolbar.TButton", padding=(12, 7), background="#ffffff", borderwidth=0, relief="flat")
    style.map("Toolbar.TButton", background=[("active", widgets.HOVER)])
    style.configure("Section.TLabel", foreground=MUTED, font=("맑은 고딕", 9, "bold"))
    style.configure("Heading.TLabel", font=("맑은 고딕", 14, "bold"))
    style.configure("Card.TFrame", background="white")
    style.configure("Card.TLabel", background="white")
    style.configure("CardTitle.TLabel", background="white", font=("맑은 고딕", 12, "bold"))
    style.configure("CardMuted.TLabel", background="white", foreground=MUTED)
    style.configure("TButton", background=widgets.SECONDARY, padding=(14, 9), borderwidth=1,
                    bordercolor=widgets.BORDER, lightcolor=widgets.BORDER, darkcolor=widgets.BORDER, relief="solid", focuscolor=widgets.SELECTED)
    style.map("TButton", background=[("active", widgets.HOVER), ("disabled", widgets.DISABLED)],
              foreground=[("disabled", widgets.DISABLED_INK)])
    style.configure("Primary.TButton", background=BLUE, foreground="white", bordercolor=BLUE,
                    font=("맑은 고딕", 10, "bold"), padding=(18, 11))
    style.map("Primary.TButton", background=[("disabled", widgets.DISABLED), ("active", widgets.ACCENT_HOVER)],
              foreground=[("disabled", widgets.DISABLED_INK), ("!disabled", "white")])
    style.configure("Nav.TButton", font=("맑은 고딕", 10, "bold"), padding=(14, 11))
    style.configure("NavPrimary.TButton", background=BLUE, foreground="white", bordercolor=BLUE,
                    font=("맑은 고딕", 10, "bold"), padding=(14, 11))
    style.map("NavPrimary.TButton", background=[("disabled", widgets.DISABLED), ("active", widgets.ACCENT_HOVER)],
              foreground=[("disabled", widgets.DISABLED_INK), ("!disabled", "white")])
    style.configure("TEntry", padding=8, fieldbackground="white", bordercolor=widgets.BORDER)
    style.configure("Compact.TButton", padding=(8, 4))
    style.configure("Compact.TEntry", padding=4)
    style.configure("Compact.TCombobox", padding=4)
    style.configure("TCombobox", padding=6, fieldbackground="white", bordercolor=widgets.BORDER)
    style.configure("TLabelframe", background=BG, bordercolor=widgets.BORDER, relief="solid")
    style.configure("TLabelframe.Label", foreground=INK, font=("맑은 고딕", 10, "bold"))
    style.configure("Treeview", background="white", fieldbackground="white", foreground=INK, rowheight=36,
                    borderwidth=0, font=("맑은 고딕", 10))
    style.configure("Treeview.Heading", background=widgets.HOVER, foreground=INK, padding=9, relief="flat", font=("맑은 고딕", 10, "bold"))
    style.map("Treeview.Heading", background=[("active", widgets.CARD_BORDER)], foreground=[("active", INK)])
    style.map("Treeview", background=[("selected", widgets.SELECTED)], foreground=[("selected", INK)])
    style.configure("Flow.TNotebook", borderwidth=0, tabmargins=0, bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.layout("Flow.TNotebook.Tab", [])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=widgets.SECONDARY, padding=(14, 9), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
    style.configure("Vertical.TScrollbar", background=widgets.BORDER, troughcolor=BG, borderwidth=0, width=10)
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor=widgets.CARD_BORDER, borderwidth=0)
    return style


class ScrollPage(ttk.Frame):
    """Only the content scrolls; navigation remains outside this component."""
    def __init__(self, parent, padding=0):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0)
        self.scrollbar = widgets.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._resize)
        # A widget-specific bindtag avoids stealing scrolling from text/tree/canvas views.
        self.tag = "ScrollPage" + str(id(self))
        self.wheel_command = self.bind_class(self.tag, "<MouseWheel>", self._wheel)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget is self:
            self.unbind_class(self.tag, "<MouseWheel>")
            # bind_class registers its Python callback on the root, so destroying
            # this frame (or only removing the binding script) does not free it.
            if self.wheel_command is not None:
                self._root().deletecommand(self.wheel_command)
                self.wheel_command = None

    def _content_changed(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        def bind_children(widget):
            if not isinstance(widget, (tk.Text, tk.Listbox, tk.Canvas, ttk.Treeview, ttk.Combobox)):
                if self.tag not in widget.bindtags():
                    widget.bindtags((*widget.bindtags()[:-1], self.tag, widget.bindtags()[-1]))
            for child in widget.winfo_children():
                bind_children(child)
        bind_children(self.body)

    def _resize(self, event):
        self.canvas.itemconfigure(self.item, width=event.width)

    def _wheel(self, event):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


def card(parent, title, description=None):
    frame = widgets.Card(parent, padding=20)
    frame.pack(fill="x", pady=(0, 14))
    ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor="w")
    if description:
        label = ttk.Label(frame, text=description, style="CardMuted.TLabel", wraplength=750, justify="left")
        label.pack(fill="x", pady=(6, 12))
        frame.bind("<Configure>", lambda event: label.winfo_exists() and label.configure(wraplength=max(200, event.width - 40)), add="+")
    return frame


def card_content(parent):
    """Keep native content surfaces consistent with their enclosing white card."""
    pending, seen = list(parent.children.values()), set()
    while pending:
        child = pending.pop()
        if id(child) in seen: continue
        seen.add(id(child))
        if isinstance(child, ttk.Frame): child.configure(style="Card.TFrame")
        elif isinstance(child, ttk.Label): child.configure(background=widgets.SURFACE)
        pending.extend(child.children.values())
