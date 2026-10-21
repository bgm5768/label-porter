"""CustomTkinter controls with the small ttk interface used by our screens.

Structural containers and virtualized tables remain native Tk widgets. This keeps
their geometry, keyboard navigation and large-data behavior intact; no global
monkey-patching of tkinter or processing code is involved.
"""
import tkinter as tk
from tkinter import ttk, font as tkfont

import customtkinter as ctk
from .smooth_surface import SmoothSurface

BG = '#f8f9fa'
SURFACE = '#ffffff'
INK = '#1f1f1f'
MUTED = '#444746'
ACCENT = '#0b57d0'
ACCENT_HOVER = '#0842a0'
BORDER = '#80868b'  # Interactive boundaries, distinct from decorative cards.
CARD_BORDER = '#dadce0'
SECONDARY = '#f1f3f4'
HOVER = '#e8eaed'
SELECTED = '#d3e3fd'
SELECTED_INK = '#174ea6'
TAB_ACTIVE = '#202124'
DISABLED = '#e8eaed'
DISABLED_INK = '#5f6368'
ctk.set_appearance_mode('light')
ctk.set_default_color_theme('blue')

# Keep native, efficient geometry containers and keyboard-accessible data tables.
Frame, Label, Treeview = ttk.Frame, ttk.Label, ttk.Treeview
Panedwindow, Separator, Spinbox = ttk.Panedwindow, ttk.Separator, ttk.Spinbox


def _font(value=None, bold=False):
    if value is None:
        return ('맑은 고딕', 13, 'bold' if bold else 'normal')
    if isinstance(value, tuple):
        return (value[0], round(value[1] * 4 / 3) if value[1] > 0 else -value[1], *value[2:])
    return value


class _Control:
    """Preserve option access and explicitly own short-lived animation callbacks."""
    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def after(self, ms, func=None, *args):
        if func is None:
            return super().after(ms)
        if not hasattr(self, '_owned_jobs'):
            self._owned_jobs = set()
        job = None
        def run():
            self._owned_jobs.discard(job)
            func(*args)
        job = super().after(ms, run)
        self._owned_jobs.add(job)
        return job

    def destroy(self):
        for job in getattr(self, '_owned_jobs', ()):
            self.after_cancel(job)
        getattr(self, '_owned_jobs', set()).clear()
        root = self._root()
        super().destroy()
        _prune_trackers(root)


def _prune_trackers(root):
    """CTk 5.2 retains empty window registries; release closed viewer windows."""
    scaling = ctk.ScalingTracker
    windows = [root, *scaling.window_widgets_dict]
    for window, callbacks in list(scaling.window_widgets_dict.items()):
        try:
            exists = window.winfo_exists()
        except tk.TclError:
            exists = False
        if not callbacks or not exists:
            scaling.window_widgets_dict.pop(window, None)
            scaling.window_dpi_scaling_dict.pop(window, None)
    appearance = ctk.AppearanceModeTracker
    if not appearance.callback_list:
        appearance.app_list.clear()
        appearance.update_loop_running = False
    if not scaling.window_widgets_dict:
        scaling.update_loop_running = False
    # Stop only CustomTkinter's known polling commands when their owners are gone.
    try:
        jobs = root.tk.call('after', 'info')
    except tk.TclError:
        return
    for job in jobs:
        script = str(root.tk.call('after', 'info', job)[0])
        command = script.split()[0]
        if ((not appearance.callback_list and command.endswith('update')) or
                (not scaling.window_widgets_dict and command.endswith('check_dpi_scaling'))):
            owner = next((window for window in windows if command in (window._tclCommands or [])), None)
            if owner is not None:
                owner.after_cancel(job)
            else:
                root.tk.call('after', 'cancel', job)


class Button(_Control, SmoothSurface, ctk.CTkButton):
    def __init__(self, master, **kwargs):
        self.style_name = kwargs.pop('style', '')
        width = kwargs.pop('width', None)
        text = kwargs.get('text', '')
        compact = 'Compact' in self.style_name
        primary = 'Primary' in self.style_name
        self._normal_fill = ACCENT if primary else SECONDARY
        font = _font(kwargs.pop('font', None), primary)
        # Measure logical pixels. Positive Tk points already include monitor DPI;
        # CTk would scale that measured width a second time on 125/150% displays.
        measured = tkfont.Font(master, family=font[0], size=-abs(font[1]),
                               weight='bold' if 'bold' in font[2:] else 'normal').measure(text)
        pixels = (width * 8 + 24) if width is not None else max(88, measured + 32)
        super().__init__(master, width=pixels, height=30 if compact else 40,
                         corner_radius=8, border_width=0 if primary else 1,
                         border_color=BORDER, fg_color=self._normal_fill,
                         hover_color=ACCENT_HOVER if primary else HOVER,
                         text_color='white' if primary else INK,
                         text_color_disabled=DISABLED_INK,
                         font=font, **kwargs)
        # CTk buttons are canvas based: supply keyboard focus and activation.
        tk.Frame.configure(self, takefocus=1)
        tk.Frame.bind(self, '<Return>', lambda _: self.invoke(), add='+')
        tk.Frame.bind(self, '<space>', lambda _: self.invoke(), add='+')
        tk.Frame.bind(self, '<FocusIn>', self._focus_in, add='+')
        tk.Frame.bind(self, '<FocusOut>', self._focus_out, add='+')
        self.configure(state=kwargs.get('state', 'normal'))

    def _focus_in(self, _):
        self._unfocused_border = (self.cget('border_width'), self.cget('border_color'))
        self.configure(border_width=2, border_color=ACCENT)

    def _focus_out(self, _):
        width, color = getattr(self, '_unfocused_border', (1, BORDER))
        self.configure(border_width=width, border_color=color)

    # CTkButton forwards focus to its text label, which would bypass our
    # accessible frame-level keyboard bindings.
    def focus_set(self):
        tk.Misc.focus_set(self)

    focus = focus_set

    def focus_force(self):
        tk.Misc.focus_force(self)

    def configure(self, **kwargs):
        if 'font' in kwargs: kwargs['font'] = _font(kwargs['font'])
        if 'bg' in kwargs: kwargs['fg_color'] = kwargs.pop('bg')
        if 'fg' in kwargs: kwargs['text_color'] = kwargs.pop('fg')
        if 'fg_color' in kwargs: self._normal_fill = kwargs['fg_color']
        if 'state' in kwargs or 'fg_color' in kwargs:
            state = kwargs.get('state', getattr(self, '_state', 'normal'))
            kwargs['fg_color'] = DISABLED if state == 'disabled' else self._normal_fill
        if hasattr(self, '_draw_engine'):
            # The workflow synchronizes actions while polling background work.
            # CTk redraws even identical settings; don't redraw every button on
            # every poll when its visible state hasn't actually changed.
            kwargs = {key: value for key, value in kwargs.items() if self.cget(key) != value}
        if not kwargs: return None
        return super().configure(**kwargs)


class Entry(_Control, SmoothSurface, ctk.CTkEntry):
    def __init__(self, master, **kwargs):
        style = kwargs.pop('style', '')
        width = kwargs.pop('width', 20)
        super().__init__(master, width=width * 8 + 20, height=32 if 'Compact' in style else 40,
                         corner_radius=7, border_width=1, border_color=BORDER,
                         fg_color=SURFACE, text_color=INK,
                         font=_font(kwargs.pop('font', None)), **kwargs)
        self.bind('<FocusIn>', lambda _: self.configure(border_color=ACCENT))
        self.bind('<FocusOut>', lambda _: self.configure(border_color=BORDER))

    def destroy(self):
        # CTkEntry 5.2.2 registers a variable trace but does not remove it.
        if isinstance(self._textvariable, tk.Variable) and self._textvariable_callback_name:
            self._textvariable.trace_remove('write', self._textvariable_callback_name)
            self._textvariable_callback_name = None
        super().destroy()


class Combobox(_Control, SmoothSurface, ctk.CTkComboBox):
    def __init__(self, master, **kwargs):
        style = kwargs.pop('style', '')
        width = kwargs.pop('width', 20)
        if 'textvariable' in kwargs: kwargs['variable'] = kwargs.pop('textvariable')
        kwargs['values'] = list(kwargs.get('values', ()))
        super().__init__(master, width=width * 8 + 34, height=32 if 'Compact' in style else 40,
                         corner_radius=7, border_width=1, border_color=BORDER,
                         fg_color=SURFACE, text_color=INK, button_color=SECONDARY,
                         button_hover_color=HOVER, dropdown_fg_color=SURFACE,
                         dropdown_text_color=INK, dropdown_hover_color=SELECTED,
                         font=_font(), dropdown_font=_font(),
                         command=lambda _: self.event_generate('<<ComboboxSelected>>'), **kwargs)
        # CustomTkinter defaults to the first value; ttk starts with no selection.
        if 'variable' not in kwargs: self.set('')
        self.bind('<Down>', lambda _: self._move_selection(1))
        self.bind('<Up>', lambda _: self._move_selection(-1))
        self.bind('<Alt-Down>', lambda _: self._open_dropdown_menu())

    def _move_selection(self, offset):
        values = self.cget('values')
        if values and self.cget('state') != 'disabled':
            self.current(max(0, min(len(values) - 1, self.current() + offset)))
            self.event_generate('<<ComboboxSelected>>')
        return 'break'

    def bind(self, sequence=None, func=None, add='+'):
        if sequence and sequence.startswith('<<'):
            return tk.Frame.bind(self, sequence, func, add)
        return super().bind(sequence, func, add=True)

    def configure(self, **kwargs):
        if 'values' in kwargs: kwargs['values'] = list(kwargs['values'])
        return super().configure(**kwargs)

    def current(self, index=None):
        values = self.cget('values')
        if index is not None:
            self.set(values[index]); return None
        try: return values.index(self.get())
        except ValueError: return -1

    def destroy(self):
        # DropdownMenu.destroy in CTk 5.2.2 omits its scaling deregistration.
        menu = self._dropdown_menu
        ctk.ScalingTracker.remove_widget(menu._set_scaling, menu)
        super().destroy()


class Checkbutton(_Control, ctk.CTkCheckBox):
    def __init__(self, master, **kwargs):
        kwargs.pop('style', None)
        super().__init__(master, font=_font(), height=30, checkbox_width=20, checkbox_height=20,
                         corner_radius=5, border_width=2, border_color=BORDER,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=INK, **kwargs)
        tk.Frame.configure(self, takefocus=1)
        tk.Frame.bind(self, '<space>', lambda _: self.toggle(), add='+')
        self._text_label.configure(wraplength=680, justify='left')


class Scrollbar(_Control, ctk.CTkScrollbar):
    def __init__(self, master, orient='vertical', **kwargs):
        super().__init__(master, orientation=orient, width=12 if orient == 'vertical' else 16,
                         height=12 if orient == 'horizontal' else 16,
                         fg_color='transparent', button_color=BORDER, button_hover_color=DISABLED_INK, **kwargs)


class Progressbar(_Control, ctk.CTkProgressBar):
    def __init__(self, master, **kwargs):
        self.value = 0
        super().__init__(master, height=7, corner_radius=4, fg_color=CARD_BORDER, progress_color=ACCENT, **kwargs)
        self.set(0)

    def configure(self, **kwargs):
        if 'value' in kwargs:
            self.value = kwargs.pop('value'); self.set(self.value / 100)
        return super().configure(**kwargs)

    def cget(self, key):
        return self.value if key == 'value' else super().cget(key)


class WrapBar(ttk.Frame):
    """Keep actions at their requested size; wrap whole controls on narrow views."""
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.items = []
        self._layout_job = None
        self.bind('<Configure>', self._schedule_layout, add='+')
        self.bind('<Destroy>', self._cleanup_layout, add='+')

    def add(self, widget):
        self.items.append(widget)
        tk.Place.place_configure(widget, x=0, y=0)
        self.configure(height=max(self.winfo_reqheight(), widget.winfo_reqheight() + 6))
        widget.bind('<Configure>', self._schedule_layout, add='+')
        self._schedule_layout()

    def _schedule_layout(self, _=None):
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._reflow)

    def _reflow(self):
        self._layout_job = None
        if not self.winfo_exists(): return
        width = self.winfo_width()
        if width <= 1: return
        used = y = row_height = 0
        for widget in self.items:
            needed = widget.winfo_reqwidth() + 8
            if used and used + needed > width:
                y += row_height + 6
                used = row_height = 0
            info = widget.place_info()
            if int(info.get('x', -1)) != used or int(info.get('y', -1)) != y:
                # Physical Tk coordinates: don't apply CTk DPI scaling twice.
                tk.Place.place_configure(widget, x=used, y=y)
            used += needed
            row_height = max(row_height, widget.winfo_reqheight())
        height = y + row_height + 6
        if self.winfo_reqheight() != height: self.configure(height=height)

    def _cleanup_layout(self, event):
        if event.widget is self:
            if self._layout_job is not None: self.after_cancel(self._layout_job)
            self._layout_job = None
            self.items.clear()


class Notebook(ttk.Notebook):
    """Native tab lifecycle with CTk tab buttons; preserves existing tab APIs."""
    def __init__(self, master, **kwargs):
        self.flow = kwargs.pop('style', '') == 'Flow.TNotebook'
        super().__init__(master, style='Flow.TNotebook', **kwargs)
        self.tabbar = None if self.flow else WrapBar(master, padding=(0, 0, 0, 2))
        self.buttons = {}
        self.bind('<<NotebookTabChanged>>', self._refresh, add='+')

    def add(self, child, **kwargs):
        super().add(child, **kwargs)
        if self.tabbar is not None:
            key = str(child)
            button = Button(self.tabbar, text=kwargs.get('text', ''), style='Compact.TButton',
                            command=lambda: self.select(key))
            self.tabbar.add(button)
            self.buttons[key] = button
            self._refresh()

    def _refresh(self, _=None):
        selected = self.select()
        for key, button in self.buttons.items():
            active = key == selected
            button.configure(fg_color=TAB_ACTIVE if active else SECONDARY,
                             text_color='white' if active else INK,
                             border_color=TAB_ACTIVE if active else BORDER,
                             hover_color=MUTED if active else HOVER,
                             state='disabled' if self.tab(key, 'state') == 'disabled' else 'normal')

    def pack(self, **kwargs):
        if self.tabbar is not None:
            self.tabbar.pack(side=kwargs.get('side', 'top'), fill='x')
        super().pack(**kwargs)

    def pack_forget(self):
        if self.tabbar is not None: self.tabbar.pack_forget()
        super().pack_forget()

    def destroy(self):
        if self.tabbar is not None: self.tabbar.destroy()
        self.buttons.clear()
        super().destroy()


class Card(_Control, SmoothSurface, ctk.CTkFrame):
    def __init__(self, master, padding=20):
        super().__init__(master, fg_color=SURFACE, border_color=CARD_BORDER, border_width=1,
                         corner_radius=12)
        tk.Frame.configure(self, padx=padding, pady=padding)
        # Native frame padding also insets placed children, including CTk's
        # background canvas. Extend it back to the outer edge of the card.
        self._canvas.place(x=-padding, y=-padding, relwidth=1, relheight=1,
                           width=padding * 2, height=padding * 2)

    def bind(self, sequence=None, func=None, add='+'):
        return tk.Frame.bind(self, sequence, func, add)


class Root(_Control, ctk.CTk):
    pass


class Window(_Control, ctk.CTkToplevel):
    pass


def create_root():
    return Root(fg_color=BG)


def create_window(parent):
    return Window(parent, fg_color=BG)


def window_size(window, preferred):
    """CTk geometry is logical pixels; screen dimensions are physical pixels."""
    scaling = window._get_window_scaling() if isinstance(window, (ctk.CTk, ctk.CTkToplevel)) else 1
    return (min(preferred[0], max(400, round(window.winfo_screenwidth() / scaling) - 60)),
            min(preferred[1], max(360, round(window.winfo_screenheight() / scaling) - 100)))
