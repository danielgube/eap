from __future__ import annotations

import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from .cli import main as cli_main
from .paths import EapPaths
from .util import utc_now, validate_id


def run(arguments: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    if len(values) != 2:
        return 2
    try:
        launcher_id = validate_id(values[0], "id de launcher")
        environment_id = validate_id(values[1], "id de profile")
    except Exception:
        return 2

    paths = EapPaths.discover()
    paths.ensure_layout()
    log_path = _log_path(paths, environment_id, launcher_id)
    exit_code = 1
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(f"EAP shortcut · {utc_now()}")
            print(f"Profile: {environment_id}")
            print(f"Launcher: {launcher_id}")
            print()
            try:
                exit_code = cli_main(
                    [
                        "launch",
                        launcher_id,
                        "--env",
                        environment_id,
                    ]
                )
            except BaseException:
                traceback.print_exc()
                exit_code = 1
            if exit_code != 0:
                print(f"\nEAP terminó con código {exit_code}.")

    if exit_code == 0:
        log_path.unlink(missing_ok=True)
    return exit_code


def _log_path(
    paths: EapPaths, environment_id: str, launcher_id: str
) -> Path:
    directory = paths.temp / "logs" / "shortcuts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{environment_id}-{launcher_id}.log"


if __name__ == "__main__":
    raise SystemExit(run())
