"""Native Windows OS file drag-and-drop for the app's Tk toplevel.

tkinter has no built-in drag-and-drop support, and this project stays
stdlib-only (no tkinterdnd2/pip - see the architecture memory on
portability), so this hooks the classic Win32 DragAcceptFiles/WM_DROPFILES
mechanism directly via ctypes - the same user32/shell32-via-ctypes
approach dev_screenshot.py already uses in this codebase, just installed
for the app's whole lifetime instead of a one-shot call.

This is the "simple" drop mechanism (DragAcceptFiles + WM_DROPFILES), not
the full OLE IDropTarget COM interface - it can't offer per-frame
DragEnter/DragOver hover feedback (WM_DROPFILES only fires once, at drop
time), but it doesn't require implementing a COM vtable in ctypes either.
Windows resolves a drop to the nearest ancestor window with a registered
drop target, so hooking only the toplevel HWND is enough to catch a drop
anywhere in the window, including over child widgets that never call
DragAcceptFiles themselves.

Windows-only. install() returns False (no-op) on any other platform or if
any ctypes call fails, so callers must treat native drop as best-effort
and offer another way to attach a file.
"""

import ctypes
import sys
import traceback
from ctypes import wintypes

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

# ctypes.wintypes' WPARAM/LPARAM have a history of not being pointer-sized
# on 64-bit builds - defined explicitly here instead of trusted, since a
# truncated HDROP or window-proc address is a silent-corruption crash
# waiting to happen, not a loud one.
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t
_WNDPROC_TYPE = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)

# Keyed by hwnd, kept alive for the process lifetime: (callback trampoline,
# original wndproc address). The trampoline MUST outlive every message
# dispatch or Windows ends up calling into freed memory the moment ctypes
# garbage-collects it - this dict is what prevents that.
_installed = {}


def install(tk_root, on_drop):
    """Make tk_root's toplevel accept OS file drops.

    on_drop(paths) is called with a list of absolute file paths. It runs on
    the Tk main thread - WM_DROPFILES arrives through the same Win32
    message pump Tk's own notifier already services (DispatchMessage calls
    our subclassed wndproc synchronously), so this is NOT a worker thread
    and none of the usual cross-thread .after() hazards apply here. Even
    so, on_drop must return quickly: it runs inside the message loop, so
    slow work (disk/network I/O) belongs in a thread it spawns, not inline.

    Returns True if the hook was installed, False if unavailable.
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = tk_root.winfo_id()
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32

        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = _LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragFinish.argtypes = [wintypes.HANDLE]

        def _wnd_proc(hwnd_, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    paths = _query_dropped_files(shell32, wparam)
                    if paths:
                        on_drop(paths)
                except Exception:
                    # Never let a drop-handling bug take down the window's
                    # message loop - log it (stdout/stderr are redirected
                    # to data/desktop.log in the packaged app) and move on.
                    traceback.print_exc()
                finally:
                    shell32.DragFinish(wparam)
                return 0
            old_addr = _installed[hwnd][1]
            return user32.CallWindowProcW(old_addr, hwnd_, msg, wparam, lparam)

        wndproc_ref = _WNDPROC_TYPE(_wnd_proc)
        new_addr = ctypes.cast(wndproc_ref, ctypes.c_void_p)
        old_addr = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, new_addr)
        if not old_addr:
            return False
        _installed[hwnd] = (wndproc_ref, old_addr)
        tk_root._win_dnd_hwnd = hwnd      # for uninstall(): the window may be gone by then, when winfo_id() can no longer be asked
        shell32.DragAcceptFiles(hwnd, True)
        return True
    except Exception:
        traceback.print_exc()
        return False


def _query_dropped_files(shell32, hdrop):
    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths = []
    for i in range(count):
        length = shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        shell32.DragQueryFileW(hdrop, i, buf, length + 1)
        paths.append(buf.value)
    return paths


def uninstall(tk_root):
    """Restore the original wndproc. Not needed for a normal app exit
    (destroy() means process exit, and the OS reclaims everything), but
    matters for any host that creates/destroys more than one such window
    in-process - e.g. a test harness, per the same concern already noted
    for the after()-loop cleanup in MainWindow.destroy()."""
    if sys.platform != "win32":
        return
    # After an abnormal teardown (the window destroyed from inside a <Destroy> handler, then this at exit) the Tk window is gone and
    # winfo_id() raises: the handle cached at install time is used instead and no Tk call is made, so this never prints a traceback
    # (it used to, at every such exit) and never leaves its entry behind.
    hwnd = getattr(tk_root, "_win_dnd_hwnd", None)
    if hwnd is None:
        try:
            hwnd = tk_root.winfo_id()
        except Exception:
            return
    entry = _installed.get(hwnd)
    if entry is None:
        return
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        if user32.IsWindow(hwnd):
            # The window still exists: put its own wndproc back. If that fails the trampoline is still what Windows calls, so it
            # must stay alive - the entry is kept.
            if not user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, entry[1]):
                return
            ctypes.windll.shell32.DragAcceptFiles(hwnd, False)
        # (A destroyed window is never called again, so its trampoline may go.)
        _installed.pop(hwnd, None)
        try:
            tk_root._win_dnd_hwnd = None
        except Exception:
            pass
    except Exception:
        traceback.print_exc()
