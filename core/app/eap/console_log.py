from __future__ import annotations

import faulthandler
import os
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, TextIO


_ANSI_SEQUENCE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _TeeStream:
    def __init__(
        self,
        original: TextIO | None,
        log: TextIO,
        lock: threading.Lock,
    ):
        self.original = original
        self.log = log
        self.lock = lock

    def write(self, value: str) -> int:
        with self.lock:
            written = (
                self.original.write(value)
                if self.original is not None
                else len(value)
            )
            try:
                self.log.write(_ANSI_SEQUENCE.sub("", value))
                self.log.flush()
            except OSError:
                pass
        return len(value) if written is None else written

    def flush(self) -> None:
        with self.lock:
            if self.original is not None:
                self.original.flush()
            try:
                self.log.flush()
            except OSError:
                pass

    def isatty(self) -> bool:
        return bool(self.original and self.original.isatty())

    def fileno(self) -> int:
        if self.original is None:
            raise OSError("La salida original no tiene descriptor")
        return self.original.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self.original, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self.original, "errors", None) or "strict"

    def __getattr__(self, name: str) -> Any:
        if self.original is None:
            raise AttributeError(name)
        return getattr(self.original, name)


@contextmanager
def capture_console_output(log_directory: Path) -> Iterator[Path | None]:
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().astimezone()
        file_name = (
            f"eap-{started_at:%Y%m%d-%H%M%S}-{os.getpid()}.log"
        )
        log_path = log_directory / file_name
        log = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        yield None
        return

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    lock = threading.Lock()
    enabled_faulthandler = False
    log.write(
        f"=== EAP iniciado {started_at.isoformat(timespec='seconds')} "
        f"· PID {os.getpid()} ===\n"
    )
    log.flush()
    try:
        if not faulthandler.is_enabled():
            faulthandler.enable(file=log, all_threads=True)
            enabled_faulthandler = True
    except (OSError, RuntimeError):
        pass

    sys.stdout = _TeeStream(original_stdout, log, lock)
    sys.stderr = _TeeStream(original_stderr, log, lock)
    try:
        yield log_path
    except Exception:
        log.write("\n=== Excepción no controlada ===\n")
        traceback.print_exc(file=log)
        log.flush()
        raise
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
        if enabled_faulthandler:
            faulthandler.disable()
        try:
            finished_at = datetime.now().astimezone()
            log.write(
                f"=== EAP finalizado "
                f"{finished_at.isoformat(timespec='seconds')} ===\n"
            )
            log.flush()
        finally:
            log.close()
