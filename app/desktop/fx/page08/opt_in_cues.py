"""fx/page08/opt_in_cues.py - Settings 08, the two opt-in cue rows that arrive with their hooks (B14; BUILD_PLAN §4 B14,
X-14; GAP_FX 4.1; spec 08.2 / S-11 / S-15; approved r2_settings_sound): "Signal acquired" and "Context limit alert".

    CUE POLICY                                          (fx/page08/cue_policy.py, order 200)
    ...
    [x] Quiet when Windows says so
    [ ] Signal acquired                                 fx.sound.signal_acquired   (default off)
    [ ] Context limit alert                             fx.sound.context_alert     (default off)

Its own section (key "optin", order 210), with no title of its own: the two lamps continue CUE POLICY, at the approved
28 px lamp pitch under "Quiet when Windows says so", the way the CRT rows (B11, 310) continue DISPLAY MOTION. It is the
registry's documented extension point (one new file here; cue_policy.py and sound_motion_page.py are unchanged).

What the two rows switch on (the engine's bridge decides; nothing here plays):
* Signal acquired: chat_view's H18 chat.first_output -> lock_on, only after a wait of at least 2.5 s, only when you are
  away, at most once per reply (fx/sound/bridge.py _first_output).
* Context limit alert: context_meter's H20 context.level -> caution, once, when a conversation crosses up to 90 % or
  more as the result of a reply (within 10 s of it), never after a reply-complete cue that played (bridge
  _context_level).
B14b's "Wait pings" row belongs ABOVE these two in the approved frame (a section at order 205, or a row added at the
top of this one); the CUE LIST is not built.

* A click writes its leaf once (page.set_pref); no audition (these rows change WHEN cues play, not how they sound) and
  no notice. The engine follows the store at once.
* The rows follow the store (page.subscribe("fx.sound")): RESTORE DEFAULTS, a backup restore.
* Read-only page (kill switch): the same lamps disabled with the stored values.
* RESTORE DEFAULTS resets these two leaves (both off).
No timer; Tk thread only.
"""

import tkinter as tk

from ..sound_motion_page import Section, register_section
from . import _sound_common as sc

SIGNAL_LABEL = "Signal acquired"
CONTEXT_LABEL = "Context limit alert"
LEAF_SIGNAL = "fx.sound.signal_acquired"
LEAF_CONTEXT = "fx.sound.context_alert"
ROWS = ((LEAF_SIGNAL, SIGNAL_LABEL), (LEAF_CONTEXT, CONTEXT_LABEL))
LAMP_GAP = 1                # approved r2_settings_sound: one lamp row (27 px) + 1 = the 28 px pitch under the DND lamp


class OptInCuesSection(Section):
    noun = "sound"

    def __init__(self, page, parent):
        from ...kit import px
        self.page = page
        self.parent = parent
        self._released = False
        self.lamps = {}
        for leaf, label in ROWS:
            var = page.string_var(sc.flag(sc.read(page, leaf)))
            lamp = sc.lamp(page, parent, label, var, lambda leaf=leaf: self._on_click(leaf))
            lamp.pack(anchor="w", pady=(px(parent, LAMP_GAP), 0))
            self.lamps[leaf] = (lamp, var)
        page.subscribe("fx.sound", self._on_pref, owner=parent)
        self.refresh()

    def _on_click(self, leaf):
        page = self.page
        if self._released or not page.live:
            self.refresh()
            return
        _lamp, var = self.lamps[leaf]
        want = var.get() == "1"
        if want != bool(sc.read(page, leaf)):         # a click that changes nothing writes nothing
            page.set_pref(leaf, want)
        self.refresh()

    def _on_pref(self, _path, _value):
        self.refresh()

    # ---------------------------------------------------------------- Section hooks
    def defaults(self):
        return {leaf: self.page.default(leaf) for leaf, _label in ROWS}

    def refresh(self):
        if self._released:
            return
        live = self.page.live
        for leaf, (lamp, var) in self.lamps.items():
            sc.set_var(var, sc.flag(sc.read(self.page, leaf)))
            sc.set_state(lamp, live)

    def release(self):
        self._released = True
        for lamp, _var in self.lamps.values():
            try:
                lamp.configure(command=None)
            except tk.TclError:
                pass


register_section("optin", 210, OptInCuesSection)
