"""Saving and downloading what the chat produced (tests/DOCUMENT_TOOL_SPEC.md P5, the file side): the user's real
Downloads folder from the Windows known-folder API, a name that never overwrites (`name (1).md`), and atomic writes.
Tk-free, so it runs on a worker thread (async_call.run_async) and in headless tests. LOCALAI_DOWNLOADS_DIR overrides
the Downloads folder (the tests inject a temp folder; the real one is never touched by them)."""
import ctypes
import os
import tempfile

DOWNLOADS_ENV = "LOCALAI_DOWNLOADS_DIR"
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
MAX_UNIQUE_TRIES = 1000


class SaveError(Exception):
    """A save that could not be done, with a reason the notice can show."""


def downloads_dir():
    """The user's Downloads folder: LOCALAI_DOWNLOADS_DIR if set, else SHGetKnownFolderPath(FOLDERID_Downloads) (never a
    hard-coded C:\\Users\\...), else ~/Downloads."""
    override = os.environ.get(DOWNLOADS_ENV)
    if override:
        return override
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", wintypes.BYTE * 8)]

        guid = GUID()
        ole32 = ctypes.WinDLL("ole32")
        shell32 = ctypes.WinDLL("shell32")
        ole32.CLSIDFromString(ctypes.c_wchar_p(FOLDERID_DOWNLOADS), ctypes.byref(guid))
        out = ctypes.c_wchar_p()
        shell32.SHGetKnownFolderPath.argtypes = (ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p))
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
            path = out.value
            ole32.CoTaskMemFree(out)
            return path
    except Exception:  # noqa: BLE001 - not Windows, or the call failed: the fallback below
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


def unique_path(folder, filename):
    """folder/filename, or folder/name (1).ext, (2)... - the first that does not exist (never overwrites silently)."""
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    n = 1
    while os.path.exists(candidate):
        if n > MAX_UNIQUE_TRIES:
            raise SaveError(f"too many files named {filename} already")
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def atomic_write_bytes(path, data):
    """Writes `data` to `path` through a temp file in the same folder + os.replace, so a failure (full disk, a
    read-only folder, a path too long, a folder that is gone) leaves no half-written file. Raises SaveError with
    the reason in plain words."""
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        raise SaveError(f"the folder {folder} does not exist")
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".part-", dir=folder)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except PermissionError:
        raise SaveError("the folder is read-only or the file is in use") from None
    except OSError as e:
        reason = (e.strerror or str(e)).strip().rstrip(".")
        if getattr(e, "errno", None) == 28 or "No space" in reason:
            reason = "the disk is full"
        elif getattr(e, "winerror", None) == 206 or "too long" in reason.lower():
            reason = "the path is too long"
        raise SaveError(reason) from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def download_bytes(filename, data, folder=None):
    """Writes `data` into the Downloads folder under `filename` (or `name (1).ext`...) -> the path written."""
    folder = folder or downloads_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise SaveError(f"the Downloads folder could not be used ({(e.strerror or str(e)).strip()})") from None
    return atomic_write_bytes(unique_path(folder, filename), data)


TEMP_FOLDER_NAME = "PersonalTerminal-open"
TEMP_EXTENSIONS = (".md", ".txt", ".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp")


def app_temp_dir():
    """The app's own folder under the system temp dir for the copies "View image" / "Open in..." hand to Windows -
    never next to the user's files; cleaned at the next start (clean_temp_copies)."""
    return os.path.join(tempfile.gettempdir(), TEMP_FOLDER_NAME)


def clean_temp_copies():
    """Removes last run's temp copies (called once at start-up); never raises, never touches anything else."""
    folder = app_temp_dir()
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def write_temp_copy(filename, data):
    """A temp copy of `data` under app_temp_dir() with a unique name; only document / image extensions (anything
    else becomes .txt) - the copy is handed to Windows to open, never run."""
    stem, ext = os.path.splitext(os.path.basename(str(filename or "file")))
    ext = ext.lower()
    if ext not in TEMP_EXTENSIONS:
        ext = ".txt"
    folder = app_temp_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise SaveError(f"the temp folder could not be made ({(e.strerror or str(e)).strip()})") from None
    return atomic_write_bytes(unique_path(folder, (stem or "file") + ext), data)


def open_with_default(path):
    """Opens `path` with the Windows default program for its type (an image: normally Photos). Not Windows: the
    reason comes back as a SaveError."""
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise SaveError("opening with the default program needs Windows")
    try:
        opener(path)
    except OSError as e:
        raise SaveError((e.strerror or str(e)).strip()) from None


def open_with_chooser(path):
    """The Windows "Open with" chooser for `path` (ShellExecuteW, verb openas)."""
    try:
        shell32 = ctypes.WinDLL("shell32")
        result = shell32.ShellExecuteW(None, "openas", path, None, None, 1)
    except (OSError, AttributeError) as e:
        raise SaveError(f"the Open with chooser is not available ({e})") from None
    if int(result) <= 32:
        raise SaveError(f"Windows refused to open the chooser (code {int(result)})")
    return int(result)


def filetypes_for(filename):
    """The Save dialog's file types for a document name."""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    md, txt, png = ("Markdown (*.md)", "*.md"), ("Text (*.txt)", "*.txt"), ("PNG image (*.png)", "*.png")
    allf = ("All files (*.*)", "*.*")
    if ext == ".txt":
        return [txt, md, allf]
    if ext == ".png":
        return [png, allf]
    return [md, txt, allf]
