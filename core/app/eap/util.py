from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ValidationError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as exc:
        raise ValidationError(f"No existe el archivo JSON: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"JSON inválido en {path}, línea {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ValidationError(f"Se esperaba un objeto JSON en {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def hash_file(
    path: Path,
    algorithm: str,
    chunk_size: int = 1024 * 1024,
) -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise ValidationError(
            f"Algoritmo de hash no soportado: {algorithm}"
        ) from exc
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    return hash_file(path, "sha256", chunk_size)


def version_key(version: str) -> tuple[int, ...]:
    numbers = tuple(int(value) for value in re.findall(r"\d+", version))
    if not numbers:
        raise ValidationError(f"Versión no comparable: {version}")
    return numbers


def version_belongs_to_track(track: int | str, version: str) -> bool:
    version_numbers = tuple(
        int(value) for value in re.findall(r"\d+", version)
    )
    track_numbers = tuple(
        int(value) for value in re.findall(r"\d+", str(track))
    )
    return bool(
        version_numbers
        and track_numbers
        and version_numbers[: len(track_numbers)] == track_numbers
    )


def java_version_key(version: str, provider: str) -> tuple[int, ...]:
    numbers = [int(value) for value in re.findall(r"\d+", version)]
    if not numbers:
        raise ValidationError(f"Versión Java no comparable: {version}")
    if "+" in version:
        release_text, build_text = version.split("+", 1)
        release = [int(value) for value in re.findall(r"\d+", release_text)]
        build = [int(value) for value in re.findall(r"\d+", build_text)]
        release.extend([0] * max(0, 4 - len(release)))
        return tuple([*release[:4], *build])
    if provider == "corretto" and len(numbers) >= 4:
        return tuple([*numbers[:3], 0, *numbers[3:]])
    return tuple(numbers)


def component_version_key(
    component_id: str,
    version: str,
    provider: str,
) -> tuple[int, ...]:
    if component_id == "java":
        return java_version_key(version, provider)
    return version_key(version)


def validate_id(value: str, label: str = "identificador") -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValidationError(f"{label.capitalize()} no válido: {value!r}")
    if value.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        raise ValidationError(f"{label.capitalize()} reservado en Windows: {value}")
    return value


def validate_version(value: str) -> str:
    if not _SAFE_VERSION.fullmatch(value):
        raise ValidationError(f"Versión no válida para una ruta: {value!r}")
    return value


def require_fields(
    value: dict[str, Any], fields: tuple[str, ...], context: str
) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ValidationError(
            f"Faltan campos en {context}: {', '.join(missing)}"
        )
