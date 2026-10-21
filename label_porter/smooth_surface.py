"""Antialiased CTk surfaces without font-glyph seams; no global Tk patches.

Keep CTk layout, text, events and state handling. Only replace its canvas skin.
Nine-slice expansion keeps rounded-card rendering inexpensive at large sizes.
"""
import math

from PIL import Image, ImageDraw, ImageTk


def rounded_surface(width, height, radius, border, background, fill, outline):
    width, height = max(1, round(width)), max(1, round(height))
    radius = min(radius, width / 2, height / 2)
    edge = math.ceil(max(radius, border)) + 2
    pw, ph = min(width, 2 * edge + 1), min(height, 2 * edge + 1)
    scale = 4
    patch = Image.new('RGB', (pw * scale, ph * scale), background)
    draw = ImageDraw.Draw(patch)
    draw.rounded_rectangle((0, 0, pw * scale - 1, ph * scale - 1), radius * scale,
                           fill=outline if border else fill)
    if border and pw > 2 * border and ph > 2 * border:
        b = border * scale
        draw.rounded_rectangle((b, b, pw * scale - b - 1, ph * scale - b - 1),
                               max(0, radius - border) * scale, fill=fill)
    patch = patch.resize((pw, ph), Image.Resampling.LANCZOS)
    if width > pw:
        stretched = Image.new('RGB', (width, ph))
        stretched.paste(patch.crop((0, 0, edge, ph)), (0, 0))
        stretched.paste(patch.crop((edge, 0, edge + 1, ph)).resize((width - 2 * edge, ph)), (edge, 0))
        stretched.paste(patch.crop((pw - edge, 0, pw, ph)), (width - edge, 0))
        patch = stretched
    if height > ph:
        stretched = Image.new('RGB', (width, height))
        stretched.paste(patch.crop((0, 0, width, edge)), (0, 0))
        stretched.paste(patch.crop((0, edge, width, edge + 1)).resize((width, height - 2 * edge)), (0, edge))
        stretched.paste(patch.crop((0, ph - edge, width, ph)), (0, height - edge))
        patch = stretched
    return patch


class SmoothSurface:
    def _draw(self, no_color_updates=False):
        super()._draw(no_color_updates)
        self._smooth_paint()

    def _on_enter(self, event=None):
        super()._on_enter(event)
        self._smooth_paint()

    def _on_leave(self, event=None):
        super()._on_leave(event)
        self._smooth_paint()

    def _smooth_paint(self):
        canvas = self._canvas
        split = hasattr(self, '_button_color')
        width = max(1, round(self._apply_widget_scaling(self._current_width)))
        height = max(1, round(self._apply_widget_scaling(self._current_height)))
        radius = self._apply_widget_scaling(self._corner_radius)
        border = self._apply_widget_scaling(self._border_width)
        bg = self._apply_appearance_mode(self._bg_color)
        # Read CTk's current colors, including hover, disabled and focus states.
        fill = canvas.itemcget('inner_parts_left' if split else 'inner_parts', 'fill')
        outline = canvas.itemcget('border_parts_left' if split else 'border_parts', 'fill')
        fill = fill or self._apply_appearance_mode(self._fg_color)
        if fill == 'transparent': fill = bg
        outline = outline or fill
        right = canvas.itemcget('inner_parts_right', 'fill') if split else None
        arrow = self._apply_appearance_mode(self._text_color_disabled if getattr(self, '_state', '') == 'disabled' else self._text_color) if split else None
        key = (width, height, radius, border, bg, fill, outline, right, arrow)
        if key != getattr(self, '_smooth_key', None):
            def rgb(color):
                return tuple(channel // 257 for channel in self.winfo_rgb(color))
            bg, fill, outline = rgb(bg), rgb(fill), rgb(outline)
            skin = rounded_surface(width, height, radius, border, bg, fill, outline)
            if split:
                right, arrow = rgb(right), rgb(arrow)
                divider = max(0, width - height)
                right_skin = rounded_surface(width, height, radius, border, bg, right, outline)
                skin.paste(right_skin.crop((divider, 0, width, height)), (divider, 0))
                icon_size = max(8, round(height / 3))
                icon = Image.new('RGBA', (icon_size * 4, icon_size * 4))
                d = ImageDraw.Draw(icon)
                d.line([(icon_size, icon_size * 1.5), (icon_size * 2, icon_size * 2.5),
                        (icon_size * 3, icon_size * 1.5)], fill=arrow, width=max(4, round(height / 6)), joint='curve')
                icon = icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
                skin.paste(icon, (width - (height + icon_size) // 2, (height - icon_size) // 2), icon)
                pieces = [(skin.crop((0, 0, divider, height)), 0, 'smooth_left', ()),
                          (skin.crop((divider, 0, width, height)), divider, 'smooth_right', ('right_parts',))]
            else:
                pieces = [(skin, 0, 'smooth_surface', ())]
            photos = []
            for piece, x, tag, event_tags in pieces:
                if piece.width < 1: continue
                photo = ImageTk.PhotoImage(piece, master=canvas)
                photos.append(photo)
                if canvas.find_withtag(tag):
                    canvas.itemconfigure(tag, image=photo)
                    canvas.coords(tag, x, 0)
                else:
                    canvas.create_image(x, 0, anchor='nw', image=photo, tags=(tag, 'smooth_skin', *event_tags))
            self._smooth_images = photos  # Only the current skin is retained.
            self._smooth_key = key
        for tag in ('border_parts', 'inner_parts', 'dropdown_arrow'):
            canvas.itemconfigure(tag, state='hidden')
        canvas.tag_raise('smooth_skin')

    def destroy(self):
        self._smooth_images = []
        self._smooth_key = None
        super().destroy()
