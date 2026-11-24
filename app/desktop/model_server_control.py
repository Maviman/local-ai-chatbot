"""A2 (bug-fix batch 30, 2026-10-12): the RESTART control's flow, shared by Home's status row and Settings > Model's
button row. Asks the backend to restart the chat model server off the Tk thread; the backend's refusal (409 while an
image job holds the GPU or a reply is generating) is shown as a denied-family notice; otherwise the same load overlay
and 1 s health poll that Set active uses run until the server is ok, failed to start, or took too long. Before, the only
restart from the window was Set active on the same model, and nothing restarted a server that had died or hung."""
import queue
import threading

from .api_client import ApiError
from .model_load_overlay import RESTART_WAIT_SECONDS, ModelLoadOverlay
from .screens.base import show_toast

RESTART_LABEL = "Restarting the model server..."


def restart_model_server(widget, ctx, api, on_done=None):
    """Runs the whole flow on `widget` (its after() timers - it must outlive the restart); on_done(ok) at the end.
    Never raises: every outcome is a notice."""
    theme = ctx.theme
    q = queue.Queue()

    def worker():
        try:
            q.put(("ok", api.restart_model_server()))
        except ApiError as e:
            q.put(("error", str(e), getattr(e, "status", None)))
        except Exception as e:  # noqa: BLE001 - the backend itself unreachable
            q.put(("error", str(e), None))

    def finish(ok):
        if on_done is not None:
            on_done(ok)

    def poll_request():
        try:
            item = q.get_nowait()
        except queue.Empty:
            widget.after(50, poll_request)
            return
        if item[0] == "error":
            message, status = item[1], item[2]
            if status == 409 and "image" in message:
                show_toast(widget, theme, "Can't restart the model server while an image is generating - try again when it finishes.",
                           kind="warning")
            elif status == 409:
                show_toast(widget, theme, "Can't restart the model server while a reply is generating - try again when it finishes.",
                           kind="warning")
            else:
                show_toast(widget, theme, f"Could not restart the model server: {message}", kind="error")
            finish(False)
            return
        overlay = ModelLoadOverlay(widget, ctx, RESTART_LABEL)
        poll_health(overlay, 0)

    def poll_health(overlay, attempt):
        hq = queue.Queue()

        def health_worker():
            try:
                hq.put(api.health())
            except Exception:  # noqa: BLE001
                hq.put({"status": "starting"})

        threading.Thread(target=health_worker, daemon=True).start()
        widget.after(50, lambda: health_result(overlay, attempt, hq))

    def health_result(overlay, attempt, hq):
        try:
            health = hq.get_nowait()
        except queue.Empty:
            widget.after(50, lambda: health_result(overlay, attempt, hq))
            return
        status = health.get("status") if isinstance(health, dict) else None
        if status == "ok":
            overlay.finish()
            try:
                ctx.on_model_switched()
            except Exception:  # noqa: BLE001
                pass
            finish(True)
            return
        if status == "error":
            overlay.finish()
            show_toast(widget, theme, "The model server failed to start: " + str(health.get("error", "")), kind="error")
            finish(False)
            return
        if attempt > RESTART_WAIT_SECONDS:
            overlay.finish()
            show_toast(widget, theme, "Still waiting for the model to load.", kind="warning")
            finish(False)
            return
        widget.after(1000, lambda: poll_health(overlay, attempt + 1))

    threading.Thread(target=worker, daemon=True).start()
    widget.after(50, poll_request)
