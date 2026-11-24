"""Run blocking work (almost always an API call) off the Tk thread and hand the
result back on it.

The rule this implements is the one ChatView._refresh_max_parallel_chats_cache
already states in its comment and this app's other worker/queue/after loops
follow: a worker thread never touches Tk - not even widget.after() - it only
puts its result on a queue, and a poll scheduled *from the Tk thread* delivers
it. The reason it matters here: every call the UI makes goes to this app's own
backend, and the backend can be slow for reasons the UI can't see (a model load
saturating a spinning disk is the one that bit - see server.py's note above
db()). A call made directly on the Tk thread freezes the whole window for as
long as the backend takes, up to its 10 s timeout, per call.

    run_async(widget, work, on_done, on_error)   one-shot
    Latest(widget).start(work, on_done, on_error) "newest request wins" - for a
        screen that reloads its data every time it is shown, where an older,
        slower response arriving after a newer one must not overwrite it.
"""

import queue
import threading
import tkinter as tk

POLL_MS = 30


def run_async(widget, work, on_done, on_error=None):
    """Runs work() on a daemon thread, then calls on_done(result) on the Tk
    thread - or on_error(exception) if work() raised (an exception with no
    on_error is dropped: callers that care must pass one). If `widget` has been
    destroyed by the time the result is ready, nothing is called."""
    results = queue.Queue(maxsize=1)

    def runner():
        try:
            results.put(("ok", work()))
        except Exception as e:  # delivered to the caller below, not swallowed
            results.put(("error", e))

    threading.Thread(target=runner, daemon=True).start()

    def poll():
        try:
            kind, value = results.get_nowait()
        except queue.Empty:
            try:
                widget.after(POLL_MS, poll)
            except tk.TclError:
                pass  # widget destroyed while waiting - nothing left to deliver to
            return
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        if kind == "ok":
            on_done(value)
        elif on_error is not None:
            on_error(value)

    widget.after(POLL_MS, poll)


class Latest:
    """A "newest request wins" slot. Each start() supersedes the previous one:
    when an older job finally finishes its result is dropped instead of
    delivered, so a slow response can never overwrite what a newer request
    already put on screen. (The older HTTP call still runs to completion - it
    just can't be cancelled - but nobody acts on it.)

    deliver_when: optional zero-argument predicate checked when a result is
    about to be delivered; if it returns False the result is dropped. Screens
    pass their own is_visible: a result that lands after the user has already
    left the screen must not be applied to it (applying list data registers
    screen-wide digit-key shortcuts, which would then fire on whatever screen
    is showing). The next on_show() reloads anyway."""

    def __init__(self, widget, deliver_when=None):
        self.widget = widget
        self.deliver_when = deliver_when
        self._seq = 0
        self._delivered_seq = 0

    @property
    def busy(self):
        """True while the most recently started job hasn't delivered yet."""
        return self._delivered_seq != self._seq

    def invalidate(self):
        """Drops whatever is in flight without starting anything new - e.g.
        when the screen is hidden or the thing it was loading is gone."""
        self._seq += 1
        self._delivered_seq = self._seq

    def start(self, work, on_done, on_error=None):
        self._seq += 1
        seq = self._seq

        def done(value):
            if seq != self._seq:
                return
            self._delivered_seq = seq
            if self.deliver_when is not None and not self.deliver_when():
                return
            on_done(value)

        def failed(error):
            if seq != self._seq:
                return
            self._delivered_seq = seq
            if self.deliver_when is not None and not self.deliver_when():
                return
            if on_error is not None:
                on_error(error)

        run_async(self.widget, work, done, failed)
        return seq
