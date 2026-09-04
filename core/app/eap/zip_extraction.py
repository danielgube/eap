from __future__ import annotations

import stat
import zipfile
from pathlib import Path, PurePosixPath

from .errors import IntegrityError


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_zip_archive(
    archive: Path,
    destination: Path,
    maximum_bytes: int,
    maximum_ratio: int,
) -> None:
    total = 0
    with zipfile.ZipFile(archive, "r") as source:
        for entry in source.infolist():
            relative = validate_zip_entry(entry)
            total += entry.file_size
            if total > maximum_bytes:
                raise IntegrityError(
                    "El tamaño extraído supera install.maxExtractBytes"
                )
            if entry.file_size and entry.compress_size == 0:
                raise IntegrityError(
                    f"Entrada ZIP con ratio inválido: {entry.filename}"
                )
            if (
                entry.compress_size
                and entry.file_size / entry.compress_size > maximum_ratio
            ):
                raise IntegrityError(
                    "Entrada ZIP supera el ratio permitido: "
                    f"{entry.filename}"
                )
            target = destination.joinpath(*relative.parts)
            ensure_destination(destination, target)


def verify_extracted_zip(archive: Path, destination: Path) -> None:
    """Ensure no extracted entry disappeared or changed size."""
    with zipfile.ZipFile(archive, "r") as source:
        for entry in source.infolist():
            relative = validate_zip_entry(entry)
            target = destination.joinpath(*relative.parts)
            ensure_destination(destination, target)
            try:
                if entry.is_dir():
                    if not target.is_dir():
                        raise IntegrityError(
                            "Falta un directorio tras la extracción: "
                            f"{entry.filename}"
                        )
                    continue
                extracted_size = target.stat().st_size
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise IntegrityError(
                    "Falta un archivo tras la extracción: "
                    f"{entry.filename}. Un antivirus u otro proceso puede "
                    "haberlo eliminado"
                ) from exc
            except OSError as exc:
                raise IntegrityError(
                    "No se puede comprobar el archivo extraído "
                    f"{entry.filename}: {exc}"
                ) from exc
            if extracted_size != entry.file_size:
                raise IntegrityError(
                    "El tamaño del archivo extraído no coincide: "
                    f"{entry.filename}"
                )


def validate_zip_entry(entry: zipfile.ZipInfo) -> PurePosixPath:
    raw_name = entry.filename.replace("\\", "/")
    relative = PurePosixPath(raw_name)
    if relative.is_absolute() or not relative.parts:
        raise IntegrityError(f"Ruta ZIP absoluta o vacía: {entry.filename}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise IntegrityError(f"Path traversal en ZIP: {entry.filename}")
    for part in relative.parts:
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED or part.endswith((" ", ".")):
            raise IntegrityError(
                f"Nombre no permitido en Windows: {entry.filename}"
            )
        if ":" in part:
            raise IntegrityError(f"Ruta ZIP con unidad: {entry.filename}")
    mode = entry.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise IntegrityError(f"Enlace simbólico no permitido: {entry.filename}")
    return relative


def ensure_destination(root: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise IntegrityError(f"La extracción sale de staging: {target}") from exc

