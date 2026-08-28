from __future__ import annotations

import json
import os
from pathlib import Path
from types import TracebackType

from .errors import TransactionError
from .util import utc_now


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            detail = ""
            try:
                detail = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            suffix = f" ({detail})" if detail else ""
            raise TransactionError(
                f"Hay otra operación usando {self.path.name}{suffix}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "createdAt": utc_now()}, stream)
        self.acquired = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False
