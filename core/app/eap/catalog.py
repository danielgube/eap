from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ValidationError
from .paths import EapPaths
from .util import (
    load_json,
    require_fields,
    validate_id,
    version_belongs_to_track,
)


PROFILE_ENVIRONMENT_VARIABLES = {
    "appdata",
    "home",
    "homedrive",
    "homepath",
    "homeshare",
    "java_tool_options",
    "localappdata",
    "temp",
    "tmp",
    "tmpdir",
    "userprofile",
    "xdg_cache_home",
    "xdg_config_home",
    "xdg_data_home",
}


@dataclass(frozen=True)
class ComponentCatalogSource:
    id: str
    repository_url: str
    catalog_url: str
    revision: str
    source_type: str

    def as_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "repositoryUrl": self.repository_url,
            "catalogUrl": self.catalog_url,
            "revision": self.revision,
            "sourceType": self.source_type,
        }


@dataclass(frozen=True)
class ComponentDefinition:
    manifest_path: Path
    value: dict[str, Any]
    source: ComponentCatalogSource | None = None
    repository_manifest: str | None = None

    @property
    def id(self) -> str:
        return str(self.value["id"])

    @property
    def display_name(self) -> str:
        return str(self.value["displayName"])

    @property
    def kind(self) -> str:
        return str(self.value["kind"])

    @property
    def install_type(self) -> str:
        return str(self.value["install"].get("type", "archive"))

    @property
    def is_external(self) -> bool:
        return self.kind == "external"

    @property
    def information(self) -> dict[str, Any]:
        return {
            "description": self.information_description,
            "paths": self.important_paths,
        }

    @property
    def information_description(self) -> str:
        info = self.value.get("info")
        if isinstance(info, dict) and isinstance(
            info.get("description"), str
        ):
            return str(info["description"])
        description = self.value.get("description")
        if isinstance(description, str) and description.strip():
            return description.strip()
        return f"Componente {self.display_name}."

    @property
    def important_paths(self) -> list[dict[str, str]]:
        info = self.value.get("info")
        if isinstance(info, dict) and isinstance(info.get("paths"), list):
            result: list[dict[str, str]] = []
            for entry in info["paths"]:
                normalized = dict(entry)
                if normalized.get("type") not in {"directory", "file"}:
                    normalized["type"] = self._legacy_important_path_type(
                        str(normalized.get("base", "")),
                        str(normalized.get("relativePath", "")),
                    )
                result.append(normalized)
            return result
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        data = self.value.get("data", {})
        if isinstance(data, dict):
            for collection in ("directories", "files"):
                entries = data.get(collection, [])
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    raw_path = entry.get("path")
                    display_name = entry.get("displayName")
                    if not isinstance(raw_path, str) or not isinstance(
                        display_name, str
                    ):
                        continue
                    mapped = self._legacy_important_path(raw_path)
                    if mapped is None or mapped in seen:
                        continue
                    seen.add(mapped)
                    result.append(
                        {
                            "displayName": display_name,
                            "base": mapped[0],
                            "relativePath": mapped[1],
                            "type": (
                                "directory"
                                if collection == "directories"
                                else "file"
                            ),
                        }
                    )
        return result or [
            {
                "displayName": "Home del profile",
                "base": "profile",
                "relativePath": "home",
                "type": "directory",
            }
        ]

    def _legacy_important_path_type(
        self, base: str, relative_path: str
    ) -> str:
        data = self.value.get("data", {})
        if not isinstance(data, dict):
            return "directory"
        for collection, path_type in (
            ("directories", "directory"),
            ("files", "file"),
        ):
            entries = data.get(collection, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_path = entry.get("path")
                if not isinstance(raw_path, str):
                    continue
                mapped = self._legacy_important_path(raw_path)
                if mapped == (base, relative_path):
                    return path_type
        return "directory"

    def _legacy_important_path(
        self, template: str
    ) -> tuple[str, str] | None:
        prefixes = (
            ("{{profile.home}}", "profile", "home"),
            (
                "{{data.component}}",
                "profile",
                f"components/{self.id}",
            ),
            ("{{workspace.selected}}", "workspace", "."),
        )
        normalized = template.replace("\\", "/")
        for prefix, base, root in prefixes:
            if normalized == prefix:
                return base, root
            if normalized.startswith(prefix + "/"):
                suffix = normalized[len(prefix) + 1 :]
                return base, f"{root}/{suffix}" if root != "." else suffix
        return None

    @property
    def source_id(self) -> str:
        return self.source.id if self.source is not None else "builtin"

    def manifest_source(self) -> dict[str, str] | None:
        if self.source is None:
            return None
        return {
            **self.source.as_json(),
            "manifest": self.repository_manifest or self.manifest_path.name,
        }

    @property
    def providers(self) -> list[dict[str, Any]]:
        return list(self.value["providers"])

    @property
    def tracks(self) -> list[dict[str, Any]]:
        return list(self.value["tracks"])

    def provider(self, provider_id: str) -> dict[str, Any]:
        for provider in self.providers:
            if provider["id"] == provider_id:
                return provider
        raise ValidationError(
            f"Proveedor {provider_id!r} no definido para {self.id}"
        )

    def validate_track(self, track: int | str) -> int | str:
        requested = str(track)
        for item in self.tracks:
            if str(item["id"]) == requested:
                return item["id"]
        choices = ", ".join(str(item["id"]) for item in self.tracks)
        raise ValidationError(
            f"Línea {track} no soportada para {self.id}; disponibles: {choices}"
        )

    def compatible_track(
        self, locked_track: int | str, current_version: str
    ) -> int | str:
        try:
            return self.validate_track(locked_track)
        except ValidationError as original_error:
            candidates = [
                item["id"]
                for item in self.tracks
                if version_belongs_to_track(item["id"], current_version)
            ]
            default_track = self.value["defaultTrack"]
            if default_track in candidates:
                return default_track
            if candidates:
                return max(
                    candidates,
                    key=lambda value: len(
                        tuple(re.findall(r"\d+", str(value)))
                    ),
                )
            raise original_error


@dataclass(frozen=True)
class Catalog:
    paths: EapPaths
    value: dict[str, Any]
    definitions: dict[str, ComponentDefinition]
    sources: dict[str, ComponentCatalogSource] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: EapPaths) -> "Catalog":
        return cls.load_from_path(paths, paths.catalog)

    @classmethod
    def load_from_path(
        cls,
        paths: EapPaths,
        catalog_path: Path,
        source: ComponentCatalogSource | None = None,
    ) -> "Catalog":
        catalog_path = paths.require_within_root(catalog_path)
        value = load_json(catalog_path)
        require_fields(value, ("schemaVersion", "catalogVersion", "components"), "catálogo")
        if value["schemaVersion"] != 1:
            raise ValidationError(
                f"schemaVersion de catálogo no soportada: {value['schemaVersion']}"
            )
        entries = value["components"]
        if not isinstance(entries, list):
            raise ValidationError("components debe ser una lista en el catálogo")

        definitions: dict[str, ComponentDefinition] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("Entrada de catálogo no válida")
            require_fields(entry, ("id", "manifest"), "entrada de catálogo")
            component_id = validate_id(str(entry["id"]), "id de componente")
            manifest_name = str(entry["manifest"])
            manifest_path = paths.require_within_root(
                catalog_path.parent / manifest_name
            )
            try:
                manifest_path.relative_to(catalog_path.parent)
            except ValueError as exc:
                raise ValidationError(
                    f"El manifiesto sale de su catálogo: {manifest_path}"
                ) from exc
            manifest = load_json(manifest_path)
            cls._validate_component(manifest, component_id, manifest_path)
            if component_id in definitions:
                raise ValidationError(f"Componente duplicado: {component_id}")
            definitions[component_id] = ComponentDefinition(
                manifest_path,
                manifest,
                source,
                manifest_name.replace("\\", "/"),
            )
        sources = {source.id: source} if source is not None else {}
        return cls(paths, value, definitions, sources)

    @staticmethod
    def _validate_component(
        value: dict[str, Any], expected_id: str, path: Path
    ) -> None:
        require_fields(
            value,
            (
                "schemaVersion",
                "id",
                "displayName",
                "kind",
                "launchers",
                "tracks",
                "providers",
                "defaultProvider",
                "defaultTrack",
                "updatePolicy",
                "install",
                "environment",
            ),
            str(path),
        )
        if value["schemaVersion"] not in {1, 2, 3}:
            raise ValidationError(f"Schema de componente no soportado en {path}")
        if value["id"] != expected_id:
            raise ValidationError(
                f"El id {value['id']!r} no coincide con {expected_id!r} en {path}"
            )
        if value["kind"] not in {
            "application",
            "external",
            "runtime",
            "service",
            "tool",
        }:
            raise ValidationError(
                f"Tipo de componente no soportado en {path}: {value['kind']!r}"
            )
        if value["schemaVersion"] in {2, 3}:
            require_fields(value, ("info",), str(path))
        info = value.get("info")
        if info is not None:
            Catalog._validate_component_info(
                info,
                expected_id,
                path,
                require_path_type=value["schemaVersion"] == 3,
            )
        if not isinstance(value["launchers"], list):
            raise ValidationError(f"launchers debe ser una lista en {path}")
        launcher_ids: set[str] = set()
        for launcher in value["launchers"]:
            if not isinstance(launcher, dict):
                raise ValidationError(
                    f"Launcher no válido en {path}"
                )
            require_fields(
                launcher,
                (
                    "id",
                    "displayName",
                    "type",
                    "workspaceMode",
                    "executable",
                    "arguments",
                    "startMode",
                ),
                f"launcher de {expected_id}",
            )
            launcher_id = validate_id(str(launcher["id"]), "id de launcher")
            if launcher_id in launcher_ids:
                raise ValidationError(
                    f"Launcher duplicado {launcher_id!r} en {path}"
                )
            launcher_ids.add(launcher_id)
            if not isinstance(launcher["displayName"], str):
                raise ValidationError(
                    f"displayName de launcher no válido en {path}"
                )
            if launcher["type"] not in {"application", "command"}:
                raise ValidationError(
                    f"Tipo de launcher no válido en {path}"
                )
            if launcher["workspaceMode"] not in {
                "environment",
                "component-data",
            }:
                raise ValidationError(
                    f"Launcher sin workspaceMode válido en {path}"
                )
            if not isinstance(launcher["executable"], str):
                raise ValidationError(
                    f"Ejecutable de launcher no válido en {path}"
                )
            if not isinstance(launcher["arguments"], list) or not all(
                isinstance(argument, str)
                for argument in launcher["arguments"]
            ):
                raise ValidationError(
                    f"Argumentos de launcher no válidos en {path}"
                )
            if launcher["startMode"] not in {"detached", "wait"}:
                raise ValidationError(
                    f"startMode de launcher no válido en {path}"
                )
            if (
                value["kind"] == "external"
                and launcher["executable"] != "{{external.executable}}"
            ):
                raise ValidationError(
                    "Un launcher externo debe usar "
                    f"{{{{external.executable}}}} en {path}"
                )
            launcher_environment = launcher.get("environment", {})
            if not isinstance(launcher_environment, dict) or not all(
                isinstance(name, str)
                and name
                and "=" not in name
                and isinstance(template, str)
                for name, template in launcher_environment.items()
            ):
                raise ValidationError(
                    f"Entorno de launcher no válido en {path}"
                )
            profile_overrides = sorted(
                name
                for name in launcher_environment
                if name.casefold() in PROFILE_ENVIRONMENT_VARIABLES
            )
            if profile_overrides:
                names = ", ".join(profile_overrides)
                raise ValidationError(
                    f"El launcher {launcher_id} redefine variables reservadas "
                    f"del profile ({names}) en {path}"
                )
            launcher_unset = launcher.get("unset", [])
            if not isinstance(launcher_unset, list) or not all(
                isinstance(name, str) and name and "=" not in name
                for name in launcher_unset
            ):
                raise ValidationError(
                    f"unset de launcher no válido en {path}"
                )
            profile_unsets = sorted(
                name
                for name in launcher_unset
                if name.casefold() in PROFILE_ENVIRONMENT_VARIABLES
            )
            if profile_unsets:
                names = ", ".join(profile_unsets)
                raise ValidationError(
                    f"El launcher {launcher_id} elimina variables reservadas "
                    f"del profile ({names}) en {path}"
                )
            data_directories = launcher.get("dataDirectories", [])
            if not isinstance(data_directories, list) or not all(
                isinstance(template, str) for template in data_directories
            ):
                raise ValidationError(
                    f"dataDirectories de launcher no válido en {path}"
                )
            data_copies = launcher.get("dataCopies", [])
            if not isinstance(data_copies, list):
                raise ValidationError(
                    f"dataCopies de launcher no válido en {path}"
                )
            for data_copy in data_copies:
                if (
                    not isinstance(data_copy, dict)
                    or not isinstance(data_copy.get("source"), str)
                    or not isinstance(data_copy.get("target"), str)
                    or data_copy.get("mode") != "if-missing"
                ):
                    raise ValidationError(
                        f"Copia mutable de launcher no válida en {path}"
                    )
        tracks = value["tracks"]
        if not isinstance(tracks, list) or not tracks:
            raise ValidationError(f"El componente no tiene líneas en {path}")
        track_ids: set[str] = set()
        for track in tracks:
            if (
                not isinstance(track, dict)
                or not isinstance(track.get("id"), (int, str))
                or isinstance(track.get("id"), bool)
                or not str(track.get("id"))
                or not isinstance(track.get("displayName"), str)
            ):
                raise ValidationError(f"Línea no válida en {path}")
            normalized_track = str(track["id"])
            if normalized_track in track_ids:
                raise ValidationError(
                    f"Línea duplicada {normalized_track!r} en {path}"
                )
            track_ids.add(normalized_track)
        if not isinstance(value["providers"], list) or not value["providers"]:
            raise ValidationError(f"El componente no tiene proveedores en {path}")
        install = value["install"]
        if not isinstance(install, dict):
            raise ValidationError(f"install debe ser un objeto en {path}")
        install_type = install.get("type", "archive")
        if value["kind"] == "external":
            require_fields(
                install,
                ("type", "executableNames", "prompt"),
                f"install externo de {expected_id}",
            )
            executable_names = install["executableNames"]
            if (
                install_type != "external-executable"
                or not isinstance(executable_names, list)
                or not executable_names
                or not all(
                    isinstance(name, str)
                    and name
                    and Path(name).name == name
                    and name.casefold().endswith(".exe")
                    for name in executable_names
                )
                or not isinstance(install["prompt"], str)
                or not install["prompt"]
            ):
                raise ValidationError(
                    f"install externo no válido en {path}"
                )
            if not value["launchers"]:
                raise ValidationError(
                    f"El componente externo no tiene launcher en {path}"
                )
        else:
            require_fields(
                install,
                (
                    "directoryTemplate",
                    "requiredFiles",
                    "validation",
                ),
                f"install de {expected_id}",
            )
            if install_type != "archive":
                raise ValidationError(
                    f"install.type no soportado en {path}: {install_type!r}"
                )
            if not isinstance(install["directoryTemplate"], str):
                raise ValidationError(
                    f"install.directoryTemplate no es válido en {path}"
                )
            if not isinstance(install["requiredFiles"], list) or not all(
                isinstance(item, str) for item in install["requiredFiles"]
            ):
                raise ValidationError(
                    "install.requiredFiles debe ser una lista de textos "
                    f"en {path}"
                )
            max_extract_bytes = install.get("maxExtractBytes")
            if max_extract_bytes is not None and (
                not isinstance(max_extract_bytes, int)
                or isinstance(max_extract_bytes, bool)
                or max_extract_bytes < 1
            ):
                raise ValidationError(
                    f"install.maxExtractBytes no es válido en {path}"
                )
            validation = install["validation"]
            if not isinstance(validation, dict) or validation.get("type") not in {
                "java-release",
                "command",
                "eclipse-package",
                "files-only",
            }:
                raise ValidationError(
                    f"install.validation no es válida en {path}"
                )
        environment = value["environment"]
        if not isinstance(environment, dict):
            raise ValidationError(f"environment debe ser un objeto en {path}")
        variables = environment.get("variables")
        unset_variables = environment.get("unset", [])
        path_entries = environment.get("path")
        data_path_entries = environment.get("dataPath", [])
        commands = environment.get("commands", [])
        if not isinstance(variables, dict):
            raise ValidationError(
                f"environment.variables debe ser un objeto en {path}"
            )
        for name, template in variables.items():
            if (
                not isinstance(name, str)
                or not name
                or "=" in name
                or not isinstance(template, str)
            ):
                raise ValidationError(
                    f"Variable de entorno no válida en {path}"
                )
        if not isinstance(unset_variables, list) or not all(
            isinstance(name, str) and name and "=" not in name
            for name in unset_variables
        ):
            raise ValidationError(
                f"environment.unset debe ser una lista de variables en {path}"
            )
        if not isinstance(path_entries, list) or not all(
            isinstance(item, str) for item in path_entries
        ):
            raise ValidationError(
                f"environment.path debe ser una lista de textos en {path}"
            )
        if not isinstance(data_path_entries, list) or not all(
            isinstance(item, str) for item in data_path_entries
        ):
            raise ValidationError(
                f"environment.dataPath debe ser una lista de textos en {path}"
            )
        if not isinstance(commands, list):
            raise ValidationError(
                f"environment.commands debe ser una lista en {path}"
            )
        for command in commands:
            if (
                not isinstance(command, dict)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                    str(command.get("name", "")),
                )
                or not isinstance(command.get("executable"), str)
                or not isinstance(command.get("arguments"), list)
                or not all(
                    isinstance(argument, str)
                    and re.fullmatch(r"[A-Za-z0-9._+-]+", argument)
                    for argument in command["arguments"]
                )
            ):
                raise ValidationError(
                    f"Comando de entorno no válido en {path}"
                )
            validate_id(str(command["name"]), "nombre de comando")
        data = value.get("data", {})
        if not isinstance(data, dict):
            raise ValidationError(f"data debe ser un objeto en {path}")
        data_roles = {
            "cache",
            "commands",
            "configuration",
            "data",
            "extensions",
            "repository",
            "workspace",
        }
        for collection in ("directories", "files"):
            entries = data.get(collection, [])
            if not isinstance(entries, list):
                raise ValidationError(
                    f"data.{collection} debe ser una lista en {path}"
                )
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValidationError(
                        f"Entrada no válida en data.{collection} de {path}"
                    )
                require_fields(
                    entry,
                    ("path", "displayName", "role"),
                    f"data.{collection} de {expected_id}",
                )
                if (
                    not isinstance(entry["path"], str)
                    or not entry["path"]
                    or not isinstance(entry["displayName"], str)
                    or not entry["displayName"]
                    or entry["role"] not in data_roles
                    or not isinstance(entry.get("showInDashboard", True), bool)
                ):
                    raise ValidationError(
                        f"Entrada no válida en data.{collection} de {path}"
                    )
                if collection == "files":
                    if (
                        entry.get("mode")
                        not in {"if-missing", "merge-properties"}
                        or not isinstance(entry.get("content"), str)
                        or len(entry["content"]) > 1024 * 1024
                    ):
                        raise ValidationError(
                            f"Archivo administrado no válido en {path}"
                        )
        requirements = value.get("requires", [])
        if not isinstance(requirements, list):
            raise ValidationError(f"requires debe ser una lista en {path}")
        for requirement in requirements:
            if (
                not isinstance(requirement, dict)
                or not isinstance(requirement.get("capability"), str)
                or not isinstance(requirement.get("minimumTrack"), int)
            ):
                raise ValidationError(
                    f"Dependencia no válida en {path}"
                )
        provider_ids: set[str] = set()
        for provider in value["providers"]:
            require_fields(
                provider,
                ("id", "componentId", "displayName", "resolver", "verification"),
                f"proveedor de {expected_id}",
            )
            provider_id = validate_id(str(provider["id"]), "id de proveedor")
            if provider_id in provider_ids:
                raise ValidationError(
                    f"Proveedor duplicado {provider_id!r} en {path}"
                )
            provider_ids.add(provider_id)
            resolver = provider["resolver"]
            verification = provider["verification"]
            if not isinstance(resolver, dict) or not isinstance(
                verification, dict
            ):
                raise ValidationError(
                    f"Proveedor no válido {provider_id!r} en {path}"
                )
            if (
                value["kind"] == "external"
                and resolver.get("type") != "external-executable"
            ):
                raise ValidationError(
                    f"Resolver externo no válido para {provider_id!r} en {path}"
                )
        if str(value["defaultProvider"]) not in provider_ids:
            raise ValidationError(
                f"Proveedor predeterminado no definido en {path}: "
                f"{value['defaultProvider']!r}"
            )
        if str(value["defaultTrack"]) not in track_ids:
            raise ValidationError(
                f"Línea predeterminada no definida en {path}: "
                f"{value['defaultTrack']!r}"
            )
        if value["updatePolicy"] not in {"manual", "same-track"}:
            raise ValidationError(
                f"Política de actualización no válida en {path}: "
                f"{value['updatePolicy']!r}"
            )

    @staticmethod
    def _validate_component_info(
        info: Any,
        expected_id: str,
        path: Path,
        require_path_type: bool = False,
    ) -> None:
        if not isinstance(info, dict):
            raise ValidationError(f"info debe ser un objeto en {path}")
        require_fields(
            info,
            ("description", "paths"),
            f"info de {expected_id}",
        )
        description = info["description"]
        if (
            not isinstance(description, str)
            or not description.strip()
            or description != description.strip()
            or len(description) > 400
            or len(re.findall(r"[.!?](?=\s|$)", description)) > 3
        ):
            raise ValidationError(
                f"info.description debe contener entre una y tres frases "
                f"breves en {path}"
            )
        important_paths = info["paths"]
        if not isinstance(important_paths, list) or not important_paths:
            raise ValidationError(
                f"info.paths debe contener al menos una ruta en {path}"
            )
        seen_important_paths: set[tuple[str, str]] = set()
        for important_path in important_paths:
            if not isinstance(important_path, dict):
                raise ValidationError(
                    f"Ruta no válida en info.paths de {path}"
                )
            required_fields = ["displayName", "base", "relativePath"]
            if require_path_type:
                required_fields.append("type")
            require_fields(
                important_path,
                tuple(required_fields),
                f"info.paths de {expected_id}",
            )
            display_name = important_path["displayName"]
            base = important_path["base"]
            relative_path = important_path["relativePath"]
            path_type = important_path.get("type")
            if (
                not isinstance(display_name, str)
                or not display_name.strip()
                or display_name != display_name.strip()
                or base not in {"profile", "workspace"}
                or not isinstance(relative_path, str)
                or not relative_path
                or "\\" in relative_path
                or path_type not in {None, "directory", "file"}
            ):
                raise ValidationError(
                    f"Ruta no válida en info.paths de {path}"
                )
            relative = PurePosixPath(relative_path)
            if (
                relative.is_absolute()
                or relative_path != relative.as_posix()
                or (
                    relative_path != "."
                    and any(part in {"", ".", ".."} for part in relative.parts)
                )
            ):
                raise ValidationError(
                    f"info.paths sólo admite rutas relativas seguras en {path}: "
                    f"{relative_path!r}"
                )
            key = (str(base), relative_path.casefold())
            if key in seen_important_paths:
                raise ValidationError(
                    f"Ruta duplicada en info.paths de {path}: "
                    f"{relative_path!r}"
                )
            seen_important_paths.add(key)

    def component(self, component_id: str) -> ComponentDefinition:
        try:
            return self.definitions[component_id]
        except KeyError as exc:
            raise ValidationError(f"Componente desconocido: {component_id}") from exc
