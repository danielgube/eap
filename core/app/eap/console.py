from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager


def get_console_title() -> str | None:
    """Return the native console title, or None when no console is available."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32.GetConsoleTitleW(buffer, len(buffer))
    return buffer.value


def set_console_title(title: str) -> bool:
    """Publish an application title for CMD and Windows Terminal tabs."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return False
    return bool(kernel32.SetConsoleTitleW(str(title)))


@contextmanager
def console_title(title: str) -> Iterator[None]:
    """Temporarily set a console title and restore its previous value."""
    previous = get_console_title()
    changed = set_console_title(title)
    try:
        yield
    finally:
        if changed and previous is not None:
            set_console_title(previous)


def _kernel32() -> object | None:
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
