"""
Personal Terminal (formerly Local AI ChatBot) - native desktop entry point.

Runs the same backend as server.py (llama.cpp process management, the
local REST API, SQLite persistence) plus a native tkinter window in place
of the old app-mode browser window. See app/server.py's module docstring
for backend responsibilities - this file only owns process bootstrap and
the GUI.
"""

import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP_DIR)
_ROOT_DIR = os.path.dirname(_APP_DIR)
_DATA_DIR = os.path.join(_ROOT_DIR, "data")

# The last paragraph of every start-up message below (wording agreed with the launch-failure session).
_DIAGNOSE_LINE = (
    "Double-click Diagnose.bat in the app's folder: it checks this copy's files, this PC and the model server, "
    "and writes a report to data\\diagnostics-<date>.txt that you can send."
)


def _with_diagnose(text):
    """Appends the Diagnose.bat paragraph - only when Diagnose.bat is really there (an app folder copied on its own has none)."""
    try:
        if os.path.isfile(os.path.join(_ROOT_DIR, "Diagnose.bat")):
            return text + "\n\n" + _DIAGNOSE_LINE
    except Exception:
        pass
    return text


def _fatal_dialog(text, title="Personal Terminal"):
    """One error box, never raises. pythonw.exe has no console, so without this a start-up failure is just a program that vanishes.
    The native MessageBoxW comes first: it needs no Tcl/Tk, and a broken Tcl/Tk is one of the failures being reported. tkinter's
    box is the fallback. PT_FATAL_MSGFILE (a test hook, unset in normal use) appends the text to that file instead of showing a box."""
    path = os.environ.get("PT_FATAL_MSGFILE")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n=====\n")
        except OSError:
            pass
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x10000)  # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        return
    except Exception:
        pass
    try:
        from tkinter import messagebox

        messagebox.showerror(title, text)
    except Exception:
        pass


DESKTOP_LOG_MAX_BYTES = 5 * 1024 * 1024      # desktop.log is appended to by every launch: past this size it is moved aside first


def _rotate_log(path, limit):
    """Moves a log aside as <path>.1 (an older .1 is replaced) once it holds more than `limit` bytes, so a log that is only ever
    appended to cannot grow without end over months of launches. Never raises."""
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _open_log():
    """data\\desktop.log, line-buffered. If data\\ cannot be created or written (a read-only folder, Windows' Controlled folder
    access, a file called data) nothing else can work either: say so, and stop."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        path = os.path.join(_DATA_DIR, "desktop.log")
        _rotate_log(path, DESKTOP_LOG_MAX_BYTES)
        return open(path, "a", encoding="utf-8", buffering=1)
    except OSError as e:
        _fatal_dialog(_with_diagnose(
            "Personal Terminal cannot write to its data folder:\n" + _DATA_DIR + "\n" + str(e.strerror or e) + "\n"
            "Move the app to a folder you can write to, or allow it under Windows Security > Virus & threat protection > "
            "Ransomware protection (Controlled folder access), then start again."
        ))
        sys.exit(1)


# pythonw.exe has no console, so sys.stdout/sys.stderr are None - any bare
# print() reached from the imported backend would crash without this.
_log = _open_log()
sys.stdout = _log
sys.stderr = _log


def _another_instance_is_running():
    """Takes the single-instance lock - an OS mutex - and reports whether a
    previous launch already held it. Done here, before the multi-second imports
    below and long before the HTTP port is bound, so a second launch (an
    impatient double-click, or relaunching because the first window looks
    frozen while its model loads) can never get as far as starting its own
    llama-server on top of the first one's. server.already_running() can't
    provide that: it probes the HTTP port, which doesn't exist yet for the
    first few seconds and can't tell "not running" from "running but not
    answering yet". The OS releases the mutex whenever the process ends, however
    it ends, so a crash can never leave the app locked out."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        global _SINGLE_INSTANCE_HANDLE
        _SINGLE_INSTANCE_HANDLE = kernel32.CreateMutexW(None, False, "Local\\LocalAIChatBot-single-instance")
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False  # no ctypes/non-Windows: server.already_running() is still the backstop


_SINGLE_INSTANCE_HANDLE = None  # kept for the life of the process - closing it would drop the lock
_ANOTHER_INSTANCE_RUNNING = _another_instance_is_running()

# Tcl/Tk's compiled-in library path is wrong once this folder is relocated
# (copied to another drive/PC) - point it at the bundled copy explicitly.
_exe_dir = os.path.dirname(os.path.abspath(sys.executable))
_tcl_lib = os.path.join(_exe_dir, "tcl", "tcl8.6")
_tk_lib = os.path.join(_exe_dir, "tcl", "tk8.6")
if os.path.isfile(os.path.join(_tcl_lib, "init.tcl")):
    os.environ["TCL_LIBRARY"] = _tcl_lib
if os.path.isfile(os.path.join(_tk_lib, "tk.tcl")):
    os.environ["TK_LIBRARY"] = _tk_lib

# A copy that is incomplete or damaged (an update package extracted into an empty folder, a copy that skipped files, a file cut
# short) fails right here. Without the guard pythonw.exe exits with only a traceback in data\desktop.log and the user sees nothing.
try:
    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    import server
    from desktop import theme
    from desktop.api_client import ApiClient
    from desktop.fx import sound as fx_sound
    from desktop.main_window import MainWindow
except Exception as _import_error:  # noqa: BLE001 - whatever it is, this copy cannot start
    import traceback

    traceback.print_exc()
    _what = getattr(_import_error, "name", None) or getattr(_import_error, "filename", None) or ""
    _fatal_dialog(_with_diagnose(
        "Part of the app's code is missing or damaged:\n"
        + (os.path.basename(str(_what)) + "\n" if _what else "")
        + type(_import_error).__name__ + ": " + str(_import_error) + "\n"
        "Re-extract the whole update package over this folder (allow overwrite), or copy the app folder from a working "
        "installation, then start again."
    ))
    sys.exit(1)


def _ping_show_window():
    """Asks the instance that is already running to bring its window to the front. -> (answered, folder it was started from or
    None). Older versions answer without the folder."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.APP_PORT}/api/show-window",
            data=b"", timeout=2,
        ) as reply:
            body = json.loads(reply.read().decode("utf-8", "replace") or "{}")
        root = body.get("root") if isinstance(body, dict) else None
        return True, (root if isinstance(root, str) and root else None)
    except Exception as e:
        print("show-window ping failed:", e)
        return False, None


def _same_folder(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return False


def _already_running_text(folder):
    where = ("Personal Terminal is already running, started from:\n" + folder) if folder else \
        "Personal Terminal is already running, started from another folder or drive."
    return _with_diagnose(
        where + "\nUse that window. If you cannot find it, run Stop Chatbot.bat in that folder (or end pythonw.exe in Task Manager), "
        "then start again."
    )


# How long a second launch waits for the running copy to answer before it gives up and says so. A copy that is merely starting answers
# within seconds (its port is served after the database is opened); one that never answers is wedged. PT_SECOND_LAUNCH_WAIT_S: a test hook.
_SECOND_LAUNCH_WAIT_S = float(os.environ.get("PT_SECOND_LAUNCH_WAIT_S") or 15)
_STUCK_TEXT = (
    "Personal Terminal is already starting, or an earlier copy is stuck.\n"
    "Wait a moment for its window. If none appears within a minute, run Stop Chatbot.bat in every folder you have started it from "
    "(or end pythonw.exe in Task Manager), then start again."
)


def _second_launch():
    """A copy is already running (the mutex, or the port answers): bring its window forward. When that copy was started from a
    DIFFERENT folder (a copy of the app on another drive) say so, because otherwise the launch looks like it did nothing. When it does
    not answer at all, keep asking for a few seconds (an impatient double-click on a copy that is still starting stays silent), and if
    it never does, say that too."""
    deadline = time.monotonic() + _SECOND_LAUNCH_WAIT_S
    answered, folder = _ping_show_window()
    while not answered and time.monotonic() < deadline:
        time.sleep(1)
        answered, folder = _ping_show_window()
    if not answered:
        _fatal_dialog(_with_diagnose(_STUCK_TEXT))
    elif not (folder and _same_folder(folder, _ROOT_DIR)):
        _fatal_dialog(_already_running_text(folder))


def _port_taken_text(error):
    port = server.APP_PORT
    holder = ""
    try:
        pid = server.listening_pid(port)
        image = server.process_image_path(pid) if pid else None
        if pid:
            holder = (os.path.basename(image) if image else "another program") + f" (process {pid})"
    except Exception:
        holder = ""
    if holder or "10048" in str(error):
        text = (f"Port {port} is in use by {holder or 'another program'}, so Personal Terminal cannot start. "
                "Close that program, or restart the PC, then start again.")
    else:
        text = (f"Personal Terminal cannot use port {port}: {error}\n"
                "Another program may be using it, or Windows has reserved it (common with Hyper-V, WSL or Docker). "
                "Close other programs, or restart the PC, then start again.")
    return _with_diagnose(text)


def main():
    if _ANOTHER_INSTANCE_RUNNING or server.already_running():
        print("Personal Terminal is already running - showing its window instead of starting a second copy.")
        _second_launch()
        return

    # Bind the API port FIRST, before anything is started: if something else
    # already holds it (a stray second copy, a headless server.py) we can say so
    # and stop without having begun loading a model or spawned a single child
    # process. (Requests aren't served until serve_forever starts below.)
    try:
        httpd = server.AppHTTPServer(("127.0.0.1", server.APP_PORT), server.Handler)
    except OSError as e:
        print(f"ERROR: could not start the server ({e})")
        _fatal_dialog(_port_taken_text(e))
        return
    server.httpd = httpd
    # The sound engine starts here (B13, FD-9): before the window, so fx.install picks it up; it renders its cues on its
    # own "fx-sound" thread while the backend starts. It reads its switches itself (LOCALAI_FX=0, data\fx_disabled,
    # LOCALAI_SFX=0 -> the silent NullEngine) and never stops the app from starting.
    try:
        kind, fx_raw = theme.load_fx_status()
        if kind == "err":
            # B13-H4: the file is there but could not be read (held without sharing by a backup, sync or antivirus
            # program past theme's start-up retries): the audible defaults must never play over a user who switched
            # sound off, so this session's engine stays silent (the latch; the next start reads the file again)
            print("fx sound: desktop_theme.json could not be read at start-up (held by another program?) - "
                  "sound off for this session")
        fx_sound.boot(fx_raw.get("sound"), silent=(kind == "err"))
    except Exception as e:  # noqa: BLE001 - sound is optional; the app starts without it
        print(f"sound engine not started ({e!r})")

    serving = False
    # Everything from here on is inside the try/finally, not just the window:
    # an exception while starting the embedding server (say) used to leave the
    # llama-server already started above running with no app, holding its VRAM.
    try:
        server.init_db()
        if not os.path.exists(server.SETTINGS_PATH):
            server.save_settings(server.DEFAULT_SETTINGS)
        # Before start_llama(), which reads the settings this may rewrite.
        server.migrate_settings()
        server.start_llama()
        server.start_embed_server()
        server.start_idle_restore_watcher()
        server.start_db_maintenance()

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        serving = True
        print(f"Personal Terminal backend running at http://127.0.0.1:{server.APP_PORT}")

        api = ApiClient(f"http://127.0.0.1:{server.APP_PORT}")
        window = MainWindow(server, api)
        window.mainloop()
    finally:
        server.stop_llama()
        # Covers window-closed-without-the-power-button (this finally runs
        # even then) - see server.shutdown()'s own matching comment for why
        # a resident image model needs explicit cleanup here too.
        server.stop_sd_server()
        server.stop_embed_server()
        if serving:
            # shutdown() waits for serve_forever() to exit - it would block
            # forever if that loop was never started.
            httpd.shutdown()
        httpd.server_close()
        # Folds the WAL back into chats.db and closes the pooled connections
        # (bounded - see server.close_db).
        server.close_db()
        try:
            fx_sound.shutdown(0.25)     # B13: the sound thread, joined for at most 0.25 s
        except Exception:  # noqa: BLE001
            pass
        print("Personal Terminal exited cleanly")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw has no console, so an unhandled error used to look like "I
        # double-clicked it and nothing happened". It's in the log either way;
        # this also says so on screen.
        import traceback

        traceback.print_exc()
        _fatal_dialog(_with_diagnose(
            "Personal Terminal hit an unexpected error and has to close.\n\n"
            "The details were written to data\\desktop.log."
        ))
        sys.exit(1)
