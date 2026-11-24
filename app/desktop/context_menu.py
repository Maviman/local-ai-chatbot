"""A single, shared right-click menu for a group of widgets (a list's rows, the
chat sidebar's rows), built only when it is actually shown.

Why not one menu per row: a tk.Menu is a real native object (and a "Move"
submenu is a second one), and creating and destroying them is by far the most
expensive thing about drawing a list - measured at about three quarters of the
time to redraw 60 rows (1.7 s against 0.42 s without them), paid on every visit
to the screen, for menus almost nobody opens. One menu whose entries are filled
in at the moment of the click costs nothing to draw.
"""

import tkinter as tk

from . import theme as theme_mod


def populate_menu(menu, items, theme):
    """Fills `menu` from items: (label, callback) for a flat entry, or
    (label, [(sub_label, sub_callback), ...]) for a cascading submenu - e.g.
    a "Move" entry whose submenu lists one command per destination folder,
    instead of flattening every "Move to: X" into the top-level menu.
    Recurses, so a submenu's own entries could nest further, though nothing
    currently needs more than one level."""
    for label, target in items:
        if isinstance(target, list):
            submenu = tk.Menu(
                menu, tearoff=0, bg=theme.panel, fg=theme.text,
                activebackground=theme.bright, activeforeground=theme.bg,
                font=theme_mod.FONT_CAPTION,
            )
            populate_menu(submenu, target, theme)
            menu.add_cascade(label=label, menu=submenu)
        else:
            menu.add_command(label=label, command=target)


class SharedContextMenu:
    """parent: any widget that lives as long as the menu should (it is destroyed
    with it). Use prepare(items) to get the menu filled for one click - the
    entries are rebuilt each time - or popup(items, x, y) to show it."""

    def __init__(self, parent, theme):
        self._parent = parent
        self._theme = theme
        self._menu = None

    def prepare(self, items):
        theme = self._theme
        if self._menu is None:
            self._menu = tk.Menu(
                self._parent, tearoff=0, bg=theme.panel, fg=theme.text,
                activebackground=theme.bright, activeforeground=theme.bg, font=theme_mod.FONT_CAPTION,
            )
        menu = self._menu
        menu.delete(0, "end")
        for submenu in menu.winfo_children():  # the cascades the previous popup created
            submenu.destroy()
        populate_menu(menu, items, theme)
        return menu

    def popup(self, items, x, y):
        self.prepare(items).tk_popup(x, y)
