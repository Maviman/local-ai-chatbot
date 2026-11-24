"""fx/page08/cue_policy.py - Settings 08 CUE POLICY, the v1 rows (B13; BUILD_PLAN §4 B13, GAP_FX 4.1, spec 08.2;
approved r2_settings_sound): when a reply's cue plays, what may sound while the window is closed, and Windows'
do-not-disturb.

    CUE POLICY
    Reply complete              WHEN AWAY  ALWAYS  OFF               fx.sound.reply_landed   (away / always / off)
    While window is closed      SILENT  REPLIES ONLY  ALL            fx.sound.while_closed   (silent / replies / all)
    [x] Quiet when Windows says so                                   fx.sound.respect_dnd

The v1 rows only (X-14, no dead switch): "Signal acquired" and "Context limit alert" arrive with their hooks in B14,
"Wait pings" (and the send-sound variant) in B14b; the CUE LIST (fx.sound.muted_cues) is not built in B13 either. The
five category lamps are drawn in SIGNAL OUTPUT, where the approved frame has them (fx/page08/signal_output.py).

* A pick writes its leaf once (page.set_pref); re-picking the word already shown writes nothing. No audition: these
  rows change WHEN cues play, not how they sound. Every change applies at once (the engine follows the store).
* The rows follow the store (page.subscribe("fx.sound")): RESTORE DEFAULTS, a backup restore.
* Read-only page (kill switch): the same rows disabled with the stored values; the labels in value ink (B8's rule for a
  label beside a disabled control).
* RESTORE DEFAULTS resets these three leaves (D-3: away / replies; respect_dnd on).
No timer; Tk thread only.
"""

import tkinter as tk

from ..sound_motion_page import Section, register_section
from . import _sound_common as sc

TITLE = "Cue policy"
REPLY_LABEL = "Reply complete"
CLOSED_LABEL = "While window is closed"
DND_LABEL = "Quiet when Windows says so"
LEAF_REPLY = "fx.sound.reply_landed"
LEAF_CLOSED = "fx.sound.while_closed"
LEAF_DND = "fx.sound.respect_dnd"
REPLY_OPTIONS = (("away", "WHEN AWAY"), ("always", "ALWAYS"), ("off", "OFF"))
CLOSED_OPTIONS = (("silent", "SILENT"), ("replies", "REPLIES ONLY"), ("all", "ALL"))


class CuePolicySection(Section):
    noun = "sound"

    def __init__(self, page, parent):
        from ...kit.widgets import Segmented
        self.page = page
        self.parent = parent
        self._released = False
        t = page.t
        live = page.live
        page.title(parent, TITLE)
        self.segments = {}
        for leaf, label, options in ((LEAF_REPLY, REPLY_LABEL, REPLY_OPTIONS),
                                     (LEAF_CLOSED, CLOSED_LABEL, CLOSED_OPTIONS)):
            row = page.option_row(parent)
            lab = page.label(row, label, fg=t["bright"] if live else t["value"])
            lab.grid(row=0, column=0, sticky="w")
            var = page.string_var(self._choice(leaf, options))
            seg = Segmented(row, page.theme, options=options, variable=var,
                            command=lambda leaf=leaf: self._on_pick(leaf), ground=t["bg"],
                            state="normal" if live else "disabled")
            seg.grid(row=0, column=1, sticky="w")
            self.segments[leaf] = (lab, var, seg, options)
        row = page.option_row(parent)
        self.dnd_var = page.string_var(sc.flag(sc.read(page, LEAF_DND)))
        self.dnd = sc.lamp(page, row, DND_LABEL, self.dnd_var, self._on_dnd)
        self.dnd.grid(row=0, column=0, sticky="w")
        page.subscribe("fx.sound", self._on_pref, owner=parent)
        self.refresh()

    # ---------------------------------------------------------------- reading
    def _choice(self, leaf, options):
        v = sc.read(self.page, leaf)
        values = [o[0] for o in options]
        if v not in values:
            v = self.page.default(leaf)
        return v if v in values else values[0]

    # ---------------------------------------------------------------- writing
    def _on_pick(self, leaf):
        page = self.page
        if self._released or not page.live:
            self.refresh()
            return
        _lab, var, _seg, options = self.segments[leaf]
        value = var.get()
        if value != self._choice(leaf, options):        # re-picking the word shown writes nothing
            page.set_pref(leaf, value)
        self.refresh()

    def _on_dnd(self):
        page = self.page
        if self._released or not page.live:
            self.refresh()
            return
        page.set_pref(LEAF_DND, self.dnd_var.get() == "1")
        self.refresh()

    def _on_pref(self, _path, _value):
        self.refresh()

    # ---------------------------------------------------------------- Section hooks
    def defaults(self):
        page = self.page
        return {LEAF_REPLY: page.default(LEAF_REPLY), LEAF_CLOSED: page.default(LEAF_CLOSED),
                LEAF_DND: page.default(LEAF_DND)}

    def refresh(self):
        if self._released:
            return
        live = self.page.live
        for leaf, (_lab, var, seg, options) in self.segments.items():
            sc.set_var(var, self._choice(leaf, options))
            sc.set_state(seg, live)
        sc.set_var(self.dnd_var, sc.flag(sc.read(self.page, LEAF_DND)))
        sc.set_state(self.dnd, live)

    def release(self):
        self._released = True
        for _lab, _var, seg, _o in self.segments.values():
            try:
                seg.configure(command=None)
            except tk.TclError:
                pass
        try:
            self.dnd.configure(command=None)
        except tk.TclError:
            pass


register_section("policy", 200, CuePolicySection)
