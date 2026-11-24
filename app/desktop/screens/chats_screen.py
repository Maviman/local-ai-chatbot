"""Chats: search line, folder filter chips ([All] [folder...] [+]), and a
plain bordered-row list of sessions - deliberately bare, no meter, no
action rail (that's the Chat screen's job; the Chat screen has its own
persistent quick-switch sidebar too, but this screen is still where
folder management and search live). Feeds ListScreen. Folder/conversation
data logic ported from the old sidebar.py against the same already-verified
backend calls - project conversations never appear here regardless of
folder filter, unchanged.

"All" replaces the old default ("General"/unfiled-only) view: it now shows
every non-project conversation regardless of folder, matching the approved
mockup's explicit "All" chip; each named-folder chip narrows to just that
folder, same filtering semantics as before.

A "New chat" action button (same outlined-mnemonic pattern as Projects'
action_row) creates straight into whichever folder chip is currently
selected. Previously this screen had no chat-creation affordance at all -
the only way to start a chat was the global Ctrl+N, which always created
an unfiled conversation regardless of which folder you were browsing,
leaving no way to land a new chat inside a folder from here.

Folder chips carry a right-click/Shift-F10/App-key context menu (Rename
folder/Delete folder) via ListScreen.set_chips' context_items - the server
and api_client already had full folder CRUD (list/create/update/delete),
but nothing in the UI ever triggered rename or delete before this. Deleting
a folder only unparents its conversations (server-side folder_id=NULL),
never deletes them.
"""

import tkinter as tk

from .. import theme as theme_mod
from ..async_call import Latest, run_async
from .base import (
    Screen, clear_prompt_slot, make_header, make_prompt_slot, show_inline_confirm, show_inline_prompt, show_toast,
)
from .list_screen import ListRow, ListScreen, format_tail

try:  # B5: the header's text action and the folder-name elision come from the kit (guarded)
    from ..kit.widgets import TextAction as _TextAction
    from ..kit.widgets import ELLIPSIS as _ELLIPSIS, elide_middle as _elide_middle, one_line as _one_line
except Exception:  # noqa: BLE001
    _TextAction = None
    _ELLIPSIS = "..."

    def _one_line(text):
        return " ".join(str(text or "").split())

    def _elide_middle(text, fits):
        return text if fits(text) else text[:12] + _ELLIPSIS + text[-11:]

FOLDER_IN_LABEL = 24   # characters of a folder name shown in "+ NEW CHAT: <FOLDER>" (elided in the middle)


class ChatsScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        self.folders = []
        self._all_conversations = []
        self.selected_folder_id = None  # None == "All"
        self._search_after_id = None
        # Every API call this screen makes runs off the Tk thread (see
        # async_call.py): the list loads through this latest-wins slot, and
        # the writes through _write_then_reload / _on_new_chat below.
        self._loader = Latest(self, deliver_when=self.is_visible)
        self._creating_chat = False

        # B5: the header carries the action as a text word at its right end (approved r2 Chats);
        # [N] is on the legend line (legend_items), not in the label. No Chats EXPORT ALL (OI-12:
        # Home keeps it).
        self.header, self._refresh_datetime = make_header(self, ctx.theme, "Chats")
        self.action_row = getattr(self.header, "actions", None) or tk.Frame(self.header, bg=ctx.theme.bg)
        try:   # a text action's word ends at the gutter (x 1084); its invisible 6 px hit area overhangs it
            if self.action_row.winfo_manager() == "place":
                self.action_row.place_configure(x=-self.header._s(16 - 6))
        except Exception:  # noqa: BLE001
            pass
        self._new_chat_btn = self._action_button("New chat", "n", self._on_new_chat)

        prompt_outer = tk.Frame(self, bg=ctx.theme.bg, height=0)
        prompt_outer.pack(fill="x", padx=16)
        self.prompt_slot = make_prompt_slot(prompt_outer, ctx.theme)

        self.list_screen = ListScreen(self, ctx, on_search_change=self._on_search_change, empty_text="No chats",
                                      empty_caption="Press + NEW CHAT to start one")
        self.list_screen.pack(fill="both", expand=True)

    def on_show(self):
        self._refresh_datetime()
        self.selected_folder_id = None
        self.list_screen.set_search_text("")
        self.bind_mnemonic("f", self._on_new_folder)
        self.bind_mnemonic("n", self._on_new_chat)
        self._reload()

    def on_hide(self):
        super().on_hide()
        clear_prompt_slot(self.prompt_slot)
        self.list_screen.on_hide()

    def focus_search(self):
        self.list_screen.focus_search()

    def legend_items(self):
        # digits: the list rows (ListScreen.bind_digits); N / F: on_show's mnemonics; / and Esc: the
        # router's global keys (/ -> focus_search above)
        # the digit range is the rows bound now (B2 fix ADV1-03/04: "1-3" for three chats, "0-9" for ten+)
        return self.list_screen.digit_legend("open") + [("N", "new chat"), ("F", "new folder"), ("/", "search"),
                                                        ("ESC", "back")]

    # ---- action row ----

    def _action_button(self, text, mnemonic, command):
        """A header text action (B5, approved shared_glyphs.text_action): the word in
        C9B `value`, upper-cased, no box at rest; hover / keyboard focus draw the ring;
        click, Return and space run it. The mnemonic key is shown on the legend line
        (legend_items), so it is no longer part of the label. Same pattern as Projects'
        _action_button (each screen owns its own small presentational helpers)."""
        theme = self.ctx.theme
        label = f"+ {text}"
        if _TextAction is not None:
            try:
                btn = _TextAction(self.action_row, theme, text=label, upper=True, command=command)
                btn.pack(side="right", padx=(4, 0))   # words 16 px apart (6 px hit pads each)
                return btn
            except Exception:  # noqa: BLE001 - the action must always be there
                pass
        btn = tk.Label(
            self.action_row, text=label.upper(), bg=theme.bg, fg=theme.text,
            font=theme_mod.FONT_CAPTION, cursor="hand2", padx=6, pady=4,
        )
        btn.pack(side="right", padx=(4, 0))   # words 16 px apart (6 px hit pads each)
        btn.bind("<Button-1>", lambda e: command())
        return btn

    def _on_new_chat(self):
        # a DRAFT new chat in the selected folder: its row is created by its first message (no abandoned empty chats)
        self.ctx.new_session(folder_id=self.selected_folder_id)

    # ---- data ----

    def _reload(self, search=None):
        api = self.ctx.api

        def fetch():
            return api.list_folders(), api.list_conversations(search=search)

        def apply(result):
            self.folders, self._all_conversations = result
            self._render_chips()
            self._render_rows()

        self._loader.start(
            fetch, apply, lambda e: show_toast(self, self.ctx.theme, f"Could not load chats: {e}", kind="error")
        )

    def _write_then_reload(self, work, error_label, then=None):
        """Runs a write off the Tk thread; on success runs `then` (if any) and
        reloads the list, on failure shows "<error_label>: <why>"."""
        def done(_result):
            if then is not None:
                then()
            self._reload()

        run_async(
            self, work, done,
            lambda e: show_toast(self, self.ctx.theme, f"{error_label}: {e}", kind="error"),
        )

    def _on_search_change(self, text):
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._scroll_reset_pending = True      # a different list is coming: it opens at its top (see _render_rows)
        self._search_after_id = self.after(250, lambda: self._reload(search=text.strip() or None))

    def _render_chips(self):
        chips = [("All", self.selected_folder_id is None, lambda: self._select_folder(None))]
        for folder in self.folders:
            chips.append(
                (
                    folder["name"], self.selected_folder_id == folder["id"],
                    lambda fid=folder["id"]: self._select_folder(fid),
                    self._folder_context_items(folder),
                )
            )
        chips.append(("+", False, self._on_new_folder))
        self.list_screen.set_chips(chips)
        self._update_new_chat_label()

    def _folder_context_items(self, folder):
        folder_id, name = folder["id"], folder["name"]
        return [
            ("Rename folder", lambda: self._rename_folder(folder_id, name)),
            ("Delete folder", lambda: self._delete_folder(folder_id, name)),
        ]

    def _update_new_chat_label(self):
        """Keeps the New chat button legible about where it's about to
        create - otherwise selecting a folder chip silently changes what
        [N]/click does with no visible confirmation."""
        folder_name = next((f["name"] for f in self.folders if f["id"] == self.selected_folder_id), None)
        if folder_name:
            # D-27 CHATS-02: "+ NEW CHAT: <FOLDER>". A long name is elided in the middle so the
            # header title keeps its room; the whole name stays on its filter word.
            folder_name = _one_line(folder_name).strip()
            if len(folder_name) > FOLDER_IN_LABEL:
                folder_name = _elide_middle(folder_name, lambda s: len(s) <= FOLDER_IN_LABEL)
        label = f"New chat: {folder_name}" if folder_name else "New chat"
        self._new_chat_btn.configure(text=f"+ {label}")

    def _select_folder(self, folder_id):
        self.selected_folder_id = folder_id
        self._scroll_reset_pending = True      # another folder's list: it opens at its top (see _render_rows)
        self._render_chips()
        self._render_rows()

    def _on_new_folder(self):
        show_inline_prompt(self.prompt_slot, self.ctx.theme, "Folder name:", self._create_folder)

    def _create_folder(self, name):
        self._write_then_reload(lambda: self.ctx.api.create_folder(name), "Could not create folder")

    def _rename_folder(self, folder_id, current_name):
        show_inline_prompt(
            self.prompt_slot, self.ctx.theme, "Folder name:",
            lambda name: self._do_rename_folder(folder_id, name), initial=current_name,
        )

    def _do_rename_folder(self, folder_id, name):
        self._write_then_reload(lambda: self.ctx.api.rename_folder(folder_id, name), "Could not rename folder")

    def _delete_folder(self, folder_id, name):
        show_inline_confirm(
            self, self.ctx.theme, f'Delete folder "{name}"? Chats inside move to "All", not deleted.',
            on_confirm=lambda: self._do_delete_folder(folder_id), confirm_label="Delete", danger=True,
            title="DELETE FOLDER",       # B5: the title says what is deleted
        )

    def _do_delete_folder(self, folder_id):
        def forget_selection():
            if self.selected_folder_id == folder_id:
                self.selected_folder_id = None

        self._write_then_reload(
            lambda: self.ctx.api.delete_folder(folder_id), "Could not delete folder", then=forget_selection
        )

    def _render_rows(self):
        visible = [c for c in self._all_conversations if not c.get("project_id")]
        if self.selected_folder_id is not None:
            visible = [c for c in visible if c.get("folder_id") == self.selected_folder_id]
        rows = [
            ListRow(
                conv.get("title") or "New chat",
                on_activate=lambda cid=conv["id"]: self.ctx.go_to_chat(cid),
                context_items=self._context_items_for(conv),
                tail=format_tail(conv.get("updated_at")),      # B5: "Oct 04  09:41"
            )
            for conv in visible
        ]
        # A folder or search change opens the new list at its top; a reload of the same list (rename, move, delete,
        # a new chat) keeps the offset and the focused row (B5 fix ADV2-06). The flag is set where the list changes.
        reset = getattr(self, "_scroll_reset_pending", False)
        self._scroll_reset_pending = False
        self.list_screen.set_rows(rows, reset_scroll=reset)

    def _context_items_for(self, conv):
        """Rename / Move (submenu of destination folders) / Delete - Move
        used to flatten one "Move to: X" entry per folder into the top-level
        menu; ListScreen.set_chips'/set_rows' menu (context_menu.populate_menu) supports a
        real cascading submenu, so it's compressed into a single "Move"
        entry instead."""
        conv_id = conv["id"]
        items = [("Rename", lambda: self._rename_conversation(conv_id))]
        move_items = []
        if conv.get("folder_id") is not None:
            move_items.append(("General (no folder)", lambda: self._move_conversation(conv_id, None)))
        for folder in self.folders:
            if conv.get("folder_id") != folder["id"]:
                move_items.append((folder["name"], lambda fid=folder["id"]: self._move_conversation(conv_id, fid)))
        if move_items:
            items.append(("Move", move_items))
        items.append(("Delete", lambda: self._delete_conversation(conv_id)))
        return items

    def _rename_conversation(self, conv_id):
        current = next(
            (c.get("title") or "" for c in self._all_conversations if c["id"] == conv_id), ""
        )
        show_inline_prompt(
            self.prompt_slot, self.ctx.theme, "Conversation name:",
            lambda name: self._do_rename(conv_id, name), initial=current,
        )

    def _do_rename(self, conv_id, name):
        self._write_then_reload(lambda: self.ctx.api.rename_conversation(conv_id, name), "Could not rename")

    def _move_conversation(self, conv_id, folder_id):
        self._write_then_reload(
            lambda: self.ctx.api.update_conversation(conv_id, folder_id=folder_id), "Could not move conversation"
        )

    def _delete_conversation(self, conv_id):
        title = next(
            (c.get("title") or "New chat" for c in self._all_conversations if c["id"] == conv_id), "?"
        )
        show_inline_confirm(
            self, self.ctx.theme, f'Delete "{title}"? This cannot be undone.',
            on_confirm=lambda: self._do_delete(conv_id), confirm_label="Delete", danger=True,
            title="DELETE CHAT",         # B5: the title says what is deleted
        )

    def _do_delete(self, conv_id):
        def forget():
            hook = getattr(self.ctx, "on_conversation_deleted", None)       # B14-H7: drop its queued message
            if hook is not None:
                hook(conv_id)
        self._write_then_reload(lambda: self.ctx.api.delete_conversation(conv_id), "Could not delete", then=forget)
