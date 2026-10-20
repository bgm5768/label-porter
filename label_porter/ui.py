"""Shared, dependency-free desktop presentation components."""
import tkinter as tk
from tkinter import ttk

BG = "#f5f6f8"
INK = "#20242c"
MUTED = "#69717e"
BLUE = "#343d4d"


def setup_style(root):
    root.configure(background=BG)
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
    style.map("Toolbar.TButton", background=[("active", "#edf0f4")])
    style.configure("Section.TLabel", foreground=MUTED, font=("맑은 고딕", 9, "bold"))
    style.configure("Heading.TLabel", font=("맑은 고딕", 14, "bold"))
    style.configure("Card.TFrame", background="white")
    style.configure("Card.TLabel", background="white")
    style.configure("CardTitle.TLabel", background="white", font=("맑은 고딕", 12, "bold"))
    style.configure("CardMuted.TLabel", background="white", foreground=MUTED)
    style.configure("TButton", background="#f7f8fa", padding=(14, 9), borderwidth=1,
                    bordercolor="#cbd2dc", lightcolor="#cbd2dc", darkcolor="#cbd2dc", relief="solid", focuscolor="#dbeafe")
    style.map("TButton", background=[("active", "#e8eefb"), ("disabled", "#edf0f5")],
              foreground=[("disabled", "#94a0b1")])
    style.configure("Primary.TButton", background=BLUE, foreground="white", bordercolor=BLUE,
                    font=("맑은 고딕", 10, "bold"), padding=(18, 11))
    style.map("Primary.TButton", background=[("disabled", "#e4e7eb"), ("active", "#202630")],
              foreground=[("disabled", "#7f8da2"), ("!disabled", "white")])
    style.configure("Nav.TButton", font=("맑은 고딕", 10, "bold"), padding=(14, 11))
    style.configure("NavPrimary.TButton", background=BLUE, foreground="white", bordercolor=BLUE,
                    font=("맑은 고딕", 10, "bold"), padding=(14, 11))
    style.map("NavPrimary.TButton", background=[("disabled", "#e4e7eb"), ("active", "#202630")],
              foreground=[("disabled", "#7f8da2"), ("!disabled", "white")])
    style.configure("TEntry", padding=8, fieldbackground="white", bordercolor="#dce3ee")
    style.configure("TCombobox", padding=6, fieldbackground="white", bordercolor="#dce3ee")
    style.configure("TLabelframe", background=BG, bordercolor="#dce3ee", relief="solid")
    style.configure("TLabelframe.Label", foreground=INK, font=("맑은 고딕", 10, "bold"))
    style.configure("Treeview", background="white", fieldbackground="white", rowheight=32,
                    borderwidth=0, font=("맑은 고딕", 10))
    style.configure("Treeview.Heading", background="#eaf0f8", padding=7, relief="flat")
    style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", INK)])
    style.configure("Flow.TNotebook", borderwidth=0, tabmargins=0, bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.layout("Flow.TNotebook.Tab", [])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background="#e7ecf4", padding=(14, 9), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
    style.configure("Vertical.TScrollbar", background="#c4cedc", troughcolor=BG, borderwidth=0, width=10)
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e5eaf3", borderwidth=0)
    return style


class ScrollPage(ttk.Frame):
    """Only the content scrolls; navigation remains outside this component."""
    def __init__(self, parent, padding=0):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._resize)
        # A widget-specific bindtag avoids stealing scrolling from text/tree/canvas views.
        self.tag = "ScrollPage" + str(id(self))
        self.bind_class(self.tag, "<MouseWheel>", self._wheel)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget is self:
            self.unbind_class(self.tag, "<MouseWheel>")

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
    frame = ttk.Frame(parent, style="Card.TFrame", padding=20)
    frame.pack(fill="x", pady=(0, 14))
    ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor="w")
    if description:
        label = ttk.Label(frame, text=description, style="CardMuted.TLabel", wraplength=750, justify="left")
        label.pack(fill="x", pady=(6, 12))
        frame.bind("<Configure>", lambda event: label.configure(wraplength=max(200, event.width - 40)), add="+")
    return frame
