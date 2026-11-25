"""Console output helpers shared by the backup scripts.

ACC project and file names can contain characters outside the Windows ANSI
code page (box-drawing bars, invisible direction marks). When stdout is a pipe
or a file, Python on Windows encodes with that code page and print() raises on
such names, so every entry point switches its streams to UTF-8 first.
"""

import sys


def use_utf8_stdout() -> None:
    """Switch stdout and stderr to UTF-8, replacing anything unencodable.

    Safe to call more than once. Streams that cannot be reconfigured (already
    replaced by a test harness or an unusual transport) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            continue
