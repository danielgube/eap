from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .paths import EapPaths
from .util import load_json, require_fields, validate_id


@dataclass(frozen=True)
class CoreTool:
    id: str
    display_name: str
    root: Path
    executables: tuple[str, ...]
    publish_to_environment_path: bool

    def executable(self, name: str) -> Path:
        if name not in self.executables:
            raise ValidationError(
                f"{name!r} no está declarado por la herramienta core {self.id}"
            )
        executable = (self.root / name).resolve()
        try:
            executable.relative_to(self.root)
        except ValueError as exc:
            raise ValidationError(
                f"El ejecutable de {self.id} sale de su directorio"
            ) from exc
        if not executable.is_file():
            raise ValidationError(
                f"Falta el ejecutable core {self.id}/{name}: {executable}"
            )
        return executable

    def environment_path_entries(self) -> list[Path]:
        entries: list[Path] = []
        seen: set[str] = set()
        for name in self.executables:
            entry = self.executable(name).parent
            normalized = str(entry).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            entries.append(entry)
        return entries


@dataclass(frozen=True)
class CoreTools:
    paths: EapPaths
    definitions: dict[str, CoreTool]

    @classmethod
    def load(cls, paths: EapPaths) -> "CoreTools":
        manifest_path = paths.core / "core_tools.json"
        if not manifest_path.is_file():
            return cls(paths, {})
        value = load_json(manifest_path)
        require_fields(value, ("schemaVersion", "tools"), "herramientas core")
        if value["schemaVersion"] != 1:
            raise ValidationError(
                "schemaVersion de herramientas core no soportada"
            )
        entries = value["tools"]
        if not isinstance(entries, list):
            raise ValidationError("tools debe ser una lista en core_tools.json")
        definitions: dict[str, CoreTool] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("Herramienta core no válida")
            require_fields(
                entry,
                (
                    "id",
                    "displayName",
                    "directory",
                    "executables",
                    "publishToEnvironmentPath",
                ),
                "herramienta core",
            )
            tool_id = validate_id(str(entry["id"]), "id de herramienta core")
            if tool_id in definitions:
                raise ValidationError(f"Herramienta core duplicada: {tool_id}")
            if not isinstance(entry["displayName"], str):
                raise ValidationError(f"displayName no válido para {tool_id}")
            directory_text = entry["directory"]
            if not isinstance(directory_text, str) or not directory_text:
                raise ValidationError(f"directory no válido para {tool_id}")
            root = paths.require_within_root(paths.core / directory_text)
            try:
                root.relative_to(paths.core)
            except ValueError as exc:
                raise ValidationError(
                    f"La herramienta {tool_id} sale de core"
                ) from exc
            executables = entry["executables"]
            if not isinstance(executables, list) or not executables or not all(
                isinstance(name, str) and name
                for name in executables
            ):
                raise ValidationError(
                    f"executables no es válido para {tool_id}"
                )
            publish = entry["publishToEnvironmentPath"]
            if not isinstance(publish, bool):
                raise ValidationError(
                    f"publishToEnvironmentPath no es booleano para {tool_id}"
                )
            tool = CoreTool(
                id=tool_id,
                display_name=str(entry["displayName"]),
                root=root,
                executables=tuple(executables),
                publish_to_environment_path=publish,
            )
            for executable in tool.executables:
                tool.executable(executable)
            definitions[tool_id] = tool
        return cls(paths, definitions)

    def tool(self, tool_id: str) -> CoreTool:
        try:
            return self.definitions[tool_id]
        except KeyError as exc:
            raise ValidationError(
                f"Herramienta core desconocida: {tool_id}"
            ) from exc

    def environment_path_entries(self) -> list[Path]:
        entries: list[Path] = []
        seen: set[str] = set()
        for tool in self.definitions.values():
            if not tool.publish_to_environment_path:
                continue
            for entry in tool.environment_path_entries():
                normalized = str(entry).casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                entries.append(entry)
        return entries

    def as_json(self) -> list[dict[str, Any]]:
        return [
            {
                "id": tool.id,
                "displayName": tool.display_name,
                "root": str(tool.root),
                "executables": list(tool.executables),
                "publishedToPath": tool.publish_to_environment_path,
                "pathEntries": [
                    str(path)
                    for path in tool.environment_path_entries()
                ]
                if tool.publish_to_environment_path
                else [],
            }
            for tool in self.definitions.values()
        ]
