"""fx.sound.bridge - seam events -> candidate cues (B12, sound engine v1). Pure: no device, no thread, no Tk, no clock.

The engine's "fx-sound" thread owns the one Bridge instance; every method takes the time it should use (`t`, `now`) as
an argument and never reads a clock itself, so the whole translation is a deterministic function of its inputs.

What lives here (AESTHETIC_SPEC 4.3 / 5.5 / S-04 / 10.2.1 S-06, GAP_FX 3.5 / 3.6, DECISIONS D-3 / D-23):
  * CUE_POLICY - each cue's category, priority, cuttable flag, min gap and max age (GAP_FX 3.5: the token buckets are
    cut, route / route_back min gap raised to 0.20 s instead). fault keeps fault's policy and plays hazard's sound
    (D-23; the recipe mapping is synth.CUE_SOURCE).
  * TOAST_RULES + toast_cue() - the S-06 toast -> sound table (ordered, first match wins; kind defaults error -> fault,
    success -> affirm, everything else silent; an explicit show_toast(..., sound=...) overrides both). The coverage over
    every show_toast( call site in app/desktop is tests/fx/test_fx_sound.py section T.
  * Bridge - the stateful translation (S-04): nav, toasts, confirms, loads (the 150 ms load_online verdict that any
    alert vetoes), shutdown, and the chat/reply/image/settings/context events the B14 hooks will emit (queued arm,
    stop acknowledgement, landed gated by reply_landed = away, lock_on and the context caution, both opt-in).
    v1 has no watches: wait.progress is ignored and power_on has no trigger until B14b.
"""
import re

__all__ = [
    "CATEGORIES", "CUE_POLICY", "CUES", "INSIST_PRIORITY", "TEST_CUE", "CATEGORY_CAPTION", "CUE_WORDS",
    "TOAST_RULES", "KIND_DEFAULT", "FETCH_CLASS_S", "SAME_TEXT_S", "VERDICT_S", "QUEUED_ARM_S", "LOCK_ON_WAIT_S",
    "CONTEXT_WINDOW_S", "STATE_CAP", "toast_cue", "normalise_text", "is_fetch_failure", "Candidate", "Bridge",
]

CATEGORIES = ("nav", "interface", "comms", "system", "alerts")

# cue -> (category, priority, cuttable, min_gap_s, max_age_s; None = exempt). GAP_FX 3.5.
CUE_POLICY = {
    "route":       ("nav",       1.0, True,  0.20, 0.150),
    "route_back":  ("nav",       1.0, True,  0.20, 0.150),
    "affirm":      ("interface", 1.2, True,  0.15, 2.0),
    "denied":      ("interface", 1.5, True,  0.25, 2.0),
    "hazard":      ("interface", 3.5, False, 0.50, 2.0),
    "transmit":    ("comms",     2.0, True,  0.15, 0.150),
    "lock_on":     ("comms",     2.0, True,  0.30, 2.0),
    "abort":       ("comms",     2.0, True,  0.30, 0.150),
    "landed":      ("comms",     2.5, False, 0.60, 2.0),
    "wait_ping":   ("comms",     0.5, True,  3.00, 2.0),
    "load_start":  ("system",    3.0, False, 1.00, 2.0),
    "load_online": ("system",    3.0, False, 1.00, 2.0),
    "power_on":    ("system",    3.0, False, 5.00, 2.0),
    "power_off":   ("system",    9.0, False, 5.00, None),
    "caution":     ("alerts",    4.0, False, 3.00, 2.0),
    "fault":       ("alerts",    4.2, False, 3.00, 2.0),
}
CUES = tuple(CUE_POLICY)
INSIST_PRIORITY = 3.6        # nav.denied{insist}: above load_start / hazard, below a sounding alert
ALERT_CUES = frozenset(c for c, p in CUE_POLICY.items() if p[0] == "alerts")

# test(category) auditions one representative cue of the category.
TEST_CUE = {"nav": "route", "interface": "affirm", "comms": "transmit", "system": "load_online", "alerts": "fault",
            "master": "affirm"}

# The lab's own words for each cue (signal_lab.html CUE_WORDS), for the settings page's captions.
CUE_WORDS = {
    "route": "screen change", "route_back": "back", "denied": "refusal", "affirm": "done / saved",
    "hazard": "dangerous prompt opens", "transmit": "message sent", "lock_on": "first words after a long wait (optional)",
    "abort": "stopped", "landed": "reply finished while away", "wait_ping": "long load still progressing (optional)",
    "load_start": "model load begins", "load_online": "model ready", "power_on": "app powers on",
    "power_off": "shutting down", "caution": "real timeout", "fault": "error",
}
CATEGORY_CAPTION = {
    "nav": "Screen changes you make: forward and back.",
    "interface": "Refusals, done / saved, and dangerous prompts opening.",
    "comms": "Message sent, stopped, and a reply finishing while you are away.",
    "system": "Model loads starting and finishing, and shutting down.",
    "alerts": "Errors and real timeouts.",
}

# --- the toast table (AESTHETIC_SPEC 10.2.1, S-06) --------------------------------------------------------------
# (rule id, kind or '*', pattern, cue or None = silent on purpose). Ordered: the first match wins.
TOAST_RULES = (
    ("could_not",          "*",       r"^Could not ",                                    "fault"),
    ("server_failed",      "*",       r"^The model server failed to start",              "fault"),
    ("not_valid_numbers",  "error",   r"aren't valid numbers",                           "denied"),
    ("workflow_missing",   "*",       r"^Workflow file not found",                       "denied"),
    ("cant_while",         "warning", r"^Can't .+ while ",                               "denied"),
    ("queued_already",     "warning", r"^You already have a queued message",             "denied"),
    ("queue_full",         "warning", r"^The queue is full",                             "denied"),   # bug-fix batch 33
    ("regen_dropped",      "warning", r"^Regenerate dropped",                            "denied"),   # bug-fix batch 33 part 3
    ("enter_first",        "warning", r"^(Enter a |Project name can't be empty|Nothing selected)", "denied"),
    ("nothing_to",         "warning", r"^(No messages to jump to|No conversations to export"
                                      r"|No image generation tool is configured)",       "denied"),
    ("open_first",         "warning", r"^(Open or start a chat before|Still uploading)", "denied"),
    # also the uplink's refused SAVE after a failed settings load (bug-fix batch 11, B6-H8)
    ("settings_loading",   "warning", r"^Settings (haven't finished loading|didn't load)", "denied"),
    # B12: also the lower-case "skipped (...)" / "skipped folders:" parts that the attach / drop notices join
    ("skipped",            "warning", r"^[Ss]kipped[ (]",                                "denied"),
    # bug-fix batch 30 (A1): the window's one notice when the model server dies or hangs mid-session (the RESTART
    # refusals are of the "Can't ... while" family above)
    ("server_watch",       "warning", r"^The model server (stopped|is not responding) - press RESTART", "caution"),
    ("timeout",            "warning", r"^(Still waiting for the model to load|The model is taking a long time)",
                                                                                         "caution"),
    ("cancelled",          "warning", r"cancelled\.?$",                                  None),
    ("regen_empty",        "warning", r"^Regenerate produced no reply",                  None),
    ("vision_off",         "warning", r"^Vision file turned off",                        None),
    ("reset_partial",      "warning", r"^Everything else was reset",                     "affirm"),
    # B12 build-time additions (call sites added after the spec's E12 pass):
    # the reply-cut-off notice (chat_view.cut_off_toast) is silent: reply.done owns the outcome sound (BUILD_PLAN B14)
    ("cut_off",            "warning", r"^(Reply cut off|The reply was interrupted)",     None),
    # a partial restore reads like 'Everything else was reset' (a success with an exception)
    ("restore_partial",    "warning", r"^Backup restored, except ",                      "affirm"),
)
_COMPILED = tuple((rid, kind, re.compile(pat), cue) for rid, kind, pat, cue in TOAST_RULES)
KIND_DEFAULT = {"error": "fault", "success": "affirm", "warning": None}
FETCH_RE = re.compile(r"^Could not (load|list)\b")
FETCH_CLASS_S = 60.0         # the fetch-failure class: at most one fault per 60 s
SAME_TEXT_S = 30.0           # identical normalised toast text: at most one fault per 30 s

VERDICT_S = 0.150            # load.end -> load_online after 150 ms unless an alert vetoed it
QUEUED_ARM_S = 1.0           # chat.queued_dispatch arms the next chat.launch of that conversation (silent)
LOCK_ON_WAIT_S = 2.5         # chat.first_output plays lock_on only after a wait this long (and only when away)
CONTEXT_WINDOW_S = 10.0      # context.level counts only within 10 s after that conversation's reply.done
CONTEXT_DANGER_PCT = 90      # context_meter.DANGER_THRESHOLD
CONTEXT_WARN_PCT = 75        # context_meter.WARN_THRESHOLD
STATE_CAP = 64               # per-conversation dicts are bounded (oldest evicted)

_QUOTED_RE = re.compile(r"(^|[\s:(\[=])(['\"])(.*?)\2")
_DIGITS_RE = re.compile(r"\d+")


def toast_cue(kind, text, sound=None):
    """-> (cue or None, rule id) for one toast. sound: an explicit override (a cue name, or 'silent'); an override
    that is neither is ignored (the table decides). Rule ids: 'override', a TOAST_RULES id, 'default:<kind>'."""
    if isinstance(sound, str) and sound:
        if sound == "silent":
            return None, "override"
        if sound in CUE_POLICY:
            return sound, "override"
    text = _bounded(text)
    k = kind if isinstance(kind, str) else ""
    for rid, rkind, rx, cue in _COMPILED:
        if (rkind == "*" or rkind == k) and rx.search(text):
            return cue, rid
    return KIND_DEFAULT.get(k), "default:" + (k if k in _KNOWN_KINDS else ("other" if k else "?"))


TEXT_SCAN_MAX = 4096         # a toast longer than this is matched on its head and tail only (never a long scan)
_KNOWN_KINDS = frozenset(("error", "warning", "success", "note", "info"))


def _bounded(text):
    if not isinstance(text, str):
        return ""
    if len(text) > TEXT_SCAN_MAX:
        return text[:TEXT_SCAN_MAX - 256] + text[-256:]
    return text


def _short(v, n=40):
    """A short printable label from event data (decision-log 'why' fields never hold unbounded user data)."""
    try:
        s = v if isinstance(v, str) else ("" if v is None else repr(v))
    except Exception:
        s = "?"
    return s[:n]


def normalise_text(text):
    """The S-06 collapse key: quoted parts and digits stripped, whitespace collapsed, lower case."""
    if not isinstance(text, str):
        return ""
    t = _QUOTED_RE.sub(r"\1", _bounded(text))
    t = _DIGITS_RE.sub("", t)
    return " ".join(t.split()).lower()


def is_fetch_failure(text):
    return isinstance(text, str) and FETCH_RE.match(text) is not None


class Candidate(object):
    """One cue the bridge proposes; the engine's arbiter decides whether it plays."""

    __slots__ = ("cue", "prio", "insist", "t", "event", "test", "verdict", "why", "conv", "seq", "collapse")

    def __init__(self, cue, t, event, why="", insist=False, test=False, verdict=False, conv=None):
        self.cue = cue
        self.prio = INSIST_PRIORITY if insist else CUE_POLICY[cue][1]
        self.insist = bool(insist)
        self.t = t
        self.event = event
        self.test = bool(test)
        self.verdict = bool(verdict)
        self.why = why
        self.conv = conv
        self.seq = 0
        self.collapse = None    # toast faults: (normalised text, is a fetch failure), charged when it PLAYS

    def __repr__(self):
        return "<Candidate %s prio=%s%s%s %s>" % (self.cue, self.prio, " insist" if self.insist else "",
                                                 " test" if self.test else "", self.why)


def _hashable(v):
    try:
        hash(v)
        return True
    except Exception:
        return False


def _put_capped(d, key, value, cap=STATE_CAP):
    d.pop(key, None)
    d[key] = value
    while len(d) > cap:
        d.pop(next(iter(d)))


class Bridge(object):
    """S-04 stateful translation. translate() -> (candidates, notes); notes are (what, detail) strings for the
    engine's decision log. State is reset on master off -> on and on recovery from the error / no-device state."""

    EVENTS = frozenset({
        "nav.route", "nav.denied", "load.begin", "load.end", "app.shutdown", "toast.shown", "confirm.shown",
        "chat.launch", "chat.queued_dispatch", "chat.stop_requested", "chat.first_output", "wait.progress",
        "reply.done", "image.finished", "image.rendered", "context.level", "settings.saved",
    })

    def __init__(self):
        self.reset()

    def reset(self):
        self.sent_at = {}          # conv -> launch time (lock_on's wait)
        self.queued_arm = {}       # conv -> chat.queued_dispatch time
        self.stop_ack = {}         # conv -> True: a stop was acknowledged at click time
        self.done_at = {}          # conv -> [reply.done time, landed actually played]
        self.ctx_band = {}         # conv -> last context band (0 below 75 %, 1 below 90 %, 2 at or above)
        self.last_done_conv = None
        self.verdict_at = None     # pending load_online verdict deadline
        self.fetch_last = None     # last fetch-failure fault time
        self.text_last = {}        # normalised toast text -> last fault time
        self.resets = getattr(self, "resets", -1) + 1

    # --- verdicts ---------------------------------------------------------------------------------------------
    def pending_deadline(self):
        return self.verdict_at

    def due(self, now):
        """The load_online verdict, once its 150 ms have passed without an alert. -> [Candidate]."""
        if self.verdict_at is not None and now >= self.verdict_at:
            t = self.verdict_at
            self.verdict_at = None
            return [Candidate("load_online", t, "load.end", why="verdict", verdict=True)]
        return []

    def note_played(self, cand, t):
        """The arbiter played `cand` (S-11 needs to know whether landed actually played for a reply)."""
        if cand.cue == "landed" and cand.conv is not None and cand.conv in self.done_at:
            self.done_at[cand.conv][1] = True
        if cand.collapse is not None:
            # fix ADV1-02: the S-06 collapse windows count faults the user HEARD, not proposals that were dropped
            # (DND, muted, hidden, busy) - those must not silence the next identical error too
            key, fetch = cand.collapse
            _put_capped(self.text_last, key, cand.t)
            if fetch:
                self.fetch_last = cand.t

    # --- translation -------------------------------------------------------------------------------------------
    def translate(self, event, data, t, prefs, presence=None):
        """event: the seam's name; data: its fields (dict); t: the emit time; prefs: the engine's normalised sound
        prefs; presence: a zero-arg callable -> (hidden, away) or None (unknown), called only when a cue depends on
        it. -> (list of Candidate, list of note strings). Never raises for bad data (-> a 'bad_event' note)."""
        notes = []
        if not isinstance(data, dict):
            return [], ["bad_event:data"]
        handler = _HANDLERS.get(event)
        if handler is None:
            return [], ["unknown_event"]
        try:
            cands = handler(self, data, t, prefs, presence, notes)
        except Exception as e:      # a malformed field must never stop the rest of the batch
            return [], ["bad_event:%s" % type(e).__name__]
        if self.verdict_at is not None:
            for c in cands:
                if c.cue in ALERT_CUES:
                    self.verdict_at = None
                    notes.append("vetoed:load_online")
                    break
        return cands, notes

    # --- handlers ---------------------------------------------------------------------------------------------
    def _nav_route(self, data, t, prefs, presence, notes):
        kind, cause = data.get("kind"), data.get("cause")
        if kind == "initial":
            notes.append("silent:initial")
            return []
        if cause == "auto":
            notes.append("silent:auto")
            return []
        if kind == "back":
            return [Candidate("route_back", t, "nav.route", why="back")]
        if kind == "forward":
            return [Candidate("route", t, "nav.route", why="forward")]
        notes.append("bad_event:kind")
        return []

    def _nav_denied(self, data, t, prefs, presence, notes):
        insist = data.get("insist") is True
        return [Candidate("denied", t, "nav.denied", why=_short(data.get("reason")), insist=insist)]

    def _load_begin(self, data, t, prefs, presence, notes):
        if self.verdict_at is not None:
            self.verdict_at = None
            notes.append("superseded:load_online")
        return [Candidate("load_start", t, "load.begin")]

    def _load_end(self, data, t, prefs, presence, notes):
        self.verdict_at = t + VERDICT_S
        notes.append("pending:load_online")
        return []

    def _app_shutdown(self, data, t, prefs, presence, notes):
        return [Candidate("power_off", t, "app.shutdown", why="terminal")]

    def _toast(self, data, t, prefs, presence, notes):
        kind, text, sound = data.get("kind"), data.get("text"), data.get("sound")
        if sound is not None and not (isinstance(sound, str) and (sound == "silent" or sound in CUE_POLICY)):
            notes.append("bad_event:sound")
        cue, rule = toast_cue(kind, text, sound)
        if cue is None:
            notes.append("silent:toast:" + rule)
            return []
        collapse = None
        if cue == "fault" and rule != "override":
            key = normalise_text(text)
            last = self.text_last.get(key)
            if last is not None and t - last < SAME_TEXT_S:
                notes.append("collapsed:same_text")
                return []
            fetch = is_fetch_failure(text)
            if fetch and self.fetch_last is not None and t - self.fetch_last < FETCH_CLASS_S:
                notes.append("collapsed:fetch")
                return []
            collapse = (key, fetch)     # charged by note_played() when the arbiter actually plays it
        c = Candidate(cue, t, "toast.shown", why=rule)
        c.collapse = collapse
        return [c]

    def _confirm(self, data, t, prefs, presence, notes):
        if data.get("danger") is True:
            return [Candidate("hazard", t, "confirm.shown", why="danger")]
        notes.append("silent:plain_confirm")
        return []

    def _conv(self, data, notes):
        conv = data.get("conv_id")
        if not _hashable(conv):
            notes.append("bad_event:conv_id")
            return None, False
        return conv, True

    def _queued_dispatch(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if ok:
            _put_capped(self.queued_arm, conv, t)
            notes.append("armed:queued")
        return []

    def _launch(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if not ok:
            return []
        _put_capped(self.sent_at, conv, t)
        armed = self.queued_arm.pop(conv, None)
        if armed is not None and 0 <= t - armed <= QUEUED_ARM_S:
            notes.append("silent:queued_arm")
            return []
        return [Candidate("transmit", t, "chat.launch", why=_short(data.get("kind")), conv=conv)]

    def _stop_requested(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if not ok:
            return []
        _put_capped(self.stop_ack, conv, True)
        return [Candidate("abort", t, "chat.stop_requested", why="at_click", conv=conv)]

    def _away(self, data, presence, notes):
        """reply_landed = away: not displayed, or the chat screen not visible, or the window hidden / minimised /
        not in front / the user not present. Unknown presence counts as present (silence is the default)."""
        if data.get("displayed") is False or data.get("visible") is False:
            return True
        pres = presence() if callable(presence) else None
        if pres is None:
            notes.append("presence:unknown")
            return False
        hidden, away = pres
        return bool(hidden or away)

    def _landed(self, data, t, prefs, presence, notes, conv, event):
        policy = prefs.get("reply_landed", "away")
        if policy == "off":
            notes.append("silent:reply_landed_off")
            return []
        if policy == "always" or self._away(data, presence, notes):
            return [Candidate("landed", t, event, why="away" if policy != "always" else "always", conv=conv)]
        notes.append("silent:watched")
        return []

    def _reply_done(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if not ok:
            return []
        outcome = data.get("outcome")
        acked = self.stop_ack.pop(conv, None) is not None
        self.sent_at.pop(conv, None)
        # B14-H10 (the UI session's decision): one reply = one alert - a reply that failed or was stopped sounds its
        # fault / abort, and no context caution follows it (only an 'ok' reply, whose landed did not play, can)
        _put_capped(self.done_at, conv, [t, outcome in ("error", "stopped")])
        self.last_done_conv = conv
        if outcome == "ok":
            return self._landed(data, t, prefs, presence, notes, conv, "reply.done")
        if outcome == "stopped":
            if acked:
                notes.append("silent:stop_acknowledged")
                return []
            return [Candidate("abort", t, "reply.done", why="stopped_unacknowledged", conv=conv)]
        if outcome == "error":
            return [Candidate("fault", t, "reply.done", why="stream_error", conv=conv)]
        if outcome == "empty":
            notes.append("silent:empty")
            return []
        notes.append("bad_event:outcome")
        return []

    def _image_finished(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if not ok:
            return []
        outcome = data.get("outcome")
        acked = self.stop_ack.pop(conv, None) is not None
        self.sent_at.pop(conv, None)
        if outcome == "ok":
            return self._landed(data, t, prefs, presence, notes, conv, "image.finished")
        if outcome == "cancelled":
            if acked:
                notes.append("silent:stop_acknowledged")
                return []
            return [Candidate("abort", t, "image.finished", why="cancelled_unacknowledged", conv=conv)]
        if outcome == "error":
            notes.append("silent:image_error_toast_owns_fault")
            return []
        notes.append("bad_event:outcome")
        return []

    def _first_output(self, data, t, prefs, presence, notes):
        conv, ok = self._conv(data, notes)
        if not ok:
            return []
        sent = self.sent_at.pop(conv, None)        # at most once per launch
        if not prefs.get("signal_acquired"):
            notes.append("silent:signal_acquired_off")
            return []
        if sent is None or t - sent < LOCK_ON_WAIT_S:
            notes.append("silent:short_wait")
            return []
        if not self._away(data, presence, notes):
            notes.append("silent:watched")
            return []
        return [Candidate("lock_on", t, "chat.first_output", why="away", conv=conv)]

    def _context_level(self, data, t, prefs, presence, notes):
        pct = data.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct != pct:
            notes.append("bad_event:pct")
            return []
        conv = data.get("conv_id", self.last_done_conv)
        if not _hashable(conv):
            notes.append("bad_event:conv_id")
            return []
        band = 2 if pct >= CONTEXT_DANGER_PCT else (1 if pct >= CONTEXT_WARN_PCT else 0)
        prev = self.ctx_band.get(conv)
        _put_capped(self.ctx_band, conv, band)
        if not prefs.get("context_alert"):
            return []
        done = self.done_at.get(conv)
        if band != 2 or (prev is not None and prev >= 2):
            return []
        if done is None or not (0 <= t - done[0] <= CONTEXT_WINDOW_S):
            notes.append("silent:not_after_reply")
            return []
        if done[1]:
            notes.append("silent:landed_played")
            return []
        done[1] = True                     # B14-H1: the caution is offered ONCE per reply (no re-arm on a down-and-up)
        return [Candidate("caution", t, "context.level", why="context_90", conv=conv)]

    def _settings_saved(self, data, t, prefs, presence, notes):
        if data.get("restart") is True:
            notes.append("silent:restart_defers_to_load")
            return []
        return [Candidate("affirm", t, "settings.saved")]

    def _ignored(self, data, t, prefs, presence, notes):
        notes.append("ignored:v1")
        return []

    def _no_sound(self, data, t, prefs, presence, notes):
        notes.append("silent:no_cue")
        return []


_HANDLERS = {
    "nav.route": Bridge._nav_route,
    "nav.denied": Bridge._nav_denied,
    "load.begin": Bridge._load_begin,
    "load.end": Bridge._load_end,
    "app.shutdown": Bridge._app_shutdown,
    "toast.shown": Bridge._toast,
    "confirm.shown": Bridge._confirm,
    "chat.queued_dispatch": Bridge._queued_dispatch,
    "chat.launch": Bridge._launch,
    "chat.stop_requested": Bridge._stop_requested,
    "chat.first_output": Bridge._first_output,
    "reply.done": Bridge._reply_done,
    "image.finished": Bridge._image_finished,
    "image.rendered": Bridge._no_sound,
    "context.level": Bridge._context_level,
    "settings.saved": Bridge._settings_saved,
    "wait.progress": Bridge._ignored,          # B14b (wait pings)
}
