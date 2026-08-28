from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from uuid import uuid4

from .config import Settings
from .core_tools import CoreTools
from .errors import IntegrityError, TransactionError, ValidationError
from .locks import FileLock
from .network import HttpClient
from .paths import EapPaths
from .util import (
    atomic_write_json,
    atomic_write_text,
    load_json,
    require_fields,
    sha256_file,
    utc_now,
    validate_id,
    validate_version,
)

_REPOSITORY_PREFIX = "pocketools.repository."
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_COMMAND_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_COMMAND_TYPES = {"cmd", "powershell", "exe", "java-jar", "python", "node"}
_RESERVED_COMMANDS = {"cmd", "eap", "powershell", "pwsh"}
_WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def semver_key(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if not match:
        raise ValidationError(
            f"La versión de Pocketool debe usar MAJOR.MINOR.PATCH: {version!r}"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


@dataclass(frozen=True)
class PocketToolSource:
    id: str
    repository_url: str
    catalog_url: str
    source_type: str

    def as_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "repositoryUrl": self.repository_url,
            "catalogUrl": self.catalog_url,
            "sourceType": self.source_type,
        }


@dataclass(frozen=True)
class PocketToolDefinition:
    source: PocketToolSource
    value: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.value["id"])

    @property
    def name(self) -> str:
        return str(self.value["name"])

    @property
    def version(self) -> str:
        return str(self.value["version"])

    @property
    def selector(self) -> str:
        return f"{self.source.id}/{self.id}"

    @property
    def artifact(self) -> dict[str, Any]:
        return dict(self.value["artifact"])

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.value.items()
            if key != "artifact"
        }

    @property
    def commands(self) -> list[dict[str, Any]]:
        return list(self.value["commands"])

    @property
    def requirements(self) -> dict[str, list[dict[str, Any]]]:
        return dict(self.value["requires"])

    def as_json(self) -> dict[str, Any]:
        return {
            "repository": self.source.id,
            **self.value,
        }


@dataclass(frozen=True)
class PocketToolInstallResult:
    selector: str
    version: str
    install_path: Path
    changed: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "pocketool": self.selector,
            "version": self.version,
            "installPath": str(self.install_path),
            "changed": self.changed,
        }


class PocketToolManager:
    def __init__(
        self,
        paths: EapPaths,
        settings: Settings,
        client: HttpClient,
        status: Callable[[str], None] | None = None,
        reserved_commands: Iterable[str] = (),
    ):
        self.paths = paths
        self.settings = settings
        self.client = client
        self.status = status or (lambda message: None)
        self.reserved_commands = {
            name.casefold() for name in reserved_commands
        }
        self.reserved_commands.update(_RESERVED_COMMANDS)
        for tool in CoreTools.load(paths).definitions.values():
            if not tool.publish_to_environment_path:
                continue
            for executable in tool.executables:
                self.reserved_commands.add(Path(executable).stem.casefold())
        self.data_root = paths.data / "pocketools"
        self.catalog_root = self.data_root / "catalogs"
        self.state_root = self.data_root / "state"
        self.lock_path = self.data_root / "lock.json"
        self.bin_root = paths.pocketools / "bin"
        self.package_root = paths.pocketools / "packages"
        self.operation_lock = paths.temp / "locks" / "pocketools.lock"
        self.download_root = paths.temp / "downloads" / "pocketools"
        self.staging_root = paths.temp / "staging" / "pocketools"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for directory in (
            self.catalog_root,
            self.state_root,
            self.bin_root,
            self.package_root,
            self.download_root,
            self.staging_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.lock_path.is_file():
            atomic_write_json(self.lock_path, self._empty_lock())

    @staticmethod
    def _empty_lock() -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "updatedAt": utc_now(),
            "pocketools": [],
        }

    def sources(self) -> list[PocketToolSource]:
        sources: list[PocketToolSource] = []
        seen: set[str] = set()
        for key, raw_url in sorted(self.settings.values.items()):
            if not key.startswith(_REPOSITORY_PREFIX) or not raw_url.strip():
                continue
            source_id = validate_id(
                key[len(_REPOSITORY_PREFIX) :],
                "id de repositorio Pocketools",
            )
            if source_id.casefold() in seen:
                raise ValidationError(
                    f"Repositorio Pocketools duplicado: {source_id}"
                )
            seen.add(source_id.casefold())
            repository_url, catalog_url, source_type = self._repository_urls(
                raw_url
            )
            sources.append(
                PocketToolSource(
                    source_id,
                    repository_url,
                    catalog_url,
                    source_type,
                )
            )
        return sources

    @staticmethod
    def _repository_urls(raw_url: str) -> tuple[str, str, str]:
        url = raw_url.strip().rstrip("/")
        HttpClient.require_https(url)
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError(
                "La URL de un repositorio Pocketools no puede contener "
                "credenciales, query ni fragmento"
            )
        if url.casefold().endswith(".json"):
            return url, url, "catalog"
        if parsed.hostname and parsed.hostname.casefold() == "github.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 2:
                raise ValidationError(
                    f"Repositorio GitHub Pocketools no válido: {url}"
                )
            owner, repository = parts
            if repository.casefold().endswith(".git"):
                repository = repository[:-4]
            if not owner or not repository:
                raise ValidationError(
                    f"Repositorio GitHub Pocketools no válido: {url}"
                )
            repository_url = f"https://github.com/{owner}/{repository}"
            return (
                repository_url,
                f"https://api.github.com/repos/{owner}/{repository}/"
                "branches/main",
                "github-tree",
            )
        raise ValidationError(
            "Use una URL de repositorio github.com/owner/repo o la URL HTTPS "
            "directa de un catálogo JSON"
        )

    def source(self, source_id: str) -> PocketToolSource:
        normalized = source_id.casefold()
        for source in self.sources():
            if source.id.casefold() == normalized:
                return source
        raise ValidationError(
            f"Repositorio Pocketools desconocido: {source_id}"
        )

    def refresh(
        self, source_id: str | None = None
    ) -> list[PocketToolDefinition]:
        sources = [self.source(source_id)] if source_id else self.sources()
        if not sources:
            raise ValidationError("No hay repositorios Pocketools configurados")
        definitions: list[PocketToolDefinition] = []
        for source in sources:
            self.status(f"Consultando Pocketools de {source.id}...")
            if source.source_type == "github-tree":
                value = self._github_repository_catalog(source)
            else:
                value = self.client.get_json(source.catalog_url)
            validated = self._validate_catalog(value, source)
            atomic_write_json(self._catalog_path(source.id), validated)
            definitions.extend(
                PocketToolDefinition(source, item)
                for item in validated["pocketools"]
            )
        return definitions

    def _github_repository_catalog(
        self, source: PocketToolSource
    ) -> dict[str, Any]:
        parsed = urlparse(source.repository_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise ValidationError(
                f"Repositorio GitHub Pocketools no válido: {source.repository_url}"
            )
        owner, repository = parts
        branch = self.client.get_json(source.catalog_url)
        if not isinstance(branch, dict) or not isinstance(
            branch.get("commit"), dict
        ):
            raise ValidationError(
                f"GitHub no devolvió la rama main de {source.id}"
            )
        revision = str(branch["commit"].get("sha", "")).casefold()
        if not _GIT_OBJECT_ID.fullmatch(revision):
            raise ValidationError(
                f"Commit main no válido para el repositorio {source.id}"
            )
        tree_url = (
            f"https://api.github.com/repos/{owner}/{repository}/git/trees/"
            f"{revision}?recursive=1"
        )
        tree_value = self.client.get_json(tree_url, maximum_bytes=20 * 1024 * 1024)
        if (
            not isinstance(tree_value, dict)
            or tree_value.get("truncated") is True
            or not isinstance(tree_value.get("tree"), list)
        ):
            raise ValidationError(
                f"El árbol Git de {source.id} está incompleto o no es válido"
            )
        blobs: dict[str, dict[str, Any]] = {}
        for entry in tree_value["tree"]:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = entry.get("path")
            size = entry.get("size")
            object_id = str(entry.get("sha", "")).casefold()
            mode = str(entry.get("mode", ""))
            if (
                not isinstance(path, str)
                or not isinstance(size, int)
                or size < 0
                or not _GIT_OBJECT_ID.fullmatch(object_id)
            ):
                raise ValidationError(
                    f"Blob Git no válido en el repositorio {source.id}"
                )
            if mode == "120000":
                if path.startswith("pocketools/"):
                    raise ValidationError(
                        "Las Pocketools no admiten enlaces simbólicos: "
                        f"{path}"
                    )
                continue
            blobs[path] = {
                "sha": object_id,
                "size": size,
            }
        manifest_paths = sorted(
            path
            for path in blobs
            if re.fullmatch(r"pocketools/[^/]+/pocketool\.json", path)
        )
        entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        maximum_bytes = self.settings.get_int(
            "install.maxExtractBytes", minimum=1
        )
        for manifest_path in manifest_paths:
            _, directory_id, _ = manifest_path.split("/")
            validate_id(directory_id, "carpeta de Pocketool")
            manifest_url = self._github_raw_url(
                owner, repository, revision, manifest_path
            )
            try:
                manifest_value = json.loads(
                    self.client.get_text(
                        manifest_url,
                        maximum_bytes=1024 * 1024,
                    )
                )
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Manifiesto JSON no válido en {manifest_path}"
                ) from exc
            manifest = self._validate_manifest(
                manifest_value, require_artifact=False
            )
            if manifest["id"] != directory_id:
                raise ValidationError(
                    f"El id {manifest['id']} no coincide con la carpeta "
                    f"{directory_id} en {source.id}"
                )
            if directory_id.casefold() in seen_ids:
                raise ValidationError(
                    f"Pocketool duplicada en {source.id}: {directory_id}"
                )
            seen_ids.add(directory_id.casefold())
            prefix = f"pocketools/{directory_id}/"
            files: list[dict[str, Any]] = []
            total_size = 0
            for repository_path, blob in sorted(blobs.items()):
                if not repository_path.startswith(prefix):
                    continue
                relative = repository_path[len(prefix) :]
                self._safe_relative_path(relative, "archivo Pocketool")
                total_size += int(blob["size"])
                if total_size > maximum_bytes:
                    raise ValidationError(
                        f"{directory_id} supera el tamaño máximo de instalación"
                    )
                files.append(
                    {
                        "path": relative,
                        "url": self._github_raw_url(
                            owner,
                            repository,
                            revision,
                            repository_path,
                        ),
                        "gitObjectId": blob["sha"],
                        "size": blob["size"],
                    }
                )
            if not files or len(files) > 10000:
                raise ValidationError(
                    f"Número de archivos no válido para {directory_id}"
                )
            artifact = {
                "type": "github-tree",
                "commit": revision,
                "files": files,
                "size": total_size,
            }
            artifact["fingerprint"] = self._artifact_fingerprint(artifact)
            entries.append({**manifest, "artifact": artifact})
        return {
            "schemaVersion": 1,
            "repository": {
                "id": source.id,
                "name": f"{owner}/{repository}",
                "url": source.repository_url,
                "revision": revision,
            },
            "pocketools": entries,
        }

    @staticmethod
    def _github_raw_url(
        owner: str, repository: str, revision: str, path: str
    ) -> str:
        return (
            f"https://raw.githubusercontent.com/{owner}/{repository}/"
            f"{revision}/{path}"
        )

    def available(
        self,
        *,
        refresh: bool = False,
        require_cache: bool = False,
    ) -> list[PocketToolDefinition]:
        if refresh:
            return self.refresh()
        definitions: list[PocketToolDefinition] = []
        missing: list[str] = []
        for source in self.sources():
            path = self._catalog_path(source.id)
            if not path.is_file():
                missing.append(source.id)
                continue
            value = self._validate_catalog(load_json(path), source)
            if source.source_type == "github-tree" and any(
                item.get("artifact", {}).get("type") != "github-tree"
                for item in value["pocketools"]
            ):
                missing.append(source.id)
                continue
            definitions.extend(
                PocketToolDefinition(source, item)
                for item in value["pocketools"]
            )
        if missing and not require_cache:
            for source_id in missing:
                definitions.extend(self.refresh(source_id))
        return definitions

    def _catalog_path(self, source_id: str) -> Path:
        return self.catalog_root / f"{validate_id(source_id)}.json"

    def _validate_catalog(
        self, value: Any, source: PocketToolSource
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError(
                f"El catálogo Pocketools de {source.id} no es un objeto JSON"
            )
        require_fields(
            value,
            ("schemaVersion", "repository", "pocketools"),
            f"catálogo Pocketools {source.id}",
        )
        if value["schemaVersion"] != 1:
            raise ValidationError(
                f"Schema de catálogo Pocketools no soportado en {source.id}"
            )
        repository = value["repository"]
        if (
            not isinstance(repository, dict)
            or not isinstance(repository.get("id"), str)
            or not isinstance(repository.get("name"), str)
        ):
            raise ValidationError(
                f"Identidad de repositorio no válida en {source.id}"
            )
        validate_id(repository["id"], "id publicado de repositorio Pocketools")
        entries = value["pocketools"]
        if not isinstance(entries, list):
            raise ValidationError(
                f"pocketools debe ser una lista en {source.id}"
            )
        seen: set[str] = set()
        validated: list[dict[str, Any]] = []
        for entry in entries:
            manifest = self._validate_manifest(entry, require_artifact=True)
            normalized = str(manifest["id"]).casefold()
            if normalized in seen:
                raise ValidationError(
                    f"Pocketool duplicada en {source.id}: {manifest['id']}"
                )
            seen.add(normalized)
            validated.append(manifest)
        return {
            "schemaVersion": 1,
            "repository": repository,
            "pocketools": validated,
        }

    @classmethod
    def _validate_manifest(
        cls, value: Any, *, require_artifact: bool
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("Manifiesto Pocketool no válido")
        required = (
            "schemaVersion",
            "id",
            "name",
            "version",
            "description",
            "license",
            "platform",
            "help",
            "commands",
            "requires",
            "install",
        )
        require_fields(value, required, "manifiesto Pocketool")
        if require_artifact:
            require_fields(value, ("artifact",), "entrada de catálogo Pocketool")
        if value["schemaVersion"] != 1:
            raise ValidationError("Schema de manifiesto Pocketool no soportado")
        pocketool_id = validate_id(str(value["id"]), "id de Pocketool")
        if not all(
            isinstance(value[field], str) and value[field].strip()
            for field in ("name", "description", "license")
        ):
            raise ValidationError(
                f"Nombre, descripción o licencia no válidos en {pocketool_id}"
            )
        version = validate_version(str(value["version"]))
        semver_key(version)
        platform = value["platform"]
        if (
            not isinstance(platform, dict)
            or platform.get("os") != "windows"
            or platform.get("architecture") not in {"x64", "any"}
        ):
            raise ValidationError(
                f"Plataforma no soportada para {pocketool_id}"
            )
        help_value = value["help"]
        if (
            not isinstance(help_value, dict)
            or not isinstance(help_value.get("summary"), str)
            or not isinstance(help_value.get("usage"), str)
            or not isinstance(help_value.get("details", []), list)
            or not all(
                isinstance(item, str) for item in help_value.get("details", [])
            )
        ):
            raise ValidationError(f"Ayuda no válida para {pocketool_id}")
        commands = value["commands"]
        if not isinstance(commands, list) or not commands:
            raise ValidationError(f"{pocketool_id} no publica comandos")
        command_names: set[str] = set()
        for command in commands:
            if not isinstance(command, dict):
                raise ValidationError(f"Comando no válido en {pocketool_id}")
            require_fields(
                command,
                ("name", "type", "entrypoint", "arguments"),
                f"comando de {pocketool_id}",
            )
            name = str(command["name"])
            if not _COMMAND_NAME.fullmatch(name):
                raise ValidationError(
                    f"Nombre de comando no válido en {pocketool_id}: {name!r}"
                )
            if name.casefold() in _RESERVED_COMMANDS:
                raise ValidationError(f"Comando reservado por EAP: {name}")
            if name.casefold() in command_names:
                raise ValidationError(f"Comando duplicado en {pocketool_id}: {name}")
            command_names.add(name.casefold())
            if command["type"] not in _COMMAND_TYPES:
                raise ValidationError(
                    f"Tipo de comando no soportado en {pocketool_id}: "
                    f"{command['type']}"
                )
            cls._safe_relative_path(str(command["entrypoint"]), "entrypoint")
            if not isinstance(command["arguments"], list) or not all(
                isinstance(argument, str) for argument in command["arguments"]
            ):
                raise ValidationError(
                    f"Argumentos de comando no válidos en {pocketool_id}"
                )
        requirements = value["requires"]
        if not isinstance(requirements, dict):
            raise ValidationError(f"requires no es un objeto en {pocketool_id}")
        for collection in ("pocketools", "components"):
            if not isinstance(requirements.get(collection), list):
                raise ValidationError(
                    f"requires.{collection} no es una lista en {pocketool_id}"
                )
        for requirement in requirements["pocketools"]:
            if (
                not isinstance(requirement, dict)
                or not isinstance(requirement.get("id"), str)
                or not isinstance(requirement.get("minimumVersion"), str)
                or (
                    "repository" in requirement
                    and not isinstance(requirement["repository"], str)
                )
            ):
                raise ValidationError(
                    f"Dependencia Pocketool no válida en {pocketool_id}"
                )
            validate_id(requirement["id"], "id de dependencia Pocketool")
            semver_key(requirement["minimumVersion"])
            if requirement.get("repository"):
                validate_id(
                    requirement["repository"],
                    "repositorio de dependencia Pocketool",
                )
        for requirement in requirements["components"]:
            if (
                not isinstance(requirement, dict)
                or not isinstance(requirement.get("capability"), str)
                or not requirement["capability"]
                or not isinstance(requirement.get("minimumTrack"), (int, str))
                or isinstance(requirement.get("minimumTrack"), bool)
            ):
                raise ValidationError(
                    f"Dependencia de componente no válida en {pocketool_id}"
                )
        install = value["install"]
        if (
            not isinstance(install, dict)
            or not isinstance(install.get("requiredFiles"), list)
            or not all(isinstance(item, str) for item in install["requiredFiles"])
        ):
            raise ValidationError(f"Install no válido en {pocketool_id}")
        for relative in install["requiredFiles"]:
            cls._safe_relative_path(relative, "archivo requerido")
        if require_artifact:
            artifact = value["artifact"]
            if not isinstance(artifact, dict):
                raise ValidationError(f"Artefacto no válido en {pocketool_id}")
            cls._validate_artifact(artifact, pocketool_id)
        return json.loads(json.dumps(value))

    @classmethod
    def _validate_artifact(
        cls, artifact: dict[str, Any], pocketool_id: str
    ) -> None:
        artifact_type = str(artifact.get("type", "zip"))
        if artifact_type == "zip":
            require_fields(
                artifact,
                ("url", "fileName", "sha256", "size"),
                f"artefacto de {pocketool_id}",
            )
            HttpClient.require_https(str(artifact["url"]))
            file_name = str(artifact["fileName"])
            safe_file_name = cls._safe_relative_path(
                file_name, "nombre de artefacto"
            )
            if len(safe_file_name.parts) != 1 or not file_name.endswith(".zip"):
                raise ValidationError(
                    f"Nombre de artefacto no válido en {pocketool_id}"
                )
            if not _SHA256.fullmatch(str(artifact["sha256"])):
                raise ValidationError(f"SHA256 no válido en {pocketool_id}")
            if not isinstance(artifact["size"], int) or artifact["size"] <= 0:
                raise ValidationError(
                    f"Tamaño de artefacto no válido en {pocketool_id}"
                )
            return
        if artifact_type != "github-tree":
            raise ValidationError(
                f"Tipo de artefacto no soportado en {pocketool_id}: "
                f"{artifact_type}"
            )
        require_fields(
            artifact,
            ("commit", "files", "size", "fingerprint"),
            f"árbol GitHub de {pocketool_id}",
        )
        commit = str(artifact["commit"]).casefold()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise ValidationError(f"Commit Git no válido en {pocketool_id}")
        files = artifact["files"]
        if not isinstance(files, list) or not files or len(files) > 10000:
            raise ValidationError(
                f"Lista de archivos no válida en {pocketool_id}"
            )
        seen: set[str] = set()
        total = 0
        for file in files:
            if not isinstance(file, dict):
                raise ValidationError(
                    f"Archivo Git no válido en {pocketool_id}"
                )
            require_fields(
                file,
                ("path", "url", "gitObjectId", "size"),
                f"archivo Git de {pocketool_id}",
            )
            path = cls._safe_relative_path(
                str(file["path"]), "archivo Pocketool"
            )
            normalized = path.as_posix().casefold()
            if normalized in seen:
                raise ValidationError(
                    f"Archivo duplicado en {pocketool_id}: {path}"
                )
            seen.add(normalized)
            HttpClient.require_https(str(file["url"]))
            parsed = urlparse(str(file["url"]))
            if (
                parsed.hostname is None
                or parsed.hostname.casefold() != "raw.githubusercontent.com"
                or f"/{commit}/" not in parsed.path
            ):
                raise ValidationError(
                    f"Archivo no fijado al commit de {pocketool_id}: {file['url']}"
                )
            if not _GIT_OBJECT_ID.fullmatch(str(file["gitObjectId"])):
                raise ValidationError(
                    f"Objeto Git no válido en {pocketool_id}: {path}"
                )
            if not isinstance(file["size"], int) or file["size"] < 0:
                raise ValidationError(
                    f"Tamaño de archivo no válido en {pocketool_id}: {path}"
                )
            total += int(file["size"])
        if artifact["size"] != total:
            raise ValidationError(
                f"Tamaño total divergente en {pocketool_id}"
            )
        fingerprint = str(artifact["fingerprint"]).casefold()
        if (
            not _SHA256.fullmatch(fingerprint)
            or fingerprint != cls._artifact_fingerprint(artifact)
        ):
            raise ValidationError(
                f"Fingerprint de árbol divergente en {pocketool_id}"
            )

    @staticmethod
    def _artifact_fingerprint(artifact: dict[str, Any]) -> str:
        canonical = {
            "type": artifact.get("type", "zip"),
            "commit": artifact.get("commit"),
            "files": artifact.get("files"),
            "size": artifact.get("size"),
        }
        return hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _artifact_identity(artifact: dict[str, Any]) -> str:
        if artifact.get("type") == "github-tree":
            return str(artifact["fingerprint"]).casefold()
        return str(artifact["sha256"]).casefold()

    @staticmethod
    def _safe_relative_path(value: str, label: str) -> PurePosixPath:
        if (
            not value
            or "\\" in value
            or any(character in value for character in ':<>"|?*\x00')
            or any(ord(character) < 32 for character in value)
        ):
            raise ValidationError(f"{label.capitalize()} no válido: {value!r}")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValidationError(f"{label.capitalize()} no válido: {value!r}")
        for part in path.parts:
            if part.endswith((" ", ".")):
                raise ValidationError(
                    f"{label.capitalize()} no válido en Windows: {value!r}"
                )
            device_name = part.split(".", 1)[0].upper()
            if device_name in _WINDOWS_DEVICE_NAMES:
                raise ValidationError(
                    f"{label.capitalize()} reservado en Windows: {value!r}"
                )
        return path

    def installed(self) -> list[dict[str, Any]]:
        return list(self._read_lock()["pocketools"])

    def _read_lock(self) -> dict[str, Any]:
        value = load_json(self.lock_path)
        if value.get("schemaVersion") != 1 or not isinstance(
            value.get("pocketools"), list
        ):
            raise ValidationError("Lock de Pocketools no válido")
        return value

    @staticmethod
    def _split_selector(selector: str) -> tuple[str | None, str]:
        parts = selector.split("/")
        if len(parts) == 1:
            return None, validate_id(parts[0], "id de Pocketool")
        if len(parts) == 2:
            return (
                validate_id(parts[0], "repositorio Pocketools"),
                validate_id(parts[1], "id de Pocketool"),
            )
        raise ValidationError(f"Selector Pocketool no válido: {selector!r}")

    def find_available(
        self,
        selector: str,
        definitions: Iterable[PocketToolDefinition] | None = None,
    ) -> PocketToolDefinition:
        source_id, pocketool_id = self._split_selector(selector)
        candidates = [
            definition
            for definition in (definitions or self.available())
            if definition.id.casefold() == pocketool_id.casefold()
            and (
                source_id is None
                or definition.source.id.casefold() == source_id.casefold()
            )
        ]
        if not candidates:
            raise ValidationError(f"Pocketool no encontrada: {selector}")
        if len(candidates) > 1:
            choices = ", ".join(item.selector for item in candidates)
            raise ValidationError(
                f"Pocketool ambigua; indique repositorio/id: {choices}"
            )
        return candidates[0]

    def find_installed(self, selector: str) -> dict[str, Any]:
        source_id, pocketool_id = self._split_selector(selector)
        candidates = [
            item
            for item in self.installed()
            if str(item.get("id", "")).casefold() == pocketool_id.casefold()
            and (
                source_id is None
                or str(item.get("repository", "")).casefold()
                == source_id.casefold()
            )
        ]
        if not candidates:
            raise ValidationError(f"Pocketool no instalada: {selector}")
        if len(candidates) > 1:
            choices = ", ".join(
                f"{item['repository']}/{item['id']}" for item in candidates
            )
            raise ValidationError(
                f"Pocketool ambigua; indique repositorio/id: {choices}"
            )
        return candidates[0]

    def resolve_installation_plan(
        self,
        selector: str,
        *,
        refresh: bool = True,
    ) -> list[PocketToolDefinition]:
        requested_source, _ = self._split_selector(selector)
        refreshed_sources: set[str] = set()
        if refresh and requested_source is not None:
            current = self.refresh(requested_source)
            refreshed_sources.add(requested_source.casefold())
            cached = self.available(require_cache=True)
            merged = {
                (item.source.id.casefold(), item.id.casefold()): item
                for item in cached
            }
            merged.update(
                {
                    (item.source.id.casefold(), item.id.casefold()): item
                    for item in current
                }
            )
            definitions = list(merged.values())
        else:
            definitions = self.available(
                refresh=refresh,
                require_cache=not refresh,
            )
            if refresh:
                refreshed_sources.update(
                    source.id.casefold() for source in self.sources()
                )
        requested = self.find_available(selector, definitions)
        installed = {
            (str(item["repository"]).casefold(), str(item["id"]).casefold()): item
            for item in self.installed()
        }
        ordered: list[PocketToolDefinition] = []
        visiting: list[str] = []
        completed: set[tuple[str, str]] = set()

        def visit(definition: PocketToolDefinition) -> None:
            key = (definition.source.id.casefold(), definition.id.casefold())
            if key in completed:
                return
            if definition.selector.casefold() in visiting:
                cycle_start = visiting.index(definition.selector.casefold())
                cycle = [*visiting[cycle_start:], definition.selector.casefold()]
                raise ValidationError(
                    "Ciclo de dependencias Pocketools: " + " -> ".join(cycle)
                )
            visiting.append(definition.selector.casefold())
            for requirement in definition.requirements["pocketools"]:
                dependency_source = str(
                    requirement.get("repository") or definition.source.id
                )
                dependency_selector = f"{dependency_source}/{requirement['id']}"
                minimum = str(requirement["minimumVersion"])
                installed_dependency = installed.get(
                    (dependency_source.casefold(), str(requirement["id"]).casefold())
                )
                if (
                    installed_dependency is not None
                    and semver_key(str(installed_dependency["version"]))
                    >= semver_key(minimum)
                    and self.check_installation(installed_dependency) is None
                ):
                    continue
                candidates = [
                    item
                    for item in definitions
                    if item.source.id.casefold() == dependency_source.casefold()
                    and item.id.casefold()
                    == str(requirement["id"]).casefold()
                ]
                if (
                    not candidates
                    and refresh
                    and dependency_source.casefold() not in refreshed_sources
                ):
                    refreshed_sources.add(dependency_source.casefold())
                    refreshed = self.refresh(dependency_source)
                    definitions.extend(
                        item
                        for item in refreshed
                        if (
                            item.source.id.casefold(),
                            item.id.casefold(),
                        )
                        not in {
                            (
                                existing.source.id.casefold(),
                                existing.id.casefold(),
                            )
                            for existing in definitions
                        }
                    )
                dependency = self.find_available(
                    dependency_selector, definitions
                )
                if semver_key(dependency.version) < semver_key(minimum):
                    raise ValidationError(
                        f"{definition.selector} requiere {dependency_selector} "
                        f">= {minimum}, pero el catálogo ofrece {dependency.version}"
                    )
                visit(dependency)
            visiting.pop()
            completed.add(key)
            ordered.append(definition)

        visit(requested)
        return ordered

    def install_plan(
        self, definitions: list[PocketToolDefinition]
    ) -> list[PocketToolInstallResult]:
        if not definitions:
            return []
        with FileLock(self.operation_lock):
            previous_lock = self._read_lock()
            next_items = list(previous_lock["pocketools"])
            results: list[PocketToolInstallResult] = []
            for definition in definitions:
                existing = next(
                    (
                        item
                        for item in next_items
                        if str(item["repository"]).casefold()
                        == definition.source.id.casefold()
                        and str(item["id"]).casefold() == definition.id.casefold()
                    ),
                    None,
                )
                if (
                    existing is not None
                    and semver_key(str(existing["version"]))
                    >= semver_key(definition.version)
                    and self.check_installation(existing) is None
                ):
                    install_path = self.paths.require_within_root(
                        self.paths.root / str(existing["installPath"])
                    )
                    results.append(
                        PocketToolInstallResult(
                            definition.selector,
                            str(existing["version"]),
                            install_path,
                            False,
                        )
                    )
                    continue
                install_path = self._install_payload(definition)
                locked = self._locked_definition(definition, install_path)
                next_items = [
                    item
                    for item in next_items
                    if not (
                        str(item["repository"]).casefold()
                        == definition.source.id.casefold()
                        and str(item["id"]).casefold()
                        == definition.id.casefold()
                    )
                ]
                next_items.append(locked)
                results.append(
                    PocketToolInstallResult(
                        definition.selector,
                        definition.version,
                        install_path,
                        True,
                    )
                )
            next_lock = {
                "schemaVersion": 1,
                "updatedAt": utc_now(),
                "pocketools": sorted(
                    next_items,
                    key=lambda item: (
                        str(item["repository"]).casefold(),
                        str(item["id"]).casefold(),
                    ),
                ),
            }
            self._validate_command_collisions(
                next_lock["pocketools"],
                reserved=self.reserved_commands,
            )
            try:
                self._write_shims(next_lock["pocketools"])
                atomic_write_json(self.lock_path, next_lock)
            except OSError as exc:
                try:
                    self._write_shims(previous_lock["pocketools"])
                except (OSError, TransactionError) as rollback_exc:
                    raise TransactionError(
                        "Falló la publicación de Pocketools y también su rollback"
                    ) from rollback_exc
                raise TransactionError(
                    "No se pudo publicar la instalación de Pocketools"
                ) from exc
            return results

    def _install_payload(self, definition: PocketToolDefinition) -> Path:
        target = self.paths.require_within_root(
            self.package_root
            / definition.source.id
            / definition.id
            / definition.version
        )
        try:
            target.relative_to(self.package_root)
        except ValueError as exc:
            raise ValidationError("Ruta de instalación Pocketool no válida") from exc
        artifact_identity = self._artifact_identity(definition.artifact)
        marker_path = target / ".eap-pocketool.json"
        if target.is_dir() and marker_path.is_file():
            marker = load_json(marker_path)
            if (
                marker.get("status") == "ready"
                and marker.get("repository") == definition.source.id
                and marker.get("id") == definition.id
                and marker.get("version") == definition.version
                and (
                    marker.get("artifactFingerprint")
                    or marker.get("artifactSha256")
                )
                == artifact_identity
            ):
                return target
            raise IntegrityError(
                f"La instalación existente de {definition.selector} es divergente"
            )
        if definition.artifact.get("type") == "github-tree":
            return self._install_github_payload(
                definition,
                target,
                artifact_identity,
            )
        operation_id = uuid4().hex
        staging = self.staging_root / operation_id
        payload = staging / "payload"
        archive = self.download_root / definition.artifact["fileName"]
        temporary_archive = archive.with_name(f"{archive.name}.{operation_id}.partial")
        staging.mkdir(parents=True, exist_ok=False)
        try:
            self.status(
                f"Descargando {definition.name} {definition.version}..."
            )
            _, downloaded = self.client.download(
                str(definition.artifact["url"]),
                temporary_archive,
                maximum_bytes=int(definition.artifact["size"]),
            )
            if downloaded != int(definition.artifact["size"]):
                raise IntegrityError(
                    f"Tamaño inesperado para {definition.selector}: "
                    f"{downloaded} bytes"
                )
            checksum = sha256_file(temporary_archive).casefold()
            if checksum != str(definition.artifact["sha256"]).casefold():
                raise IntegrityError(
                    f"SHA256 incorrecto para {definition.selector}"
                )
            os.replace(temporary_archive, archive)
            self._extract_zip(archive, payload)
            extracted_manifest = self._validate_manifest(
                load_json(payload / "pocketool.json"),
                require_artifact=False,
            )
            if extracted_manifest != definition.manifest:
                raise IntegrityError(
                    f"El manifiesto del artefacto no coincide con el catálogo: "
                    f"{definition.selector}"
                )
            for relative in definition.value["install"]["requiredFiles"]:
                required_path = payload.joinpath(
                    *self._safe_relative_path(relative, "archivo requerido").parts
                )
                if not required_path.is_file():
                    raise IntegrityError(
                        f"Falta {relative} en {definition.selector}"
                    )
            for command in definition.commands:
                entrypoint = payload.joinpath(
                    *self._safe_relative_path(
                        str(command["entrypoint"]), "entrypoint"
                    ).parts
                )
                if not entrypoint.is_file():
                    raise IntegrityError(
                        f"No existe el entrypoint {command['entrypoint']} en "
                        f"{definition.selector}"
                    )
            atomic_write_json(
                payload / ".eap-pocketool.json",
                {
                    "status": "ready",
                    "repository": definition.source.id,
                    "id": definition.id,
                    "version": definition.version,
                    "artifactSha256": checksum,
                    "artifactFingerprint": artifact_identity,
                    "installedAt": utc_now(),
                },
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            payload.replace(target)
            return target
        except OSError as exc:
            raise TransactionError(
                f"No se pudo instalar el payload de {definition.selector}: {exc}"
            ) from exc
        finally:
            temporary_archive.unlink(missing_ok=True)
            if not self.settings.get_bool("download.keepArchives"):
                archive.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _install_github_payload(
        self,
        definition: PocketToolDefinition,
        target: Path,
        artifact_identity: str,
    ) -> Path:
        operation_id = uuid4().hex
        staging = self.staging_root / operation_id
        payload = staging / "payload"
        staging.mkdir(parents=True, exist_ok=False)
        payload.mkdir(parents=True, exist_ok=False)
        try:
            files = definition.artifact["files"]
            for index, file in enumerate(files, start=1):
                relative = self._safe_relative_path(
                    str(file["path"]), "archivo Pocketool"
                )
                destination = payload.joinpath(*relative.parts)
                temporary = destination.with_name(
                    f".{destination.name}.{operation_id}.partial"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                self.status(
                    f"Descargando {definition.name} {definition.version} "
                    f"({index}/{len(files)}): {relative.as_posix()}"
                )
                _, downloaded = self.client.download(
                    str(file["url"]),
                    temporary,
                    maximum_bytes=max(1, int(file["size"])),
                )
                if downloaded != int(file["size"]):
                    raise IntegrityError(
                        f"Tamaño inesperado para {definition.selector}/"
                        f"{relative.as_posix()}"
                    )
                expected_object_id = str(file["gitObjectId"]).casefold()
                object_id = self._git_blob_object_id(
                    temporary, len(expected_object_id)
                )
                if object_id != expected_object_id:
                    raise IntegrityError(
                        f"Objeto Git incorrecto para {definition.selector}/"
                        f"{relative.as_posix()}"
                    )
                temporary.replace(destination)
            extracted_manifest = self._validate_manifest(
                load_json(payload / "pocketool.json"),
                require_artifact=False,
            )
            if extracted_manifest != definition.manifest:
                raise IntegrityError(
                    "El manifiesto descargado no coincide con el índice "
                    f"consultado: {definition.selector}"
                )
            self._validate_payload_files(definition, payload)
            atomic_write_json(
                payload / ".eap-pocketool.json",
                {
                    "status": "ready",
                    "repository": definition.source.id,
                    "id": definition.id,
                    "version": definition.version,
                    "repositoryCommit": definition.artifact["commit"],
                    "artifactFingerprint": artifact_identity,
                    "installedAt": utc_now(),
                },
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            payload.replace(target)
            return target
        except OSError as exc:
            raise TransactionError(
                f"No se pudo instalar {definition.selector} desde GitHub: {exc}"
            ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _git_blob_object_id(path: Path, object_id_length: int = 40) -> str:
        size = path.stat().st_size
        digest = (
            hashlib.sha256()
            if object_id_length == 64
            else hashlib.sha1(usedforsecurity=False)
        )
        digest.update(f"blob {size}\0".encode("ascii"))
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_payload_files(
        self, definition: PocketToolDefinition, payload: Path
    ) -> None:
        for relative in definition.value["install"]["requiredFiles"]:
            required_path = payload.joinpath(
                *self._safe_relative_path(relative, "archivo requerido").parts
            )
            if not required_path.is_file():
                raise IntegrityError(
                    f"Falta {relative} en {definition.selector}"
                )
        for command in definition.commands:
            entrypoint = payload.joinpath(
                *self._safe_relative_path(
                    str(command["entrypoint"]), "entrypoint"
                ).parts
            )
            if not entrypoint.is_file():
                raise IntegrityError(
                    f"No existe el entrypoint {command['entrypoint']} en "
                    f"{definition.selector}"
                )

    def _extract_zip(self, archive: Path, destination: Path) -> None:
        maximum_bytes = self.settings.get_int("install.maxExtractBytes", minimum=1)
        maximum_ratio = self.settings.get_int("install.maxCompressionRatio", minimum=1)
        seen: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(archive) as package:
                entries = package.infolist()
                if len(entries) > 10000:
                    raise IntegrityError("El Pocketool contiene demasiados archivos")
                for entry in entries:
                    path = self._safe_relative_path(entry.filename.rstrip("/"), "ruta ZIP")
                    normalized = "/".join(path.parts).casefold()
                    if normalized in seen:
                        raise IntegrityError(
                            f"Ruta duplicada en el ZIP Pocketool: {entry.filename}"
                        )
                    seen.add(normalized)
                    unix_mode = entry.external_attr >> 16
                    if unix_mode and stat.S_ISLNK(unix_mode):
                        raise IntegrityError("Los Pocketools no admiten enlaces simbólicos")
                    total += entry.file_size
                    if total > maximum_bytes:
                        raise IntegrityError(
                            "El contenido extraído del Pocketool supera el límite"
                        )
                    if (
                        entry.file_size > 0
                        and entry.compress_size == 0
                        or (
                            entry.compress_size > 0
                            and entry.file_size / entry.compress_size > maximum_ratio
                        )
                    ):
                        raise IntegrityError(
                            f"Ratio de compresión no seguro: {entry.filename}"
                        )
                destination.mkdir(parents=True, exist_ok=False)
                for entry in entries:
                    relative = self._safe_relative_path(
                        entry.filename.rstrip("/"), "ruta ZIP"
                    )
                    target = destination.joinpath(*relative.parts)
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise IntegrityError(f"ZIP Pocketool inválido: {archive}") from exc
        except OSError as exc:
            raise TransactionError(
                f"No se pudo extraer el ZIP Pocketool {archive}: {exc}"
            ) from exc

    def _locked_definition(
        self, definition: PocketToolDefinition, install_path: Path
    ) -> dict[str, Any]:
        return {
            "repository": definition.source.id,
            "repositoryUrl": definition.source.repository_url,
            "id": definition.id,
            "name": definition.name,
            "version": definition.version,
            "installPath": str(install_path.relative_to(self.paths.root)),
            "artifact": definition.artifact,
            "manifest": definition.manifest,
        }

    @staticmethod
    def _validate_command_collisions(
        items: list[dict[str, Any]],
        *,
        reserved: set[str] | None = None,
    ) -> None:
        commands: dict[str, str] = {}
        reserved = reserved or set()
        for item in items:
            selector = f"{item['repository']}/{item['id']}"
            for command in item["manifest"]["commands"]:
                normalized = str(command["name"]).casefold()
                if normalized in reserved:
                    raise ValidationError(
                        f"El comando {command['name']} ya está reservado por "
                        "EAP o por un componente"
                    )
                existing = commands.get(normalized)
                if existing is not None:
                    raise ValidationError(
                        f"El comando {command['name']} colisiona entre "
                        f"{existing} y {selector}"
                    )
                commands[normalized] = selector

    def _write_shims(self, items: list[dict[str, Any]]) -> None:
        operation_id = uuid4().hex
        staging = self.staging_root / f"bin-{operation_id}"
        backup = self.staging_root / f"bin-backup-{operation_id}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            for item in items:
                selector = f"{item['repository']}/{item['id']}"
                for command in item["manifest"]["commands"]:
                    command_name = str(command["name"])
                    content = (
                        "@echo off\n"
                        "setlocal\n"
                        '"%~dp0..\\..\\core\\tools\\python-embed\\python.exe" '
                        "-B -I -X utf8 -m eap pocketool run "
                        f'"{selector}" "{command_name}" -- %*\n'
                        "exit /b %ERRORLEVEL%\n"
                    )
                    atomic_write_text(staging / f"{command_name}.cmd", content)
            if self.bin_root.exists():
                self.bin_root.replace(backup)
            try:
                staging.replace(self.bin_root)
            except OSError:
                if backup.exists() and not self.bin_root.exists():
                    backup.replace(self.bin_root)
                raise
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
        except OSError as exc:
            if backup.exists() and not self.bin_root.exists():
                try:
                    backup.replace(self.bin_root)
                except OSError as rollback_exc:
                    raise TransactionError(
                        "Falló la publicación de shims y también su rollback"
                    ) from rollback_exc
            raise TransactionError(
                f"No se pudieron publicar los shims Pocketools: {exc}"
            ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def uninstall(self, selector: str) -> dict[str, Any]:
        with FileLock(self.operation_lock):
            previous_lock = self._read_lock()
            source_id, pocketool_id = self._split_selector(selector)
            candidates = [
                item
                for item in previous_lock["pocketools"]
                if str(item.get("id", "")).casefold()
                == pocketool_id.casefold()
                and (
                    source_id is None
                    or str(item.get("repository", "")).casefold()
                    == source_id.casefold()
                )
            ]
            if not candidates:
                raise ValidationError(f"Pocketool no instalada: {selector}")
            if len(candidates) > 1:
                raise ValidationError(
                    "Pocketool ambigua; indique repositorio/id: "
                    + ", ".join(
                        f"{item['repository']}/{item['id']}"
                        for item in candidates
                    )
                )
            target = candidates[0]
            target_selector = f"{target['repository']}/{target['id']}"
            dependents: list[str] = []
            for item in previous_lock["pocketools"]:
                if item is target:
                    continue
                for requirement in item["manifest"]["requires"]["pocketools"]:
                    repository = str(
                        requirement.get("repository") or item["repository"]
                    )
                    if (
                        repository.casefold() == str(target["repository"]).casefold()
                        and str(requirement["id"]).casefold()
                        == str(target["id"]).casefold()
                    ):
                        dependents.append(f"{item['repository']}/{item['id']}")
            if dependents:
                raise ValidationError(
                    f"No se puede desinstalar {target_selector}; depende(n): "
                    + ", ".join(dependents)
                )
            install_path = self.paths.require_within_root(
                self.paths.root / str(target["installPath"])
            )
            try:
                install_path.relative_to(self.package_root)
            except ValueError as exc:
                raise IntegrityError(
                    "El payload Pocketool apunta fuera de su almacén"
                ) from exc
            remaining = [
                item for item in previous_lock["pocketools"] if item is not target
            ]
            next_lock = {
                "schemaVersion": 1,
                "updatedAt": utc_now(),
                "pocketools": remaining,
            }
            self._write_shims(remaining)
            try:
                atomic_write_json(self.lock_path, next_lock)
            except OSError as exc:
                self._write_shims(previous_lock["pocketools"])
                raise TransactionError(
                    f"No se pudo desinstalar {target_selector}"
                ) from exc
            removed = False
            try:
                shutil.rmtree(install_path)
                removed = True
            except FileNotFoundError:
                removed = True
            except OSError:
                pass
            return {
                "pocketool": target_selector,
                "version": target["version"],
                "payloadRemoved": removed,
                "residualPath": None if removed else str(install_path),
            }

    def help(self, selector: str) -> dict[str, Any]:
        installed = self.find_installed(selector)
        failure = self.check_installation(installed)
        if failure is not None:
            raise IntegrityError(
                f"La instalación de {selector} no es válida: {failure}"
            )
        return {
            "repository": installed["repository"],
            "id": installed["id"],
            "name": installed["name"],
            "version": installed["version"],
            "help": installed["manifest"]["help"],
            "commands": installed["manifest"]["commands"],
            "requires": installed["manifest"]["requires"],
        }

    def run(
        self,
        selector: str,
        command_name: str,
        arguments: list[str],
        environment: dict[str, str],
    ) -> int:
        installed = self.find_installed(selector)
        failure = self.check_installation(installed)
        if failure is not None:
            raise IntegrityError(
                f"La instalación de {selector} no es válida: {failure}"
            )
        command = next(
            (
                item
                for item in installed["manifest"]["commands"]
                if str(item["name"]).casefold() == command_name.casefold()
            ),
            None,
        )
        if command is None:
            raise ValidationError(
                f"{selector} no publica el comando {command_name}"
            )
        install_path = self.paths.require_within_root(
            self.paths.root / str(installed["installPath"])
        )
        try:
            install_path.relative_to(self.package_root)
        except ValueError as exc:
            raise IntegrityError("Instalación Pocketool fuera del almacén") from exc
        entrypoint = install_path.joinpath(
            *self._safe_relative_path(
                str(command["entrypoint"]), "entrypoint"
            ).parts
        ).resolve()
        try:
            entrypoint.relative_to(install_path)
        except ValueError as exc:
            raise IntegrityError("Entrypoint Pocketool fuera del payload") from exc
        if not entrypoint.is_file():
            raise IntegrityError(f"Entrypoint Pocketool ausente: {entrypoint}")
        command_type = str(command["type"])
        declared_arguments = [str(item) for item in command["arguments"]]
        if command_type == "powershell":
            executable = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            invocation = [
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(entrypoint),
                *declared_arguments,
                *arguments,
            ]
        elif command_type == "cmd":
            invocation = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                str(entrypoint),
                *declared_arguments,
                *arguments,
            ]
        elif command_type == "exe":
            invocation = [str(entrypoint), *declared_arguments, *arguments]
        else:
            runtime_name = {
                "java-jar": "java.exe",
                "python": "python.exe",
                "node": "node.exe",
            }[command_type]
            runtime = shutil.which(runtime_name, path=environment.get("PATH"))
            if runtime is None:
                raise ValidationError(
                    f"{selector} necesita {runtime_name} en el profile activo"
                )
            invocation = [runtime]
            if command_type == "java-jar":
                invocation.append("-jar")
            invocation.extend([str(entrypoint), *declared_arguments, *arguments])
        child_environment = dict(environment)
        state_path = (
            self.state_root / str(installed["repository"]) / str(installed["id"])
        )
        state_path.mkdir(parents=True, exist_ok=True)
        child_environment.update(
            {
                "EAP_POCKETOOL_ID": str(installed["id"]),
                "EAP_POCKETOOL_VERSION": str(installed["version"]),
                "EAP_POCKETOOL_ROOT": str(install_path),
                "EAP_POCKETOOL_DATA": str(state_path),
            }
        )
        return subprocess.run(
            invocation,
            cwd=Path.cwd(),
            env=child_environment,
            check=False,
        ).returncode

    def check_installation(self, item: dict[str, Any]) -> str | None:
        try:
            install_path = self.paths.require_within_root(
                self.paths.root / str(item["installPath"])
            )
            install_path.relative_to(self.package_root)
            marker = load_json(install_path / ".eap-pocketool.json")
            artifact = item.get("artifact", {})
            if not isinstance(artifact, dict):
                return "artefacto bloqueado no válido"
            expected_identity = self._artifact_identity(artifact)
            if not (
                marker.get("status") == "ready"
                and marker.get("repository") == item.get("repository")
                and marker.get("id") == item.get("id")
                and marker.get("version") == item.get("version")
                and (
                    marker.get("artifactFingerprint")
                    or marker.get("artifactSha256")
                )
                == expected_identity
            ):
                return "marcador ausente o divergente"
            for command in item["manifest"]["commands"]:
                entrypoint = install_path.joinpath(
                    *self._safe_relative_path(
                        str(command["entrypoint"]), "entrypoint"
                    ).parts
                )
                if not entrypoint.is_file():
                    return f"falta {command['entrypoint']}"
        except (KeyError, OSError, ValidationError, ValueError) as exc:
            return str(exc)
        return None


def update_repository_property(
    path: Path, source_id: str, repository_url: str | None
) -> None:
    source_id = validate_id(source_id, "id de repositorio Pocketools")
    key = f"{_REPOSITORY_PREFIX}{source_id}"
    lines = (
        path.read_text(encoding="utf-8-sig").splitlines()
        if path.is_file()
        else []
    )
    output: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith(("#", ";", "!"))
            and "=" in line
            and line.split("=", 1)[0].strip() == key
        ):
            if not found and repository_url is not None:
                output.append(f"{key}={repository_url}")
            found = True
            continue
        output.append(line)
    if not found and repository_url is not None:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={repository_url}")
    atomic_write_text(path, "\n".join(output) + ("\n" if output else ""))
