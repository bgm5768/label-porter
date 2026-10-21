"""Real CTk controls preserve navigation, variable ownership and data semantics."""
import gc
import tkinter as tk
import unittest
import weakref
from unittest.mock import patch

import customtkinter as ctk
from label_porter import widgets
from label_porter.gui import App
from label_porter.ui import setup_style, card


class CustomWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root = widgets.create_root()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        setup_style(self.root)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_button_keyboard_activation_and_disabled_state(self):
        calls = []
        button = widgets.Button(self.root, text='다음 단계', command=lambda: calls.append(1), style='Primary.TButton')
        button.pack(); self.root.deiconify(); self.root.update()
        button.focus_force(); button.event_generate('<Return>'); self.root.update()
        self.assertEqual(calls, [1])
        button.configure(state='disabled'); button.event_generate('<space>'); button.invoke()
        self.assertEqual(calls, [1])
        self.assertEqual(button.cget('fg_color'), widgets.DISABLED)
        button.configure(state='normal')
        self.assertEqual(button.cget('fg_color'), widgets.ACCENT)

    def test_combobox_keyboard_updates_shared_variable_and_selection_event(self):
        value = tk.StringVar(value='전체')
        selected = []
        combo = widgets.Combobox(self.root, textvariable=value, values=['전체', '연결됨', '짝 없음'], state='readonly')
        combo.pack(); combo.bind('<<ComboboxSelected>>', lambda _: selected.append(value.get()))
        self.root.deiconify(); self.root.update()
        combo._entry.focus_force(); combo._entry.event_generate('<Down>'); self.root.update()
        self.assertEqual(value.get(), '연결됨'); self.assertEqual(selected, ['연결됨'])
        self.assertEqual(combo.current(), 1)
        combo.configure(values=['전체', '새 상태']); combo.current(1)
        self.assertEqual(value.get(), '새 상태')

    def test_unchanged_polling_state_does_not_redraw_buttons(self):
        button = widgets.Button(self.root, text='다음 단계', style='Primary.TButton')
        with patch.object(button, '_draw', wraps=button._draw) as draw:
            for _ in range(100): button.configure(state='normal', text='다음 단계')
            self.assertEqual(draw.call_count, 0)
            button.configure(state='disabled')
            self.assertGreater(draw.call_count, 0)

    def test_entry_and_dropdown_release_callbacks_without_removing_external_trace(self):
        variable = tk.StringVar()
        external = variable.trace_add('write', lambda *_: None)
        parent = widgets.Frame(self.root); parent.pack()
        entry = widgets.Entry(parent, textvariable=variable)
        combo = widgets.Combobox(parent, values=['한글'])
        refs = [weakref.ref(entry), weakref.ref(combo)]
        parent.destroy(); del entry, combo, parent
        self.root.update(); gc.collect()
        self.assertEqual([r() for r in refs], [None, None])
        self.assertEqual([t[1] for t in variable.trace_info()], [external])
        variable.trace_remove('write', external)

    def test_card_background_covers_padding_and_tab_header_tracks_lifetime(self):
        panel = card(self.root, '규칙', '설명'); self.root.deiconify(); self.root.update()
        self.assertEqual(panel._canvas.winfo_width(), panel.winfo_width())
        self.assertEqual(panel._canvas.winfo_x(), 0)
        tabs = widgets.Notebook(self.root); tabs.pack(fill='both', expand=True)
        for title in ('연결', 'JSON'): tabs.add(widgets.Frame(tabs), text=title)
        self.root.update(); tabs.buttons[tabs.tabs()[1]].invoke(); self.root.update()
        self.assertEqual(tabs.index(tabs.select()), 1)
        tabs.pack_forget(); self.root.update(); self.assertFalse(tabs.tabbar.winfo_ismapped())
        tabs.pack(fill='both', expand=True); self.root.update(); self.assertTrue(tabs.tabbar.winfo_ismapped())
        bar = weakref.ref(tabs.tabbar); tabs.destroy(); del tabs; gc.collect(); self.assertIsNone(bar())

    def test_actual_ctk_app_keeps_workflow_actions_and_help_inside_window(self):
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root)
            self.root.geometry('1040x680'); self.root.deiconify(); self.root.update()
            self.assertIsInstance(app.primary_button, ctk.CTkButton)
            self.assertIsInstance(app.source_panel.buttons[0], ctk.CTkButton)
            self.assertIsInstance(app.ai_guide_box.search_entry, ctk.CTkEntry)
            for index in range(4):
                app._show_tools(index); self.root.update()
                self.assertTrue(app.back_button.winfo_ismapped())
                self.assertLessEqual(app.back_button.winfo_rootx() + app.back_button.winfo_width(),
                                     self.root.winfo_rootx() + self.root.winfo_width())
            app.back_button.invoke(); self.root.update()
            self.assertEqual(app.route, 'workflow')
            self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))

    def test_initial_window_geometry_accounts_for_dpi(self):
        with patch.object(self.root, '_get_window_scaling', return_value=1.5), \
                patch.object(self.root, 'winfo_screenwidth', return_value=1920), \
                patch.object(self.root, 'winfo_screenheight', return_value=1080):
            width, height = widgets.window_size(self.root, (1320, 900))
            self.assertLess(width * 1.5, 1920)
            self.assertLess(height * 1.5, 1080)
