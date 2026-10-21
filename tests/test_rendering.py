import gc
import tkinter as tk
import unittest
import weakref
from unittest.mock import patch

import customtkinter as ctk
from label_porter import widgets
from label_porter.smooth_surface import rounded_surface
from label_porter.ui import setup_style
from label_porter.gui import App


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.root = widgets.create_root()
        self.root.geometry('640x440')
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        setup_style(self.root)

    def tearDown(self):
        ctk.set_widget_scaling(1)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_continuous_antialiased_border_at_fractional_dpi(self):
        for factor in (1, 1.25, 1.5, 2):
            image = rounded_surface(120 * factor, 32 * factor, 8 * factor, factor,
                                    '#ffffff', '#f1f3f4', '#80868b')
            width, height = image.size
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            # There must be a real outline at every straight-edge pixel, not
            # a gap where corner glyphs meet the border rectangles.
            for x in range(round(10 * factor), width - round(10 * factor)):
                self.assertLess(max(image.getpixel((x, 0))), 210)
                self.assertLess(max(image.getpixel((x, height - 1))), 210)
            corner = image.crop((0, 0, round(10 * factor), round(10 * factor)))
            self.assertGreater(len(set(corner.get_flattened_data())), 8)

    def test_skin_hover_dropdown_click_and_disposal(self):
        button = widgets.Button(self.root, text='실행', style='Primary.TButton'); button.pack()
        combo = widgets.Combobox(self.root, values=['원본', '보완'], state='readonly'); combo.pack()
        self.root.update()
        normal = button._smooth_key
        button._on_enter(); self.assertNotEqual(button._smooth_key, normal)
        button._on_leave(); self.assertEqual(button._smooth_key, normal)
        for factor in (1, 1.25, 1.5):
            ctk.set_widget_scaling(factor); self.root.update()
            self.assertEqual(button._smooth_images[0].width(), button.winfo_width())
            self.assertEqual(button._smooth_images[0].height(), button.winfo_height())
        canvas = combo._canvas
        with patch.object(combo, '_open_dropdown_menu') as opened:
            canvas.event_generate('<Motion>', x=canvas.winfo_width()-12, y=canvas.winfo_height()//2)
            self.root.update()
            canvas.event_generate('<Button-1>', x=canvas.winfo_width()-12, y=canvas.winfo_height()//2)
            self.root.update()
            opened.assert_called_once()
        names = [str(photo) for photo in button._smooth_images + combo._smooth_images]
        refs = [weakref.ref(button), weakref.ref(combo)]
        button.destroy(); combo.destroy(); del button, combo, canvas
        self.root.update(); gc.collect()
        self.assertTrue(all(name not in self.root.tk.call('image', 'names') for name in names))
        self.assertTrue(all(ref() is None for ref in refs))

    def test_wrap_bar_keeps_all_actions_inside_at_each_scale(self):
        bar = widgets.WrapBar(self.root); bar.pack(fill='x')
        for text in ('입력 위치 보기', '데이터 새로 읽기', '중단', '선택 데이터 보기 →'):
            bar.add(widgets.Button(bar, text=text, style='Compact.TButton'))
        for factor in (1, 1.25, 1.5):
            ctk.set_widget_scaling(factor)
            for width in (640, 360, 640):
                self.root.geometry(f'{width}x440'); self.root.update()
                boxes = [(b.winfo_x(), b.winfo_y(), b.winfo_width(), b.winfo_height()) for b in bar.items]
                for x, y, w, h in boxes:
                    self.assertLessEqual(x+w, bar.winfo_width())
                    self.assertLessEqual(y+h, bar.winfo_height())
                for i, (x, y, w, h) in enumerate(boxes):
                    for xx, yy, ww, hh in boxes[i+1:]:
                        self.assertTrue(x+w <= xx or xx+ww <= x or y+h <= yy or yy+hh <= y)

    def test_button_width_is_not_double_scaled_by_tk_points(self):
        original = self.root.tk.call('tk', 'scaling')
        try:
            sizes = []
            for scaling in (1.3333, 2.0):
                self.root.tk.call('tk', 'scaling', scaling)
                button = widgets.Button(self.root, text='데이터 새로 읽기')
                sizes.append(button._desired_width); button.destroy()
            self.assertEqual(sizes[0], sizes[1])
        finally:
            self.root.tk.call('tk', 'scaling', original)

    def test_narrow_footer_keeps_report_folder_and_navigation_visible(self):
        ctk.set_widget_scaling(1.5)
        with patch.object(App, '_load_settings'), patch.object(App, '_save_settings'):
            app = App(self.root)
            self.root.geometry('1040x800'); self.root.update()
            with patch.object(app, '_session_ready', return_value=True):
                app._show_step(app.review_step); self.root.update()
            buttons = (app.report_button, app.folder_button, app.back_button, app.primary_button)
            for button in buttons:
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                     self.root.winfo_rootx() + self.root.winfo_width())
            self.assertLess(app.report_button.winfo_rooty(), app.back_button.winfo_rooty())
