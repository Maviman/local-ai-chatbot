"""Projects: directory-grid of projects grouped by project-group (same
grouping logic as the old ProjectListView), with a detail mode listing one
project's conversations. Per the confirmed mockup revision there is no
right-hand action column - standalone actions (grid: New project; detail:
New chat/Manage) are outlined inline buttons in an action row below the
header, each with a single-letter mnemonic (N, or N/M in detail) so
they're both clickable and keyboard-reachable. Digits quick-select grid
items / detail rows.

"New group" was removed 2026-08-03 - creating a group silently succeeded
server-side but never rendered anything, since _reload_grid() below only
ever emits a tile for a group that already has >=1 project in it, and
there was no UI anywhere to assign an existing project into a newly
created (necessarily empty) group either. Grouping/rendering logic itself
is untouched - group_id-tagged projects (however they got that way) still
render correctly under their group's heading.

The manage/delete dialogs (instructions, files + upload streaming, memory,
delete-with-keep-options) are reused from projects_panel.py - that
field-level logic is already debugged; only the list/detail containers
were replaced by this screen.
"""

import time
import tkinter as tk

from .. import theme as theme_mod
from ..async_call import Latest, run_async
from .base import (
    Screen, clear_prompt_slot, make_header, make_prompt_slot, show_inline_confirm, show_inline_prompt, show_toast,
)
from .directory_screen import DirectoryItem, DirectoryScreen
from .list_screen import ListRow, ListScreen, ProjectList, format_tail

try:  # B5: header text actions come from the kit (guarded: a plain Label stands in)
    from ..kit.widgets import TextAction as _TextAction
except Exception:  # noqa: BLE001
    _TextAction = None

UNGROUPED = "Ungrouped"


class ProjectsScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self.groups = []
        self.projects = []
        self._detail_project = None  # dict while in detail mode, else None
        # Every API call this screen makes runs off the Tk thread (see
        # async_call.py); each kind of load gets its own latest-wins slot.
        self._grid_loader = Latest(self, deliver_when=self.is_visible)
        self._detail_loader = Latest(self, deliver_when=self.is_visible)   # opening a project (gates the view switch)
        self._rows_loader = Latest(self, deliver_when=self.is_visible)     # the detail view's conversation list
        self._creating = False

        # B2: the shared title-only header (kit.header, Canvas text). title_var drives its title:
        # PROJECTS in the grid, the project's name in detail (elided in the middle when too long,
        # the full name in the header's hover tip - never cut). The key hints that sat under the
        # date are on the shell's legend line now (legend_items); the date itself is dropped (D-32),
        # though _refresh_datetime still keeps datetime_var current.
        self.header, _ = make_header(self, theme, "Projects")
        self.title_var = tk.StringVar(value="Projects")
        self.title_var.trace_add("write", lambda *_a: self.header.set_title(self.title_var.get())
                                 if hasattr(self.header, "set_title") else None)
        self.datetime_var = tk.StringVar()

        # B5: the actions are text words at the header's right end (grid: + NEW PROJECT;
        # detail: MANAGE  + NEW CHAT); N / M are on the legend line. action_row IS the header's
        # actions frame, so _show_grid / _apply_detail rebuild it exactly as before.
        self.action_row = getattr(self.header, "actions", None) or tk.Frame(self.header, bg=theme.bg)
        try:   # a text action's word ends at the gutter (x 1084); its invisible 6 px hit area overhangs it
            if self.action_row.winfo_manager() == "place":
                self.action_row.place_configure(x=-self.header._s(16 - 6))
        except Exception:  # noqa: BLE001
            pass

        prompt_outer = tk.Frame(self, bg=theme.bg, height=0)
        prompt_outer.pack(fill="x", padx=16)
        self.prompt_slot = make_prompt_slot(prompt_outer, theme)

        # B5: one column of unboxed rows under group headings (ProjectList.set_items is the
        # adapter for the DirectoryItem list _apply_grid builds; grid_view stays the packed widget)
        self.grid_view = ProjectList(self, ctx)
        self.detail_view = ListScreen(self, ctx, show_search=False, empty_text="No chats",
                                      empty_caption="Press + NEW CHAT to start one")

    def _refresh_datetime(self):
        self.datetime_var.set(time.strftime("%Y-%m-%d  %H:%M"))

    def legend_items(self):
        # digits: the grid boxes / detail rows (bind_digits); N / M: the mnemonics _show_grid and
        # _apply_detail bind; Esc: the router's global back (detail -> grid through handle_back)
        # the digit range is what the showing view has bound now (B2 fix ADV1-03/04)
        if self._detail_project is not None:
            return self.detail_view.digit_legend("open") + [("N", "new chat"), ("M", "manage"), ("ESC", "back")]
        return self.grid_view.digit_legend("open") + [("N", "new project"), ("ESC", "back")]

    def _refresh_legend(self):
        """The keys change with the mode: ask the shell to repaint the legend (no-op standalone)."""
        refresh = getattr(self.ctx, "refresh_legend", None)
        if refresh is not None and self.is_visible():
            refresh()

    def _action_button(self, text, mnemonic, command):
        """A header text action (B5, approved shared_glyphs.header_actions): the word in
        C9B `value`, upper-cased, no box at rest; hover / keyboard focus draw the ring;
        click, Return and space run it. Visible and clickable, not just reachable via its
        mnemonic key (shown on the legend line now). A creating action reads "+ NEW ...";
        Manage reads MANAGE. The first action made sits rightmost (detail: MANAGE  + NEW CHAT)."""
        theme = self.ctx.theme
        label = f"+ {text}" if text.lower().startswith("new ") else text
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

    # ---- lifecycle ----

    def on_show(self):
        self._refresh_datetime()
        if self._detail_project is not None:
            self._show_detail(self._detail_project["id"])
        else:
            self._show_grid()

    def on_hide(self):
        super().on_hide()
        clear_prompt_slot(self.prompt_slot)
        self.grid_view.on_hide()
        self.detail_view.on_hide()

    def handle_back(self):
        if self._detail_project is not None:
            self._show_grid()
            return True
        return False

    # ---- grid mode ----

    def _show_grid(self):
        clear_prompt_slot(self.prompt_slot)
        # A project the user asked to open a moment ago must not pop back up
        # over the grid they've just returned to (Esc while it was loading).
        self._detail_loader.invalidate()
        self._detail_project = None
        self.detail_view.pack_forget()
        self.detail_view.clear_local_bindings()
        self.grid_view.pack(fill="both", expand=True)
        self.title_var.set("Projects")
        self._refresh_legend()
        for child in self.action_row.winfo_children():
            child.destroy()
        self._action_button("New project", "n", self._on_new_project)
        self._reload_grid()
        self.clear_local_bindings()
        self.bind_mnemonic("n", self._on_new_project)

    def _reload_grid(self):
        api = self.ctx.api

        def fetch():
            return api.list_project_groups(), api.list_projects(), api.list_conversations()

        self._grid_loader.start(
            fetch, self._apply_grid,
            lambda e: show_toast(self, self.ctx.theme, f"Could not load projects: {e}", kind="error"),
        )

    def _apply_grid(self, result):
        self.groups, self.projects, conversations = result
        counts = {}
        for conv in conversations:
            pid = conv.get("project_id")
            if pid is not None:
                counts[pid] = counts.get(pid, 0) + 1

        by_group = {}
        ungrouped = []
        for proj in sorted(self.projects, key=lambda p: (p.get("sort_order") or 0, p["name"])):
            gid = proj.get("group_id")
            if gid is None:
                ungrouped.append(proj)
            else:
                by_group.setdefault(gid, []).append(proj)

        items = []
        # by name (case-insensitively), then id: a stable order, and each group's heading stands on its own id (group_key) -
        # two groups named the same, or one named like the ungrouped block, used to merge or split headings (B5 ADV1-08)
        for group in sorted(self.groups, key=lambda g: (str(g.get("name") or "").casefold(), g["id"])):
            for proj in by_group.get(group["id"], []):
                items.append(self._project_item(proj, group["name"], counts, group_key=("group", group["id"])))
        for proj in ungrouped:
            items.append(self._project_item(proj, UNGROUPED if self.groups else None, counts, group_key="__ungrouped__"))
        self.grid_view.set_items(items)

    def _project_item(self, proj, group_name, counts, group_key=None):
        n = counts.get(proj["id"], 0)
        caption = f"{n} chat{'s' if n != 1 else ''}"
        return DirectoryItem(
            proj["name"], caption,
            on_activate=lambda pid=proj["id"]: self._show_detail(pid),
            group=group_name, group_key=group_key,
        )

    def _on_new_project(self):
        show_inline_prompt(self.prompt_slot, self.ctx.theme, "Project name:", self._create_project)

    def _create_project(self, name):
        if self._creating:
            return
        self._creating = True

        def created(proj):
            self._creating = False
            self.ctx.go_to_project_manage(proj["id"])

        def failed(error):
            self._creating = False
            show_toast(self, self.ctx.theme, f"Could not create project: {error}", kind="error")

        run_async(self, lambda: self.ctx.api.create_project(name), created, failed)

    # ---- detail mode ----

    def _show_detail(self, project_id):
        clear_prompt_slot(self.prompt_slot)

        def failed(error):
            show_toast(self, self.ctx.theme, f"Could not load project: {error}", kind="error")
            self._show_grid()

        # The view only switches once the project (its name is the title) has
        # arrived; until then the current view stays up.
        self._detail_loader.start(lambda: self.ctx.api.get_project(project_id), self._apply_detail, failed)

    def _apply_detail(self, proj):
        self._detail_project = proj
        self._detail_scroll_reset = True       # another project's chats: the list opens at its top (see _apply_detail_rows)
        self.grid_view.pack_forget()
        self.grid_view.clear_local_bindings()
        self.detail_view.pack(fill="both", expand=True)
        self.title_var.set(proj["name"])
        self._refresh_legend()
        for child in self.action_row.winfo_children():
            child.destroy()
        self._action_button("New chat", "n", self._on_new_chat)
        self._action_button("Manage", "m", self._on_manage)
        self._reload_detail()
        self.clear_local_bindings()
        self.bind_mnemonic("n", self._on_new_chat)
        self.bind_mnemonic("m", self._on_manage)

    def _reload_detail(self):
        if self._detail_project is None:
            return
        project_id = self._detail_project["id"]
        self._rows_loader.start(
            self.ctx.api.list_conversations,
            lambda all_convs: self._apply_detail_rows(project_id, all_convs),
            lambda e: show_toast(self, self.ctx.theme, f"Could not load conversations: {e}", kind="error"),
        )

    def _apply_detail_rows(self, project_id, all_convs):
        if self._detail_project is None or self._detail_project["id"] != project_id:
            return  # the user has left (or switched) this project while the list was loading
        visible = [c for c in all_convs if c.get("project_id") == project_id]
        rows = [
            ListRow(
                conv.get("title") or "New chat",
                on_activate=lambda cid=conv["id"]: self.ctx.go_to_chat(cid),
                context_items=[
                    ("Rename", lambda cid=conv["id"], t=conv.get("title") or "": self._rename_chat(cid, t)),
                    ("Delete", lambda cid=conv["id"], t=conv.get("title") or "New chat": self._delete_chat(cid, t)),
                ],
                tail=format_tail(conv.get("updated_at"), "iso"),      # B5: the updated time
            )
            for conv in visible
        ]
        reset = getattr(self, "_detail_scroll_reset", False)
        self._detail_scroll_reset = False
        self.detail_view.set_rows(rows, reset_scroll=reset)

    def _on_new_chat(self):
        if self._detail_project is None:
            return
        # a DRAFT new chat in this project: its row is created by its first message (no abandoned empty chats)
        self.ctx.new_session(project_id=self._detail_project["id"])

    def _on_manage(self):
        if self._detail_project is None:
            return
        self.ctx.go_to_project_manage(self._detail_project["id"])

    def forget_project(self, project_id):
        """Called via ctx.on_project_deleted after a project is deleted
        from the Manage screen - clears a stale detail-mode reference so
        coming back here shows the grid instead of re-fetching a 404."""
        if self._detail_project is not None and self._detail_project["id"] == project_id:
            self._detail_project = None
            if self.is_visible():
                # B5 fix ADV1-05: still showing the deleted project's detail - back to the grid
                # (its N / M / digits / Esc would otherwise act on a project that is gone)
                self._show_grid()

    def _rename_chat(self, conv_id, current):
        show_inline_prompt(
            self.prompt_slot, self.ctx.theme, "Conversation name:",
            lambda name: self._do_rename_chat(conv_id, name), initial=current,
        )

    def _do_rename_chat(self, conv_id, name):
        run_async(
            self, lambda: self.ctx.api.rename_conversation(conv_id, name), lambda _r: self._reload_detail(),
            lambda e: show_toast(self, self.ctx.theme, f"Could not rename: {e}", kind="error"),
        )

    def _delete_chat(self, conv_id, title):
        show_inline_confirm(
            self, self.ctx.theme, f'Delete "{title}"? This cannot be undone.',
            on_confirm=lambda: self._do_delete_chat(conv_id), confirm_label="Delete", danger=True,
            title="DELETE CHAT",         # B5: the title says what is deleted
        )

    def _do_delete_chat(self, conv_id):
        def deleted(_r):
            hook = getattr(self.ctx, "on_conversation_deleted", None)       # B14-H7: drop its queued message
            if hook is not None:
                hook(conv_id)
            self._reload_detail()

        run_async(
            self, lambda: self.ctx.api.delete_conversation(conv_id), deleted,
            lambda e: show_toast(self, self.ctx.theme, f"Could not delete: {e}", kind="error"),
        )
