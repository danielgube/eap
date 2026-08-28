from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .paths import EapPaths
from .util import load_json, require_fields, validate_id


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
class ComponentDefinition:
    manifest_path: Path
    value: dict[str, Any]

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


@dataclass(frozen=True)
class Catalog:
    paths: EapPaths
    value: dict[str, Any]
    definitions: dict[str, ComponentDefinition]

    @classmethod
    def load(cls, paths: EapPaths) -> "Catalog":
        value = load_json(paths.catalog)
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
            manifest_path = paths.require_within_root(
                paths.catalog.parent / str(entry["manifest"])
            )
            try:
                manifest_path.relative_to(paths.catalog.parent)
            except ValueError as exc:
                raise ValidationError(
                    f"El manifiesto sale de core/catalog: {manifest_path}"
                ) from exc
            manifest = load_json(manifest_path)
            cls._validate_component(manifest, component_id, manifest_path)
            if component_id in definitions:
                raise ValidationError(f"Componente duplicado: {component_id}")
            definitions[component_id] = ComponentDefinition(manifest_path, manifest)
        return cls(paths, value, definitions)

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
                "install",
                "environment",
            ),
            str(path),
        )
        if value["schemaVersion"] != 1:
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
            validation = install["validation"]
            if not isinstance(validation, dict) or validation.get("type") not in {
                "java-release",
                "command",
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
                        entry.get("mode") != "if-missing"
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

    def component(self, component_id: str) -> ComponentDefinition:
        try:
            return self.definitions[component_id]
        except KeyError as exc:
            raise ValidationError(f"Componente desconocido: {component_id}") from exc
