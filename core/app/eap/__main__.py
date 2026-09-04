from .console_log import capture_console_output
from .paths import EapPaths


if __name__ == "__main__":
    with capture_console_output(EapPaths.discover().logs):
        from .cli import main

        raise SystemExit(main())
