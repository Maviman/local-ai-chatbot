"""The end of a log file, without reading all of it.

The Home screen's boot-log strip (data/desktop.log - append-only, so it only ever grows) and the model-load overlay
(data/llama-server.log, rewritten by every load) both showed "the last few lines" by opening the file and calling
readlines() - the whole file - on the Tk thread, the overlay every 500 ms. Both files live on the disk a model load is
saturating, so that read could stall the window for exactly as long as the load kept the disk busy.

read_log_tail() reads only the final max_bytes. It is meant to be called from a worker thread (see async_call.Latest);
it never raises."""

import os


def read_log_tail(path, max_lines, max_bytes=65536):
    """The last `max_lines` lines of the file at `path`, each with its line ending - or [] when there is nothing to read
    (missing, unreadable, empty). Undecodable bytes are replaced, never an error. When the read starts part-way into the
    file its first line is cut off mid-way, so it is dropped rather than shown torn."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-max_lines:]
