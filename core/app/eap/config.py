from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError

DEFAULTS: dict[str, str] = {
    "profile.default": "default",
    "environment.default": "default",
    "network.timeoutSeconds": "20",
    "update.checkOnStartup": "true",
    "update.checkIntervalHours": "24",
    "download.keepArchives": "true",
    "install.maxExtractBytes": str(2 * 1024 * 1024 * 1024),
    "install.maxCompressionRatio": "200",
    "transfer.maxExtractBytes": str(50 * 1024 * 1024 * 1024),
    "transfer.maxFiles": "500000",
    "pocketools.repository.danielgube": (
        "https://github.com/danielgube/eap-pocketools"
    ),
}


def load_properties(path: Path) -> dict[str, str]:
    """Load exactly the properties declared in *path*, without defaults."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "!")):
                continue
            if "=" not in line:
                raise ValidationError(
                    f"Propiedad inválida en {path}:{line_number}"
                )
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key:
                raise ValidationError(
                    f"Clave vacía en {path}:{line_number}"
                )
            if key in values:
                raise ValidationError(
                    f"Propiedad duplicada {key!r} en {path}:{line_number}"
                )
            values[key] = raw_value.strip()
    return values


@dataclass(frozen=True)
class Settings:
    values: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Settings":
        declared = load_properties(path)
        if (
            "profile.default" not in declared
            and "environment.default" in declared
        ):
            declared["profile.default"] = declared["environment.default"]
        if (
            "environment.default" not in declared
            and "profile.default" in declared
        ):
            declared["environment.default"] = declared["profile.default"]
        values = dict(DEFAULTS)
        values.update(declared)
        return cls(values)

    def get(self, key: str) -> str:
        try:
            return self.values[key]
        except KeyError as exc:
            raise ValidationError(f"Configuración requerida ausente: {key}") from exc

    def get_bool(self, key: str) -> bool:
        value = self.get(key).lower()
        if value in {"true", "yes", "1", "on"}:
            return True
        if value in {"false", "no", "0", "off"}:
            return False
        raise ValidationError(f"Booleano inválido para {key}: {value!r}")

    def get_int(self, key: str, minimum: int = 0) -> int:
        raw_value = self.get(key)
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValidationError(
                f"Entero inválido para {key}: {raw_value!r}"
            ) from exc
        if value < minimum:
            raise ValidationError(
                f"{key} debe ser mayor o igual que {minimum}"
            )
        return value
