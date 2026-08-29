from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from .catalog import Catalog, ComponentCatalogSource
from .config import Settings
from .errors import NetworkError, ValidationError
from .network import HttpClient
from .paths import EapPaths
from .util import atomic_write_json, atomic_write_text, require_fields, validate_id

_REPOSITORY_PREFIX = "components.repository."
_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REVISION = re.compile(r"^[0-9a-fA-F]{40,64}$")
_MAX_CATALOG_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_COMPONENTS = 1000


@dataclass(frozen=True)
class ComponentRepositorySource:
    id: str
    repository_url: str
    catalog_url: str
    source_type: str

    def as_json(self, revision: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "repositoryUrl": self.repository_url,
            "catalogUrl": self.catalog_url,
            "sourceType": self.source_type,
            "revision": revision,
        }


class ComponentRepositoryManager:
    def __init__(
        self,
        paths: EapPaths,
        settings: Settings,
        client: HttpClient,
        status: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        self.settings = settings
        self.client = client
        self.status = status or (lambda message: None)
        self.cache_root = paths.data / "component-catalogs"
        self.staging_root = paths.temp / "staging" / "component-catalogs"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def sources(self) -> list[ComponentRepositorySource]:
        sources: list[ComponentRepositorySource] = []
        seen: set[str] = set()
        for key, raw_url in sorted(self.settings.values.items()):
            if not key.startswith(_REPOSITORY_PREFIX) or not raw_url.strip():
                continue
            source_id = validate_id(
                key[len(_REPOSITORY_PREFIX) :],
                "id de repositorio de componentes",
            )
            normalized = source_id.casefold()
            if normalized in seen:
                raise ValidationError(
                    f"Repositorio de componentes duplicado: {source_id}"
                )
            seen.add(normalized)
            repository_url, catalog_url, source_type = self._repository_urls(
                raw_url
            )
            sources.append(
                ComponentRepositorySource(
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
                "La URL de un repositorio de componentes no puede contener "
                "credenciales, query ni fragmento"
            )
        if url.casefold().endswith(".json"):
            return url, url, "catalog"
        if parsed.hostname and parsed.hostname.casefold() == "github.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 2:
                raise ValidationError(
                    f"Repositorio GitHub de componentes no válido: {url}"
                )
            owner, repository = parts
            if repository.casefold().endswith(".git"):
                repository = repository[:-4]
            if not owner or not repository:
                raise ValidationError(
                    f"Repositorio GitHub de componentes no válido: {url}"
                )
            repository_url = f"https://github.com/{owner}/{repository}"
            branch_url = (
                f"https://api.github.com/repos/{owner}/{repository}/"
                "branches/main"
            )
            return repository_url, branch_url, "github"
        raise ValidationError(
            "Use una URL github.com/owner/repo o la URL HTTPS directa de "
            "un catalog.json"
        )

    def source(self, source_id: str) -> ComponentRepositorySource:
        normalized = source_id.casefold()
        for source in self.sources():
            if source.id.casefold() == normalized:
                return source
        raise ValidationError(
            f"Repositorio de componentes desconocido: {source_id}"
        )

    def refresh(self, source_id: str | None = None) -> Catalog:
        sources = [self.source(source_id)] if source_id else self.sources()
        if not sources:
            raise ValidationError(
                "No hay repositorios de componentes configurados"
            )
        snapshots: dict[str, dict[str, Any]] = {}
        for source in sources:
            self.status(f"Consultando componentes de {source.id}...")
            snapshots[source.id] = self._fetch_snapshot(source)
        self._validate_component_collisions(snapshots)
        for source in sources:
            snapshot = snapshots[source.id]
            self._cache_snapshot(source, snapshot)
        return self.load()

    def _validate_component_collisions(
        self, snapshots: dict[str, dict[str, Any]]
    ) -> None:
        published: dict[str, str] = {}
        for source in self.sources():
            snapshot = snapshots.get(source.id)
            if snapshot is not None:
                catalog = snapshot["catalog"]
            else:
                active_path = self._source_root(source.id) / "active.json"
                if not active_path.is_file():
                    continue
                try:
                    active = json.loads(
                        active_path.read_text(encoding="utf-8-sig")
                    )
                    if active.get("repositoryUrl") != source.repository_url:
                        continue
                    revision = str(active["revision"]).casefold()
                    if not _REVISION.fullmatch(revision):
                        raise ValueError("revisión no válida")
                    catalog_path = (
                        self._source_root(source.id)
                        / "revisions"
                        / revision
                        / "catalog.json"
                    )
                    catalog = self._parse_catalog(
                        catalog_path.read_text(encoding="utf-8-sig"),
                        source.id,
                    )
                except (
                    KeyError,
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    ValidationError,
                ) as exc:
                    self.status(
                        f"Ignorando caché no válida de {source.id}: {exc}"
                    )
                    continue
            for entry in catalog["components"]:
                component_id = str(entry["id"])
                normalized = component_id.casefold()
                previous = published.get(normalized)
                if previous is not None:
                    raise ValidationError(
                        f"Componente {component_id!r} publicado por dos "
                        f"repositorios: {previous} y {source.id}"
                    )
                published[normalized] = source.id

    def load(self) -> Catalog:
        bundled = Catalog.load(self.paths)
        definitions = dict(bundled.definitions)
        external_ids: dict[str, str] = {}
        catalog_sources: dict[str, ComponentCatalogSource] = {}
        for configured in self.sources():
            active_path = self._source_root(configured.id) / "active.json"
            if not active_path.is_file():
                continue
            try:
                active = json.loads(active_path.read_text(encoding="utf-8-sig"))
                revision = str(active["revision"]).casefold()
                if not _REVISION.fullmatch(revision):
                    raise ValidationError(
                        f"Revisión cacheada no válida para {configured.id}"
                    )
                if active.get("repositoryUrl") != configured.repository_url:
                    continue
                revision_root = (
                    self._source_root(configured.id) / "revisions" / revision
                )
                metadata = json.loads(
                    (revision_root / "repository.json").read_text(
                        encoding="utf-8-sig"
                    )
                )
                origin = ComponentCatalogSource(
                    configured.id,
                    configured.repository_url,
                    str(metadata["catalogUrl"]),
                    revision,
                    configured.source_type,
                )
                cached = Catalog.load_from_path(
                    self.paths,
                    revision_root / "catalog.json",
                    origin,
                )
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                self.status(
                    f"Ignorando caché no válida de {configured.id}: {exc}"
                )
                continue
            for component_id, definition in cached.definitions.items():
                previous = external_ids.get(component_id.casefold())
                if previous is not None:
                    raise ValidationError(
                        f"Componente {component_id!r} publicado por dos "
                        f"repositorios: {previous} y {configured.id}"
                    )
                external_ids[component_id.casefold()] = configured.id
                definitions[component_id] = definition
            catalog_sources[configured.id] = origin
        value = {
            "schemaVersion": 1,
            "catalogVersion": bundled.value.get("catalogVersion"),
            "components": [
                {
                    "id": definition.id,
                    "source": definition.source_id,
                }
                for definition in definitions.values()
            ],
        }
        return Catalog(self.paths, value, definitions, catalog_sources)

    def cached_sources(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for source in self.sources():
            revision: str | None = None
            active_path = self._source_root(source.id) / "active.json"
            if active_path.is_file():
                try:
                    value = json.loads(
                        active_path.read_text(encoding="utf-8-sig")
                    )
                    if value.get("repositoryUrl") != source.repository_url:
                        result.append(source.as_json(None))
                        continue
                    raw_revision = value.get("revision")
                    if isinstance(raw_revision, str):
                        revision = raw_revision
                except (OSError, json.JSONDecodeError):
                    revision = None
            result.append(source.as_json(revision))
        return result

    def _fetch_snapshot(
        self, source: ComponentRepositorySource
    ) -> dict[str, Any]:
        if source.source_type == "github":
            branch = self.client.get_json(source.catalog_url)
            if not isinstance(branch, dict) or not isinstance(
                branch.get("commit"), dict
            ):
                raise NetworkError(
                    f"GitHub no devolvió la rama main de {source.id}"
                )
            revision = str(branch["commit"].get("sha", "")).casefold()
            if not _GIT_OBJECT_ID.fullmatch(revision):
                raise ValidationError(
                    f"Commit main no válido para {source.id}"
                )
            parsed = urlparse(source.repository_url)
            owner, repository = [
                part for part in parsed.path.split("/") if part
            ]
            base_url = (
                f"https://raw.githubusercontent.com/{owner}/{repository}/"
                f"{revision}/"
            )
            catalog_url = base_url + "catalog.json"
        else:
            revision = ""
            catalog_url = source.catalog_url
            base_url = urljoin(catalog_url, "./")

        catalog_text = self.client.get_text(
            catalog_url, maximum_bytes=_MAX_CATALOG_BYTES
        )
        catalog_value = self._parse_catalog(catalog_text, source.id)
        manifests: dict[str, str] = {}
        fingerprint = hashlib.sha256()
        fingerprint.update(catalog_text.encode("utf-8"))
        for entry in catalog_value["components"]:
            manifest_path = self._manifest_path(str(entry["manifest"]))
            manifest_url = urljoin(base_url, manifest_path)
            manifest_text = self.client.get_text(
                manifest_url, maximum_bytes=_MAX_MANIFEST_BYTES
            )
            try:
                manifest = json.loads(manifest_text)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Manifiesto JSON no válido: {manifest_path}"
                ) from exc
            Catalog._validate_component(
                manifest,
                str(entry["id"]),
                Path(f"{source.id}/{manifest_path}"),
            )
            manifests[manifest_path] = manifest_text
            fingerprint.update(manifest_path.encode("utf-8"))
            fingerprint.update(manifest_text.encode("utf-8"))
        if not revision:
            revision = fingerprint.hexdigest()
        return {
            "revision": revision,
            "catalogUrl": catalog_url,
            "catalogText": catalog_text,
            "catalog": catalog_value,
            "manifests": manifests,
        }

    @staticmethod
    def _parse_catalog(text: str, source_id: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"catalog.json no es JSON válido en {source_id}"
            ) from exc
        if not isinstance(value, dict):
            raise ValidationError(
                f"catalog.json debe ser un objeto en {source_id}"
            )
        require_fields(
            value,
            ("schemaVersion", "catalogVersion", "components"),
            f"catálogo {source_id}",
        )
        if value["schemaVersion"] != 1:
            raise ValidationError(
                f"schemaVersion no soportada en {source_id}: "
                f"{value['schemaVersion']}"
            )
        entries = value["components"]
        if (
            not isinstance(entries, list)
            or not entries
            or len(entries) > _MAX_COMPONENTS
        ):
            raise ValidationError(
                f"Lista de componentes no válida en {source_id}"
            )
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"Entrada de componente no válida en {source_id}"
                )
            require_fields(entry, ("id", "manifest"), source_id)
            component_id = validate_id(
                str(entry["id"]), "id de componente"
            )
            normalized = component_id.casefold()
            if normalized in seen:
                raise ValidationError(
                    f"Componente duplicado en {source_id}: {component_id}"
                )
            seen.add(normalized)
            manifest_path = ComponentRepositoryManager._manifest_path(
                str(entry["manifest"])
            )
            expected_path = f"components/{component_id}.json"
            if manifest_path != expected_path:
                raise ValidationError(
                    f"El manifiesto de {component_id} debe publicarse como "
                    f"{expected_path}, no {manifest_path}"
                )
        return value

    @staticmethod
    def _manifest_path(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if (
            path.is_absolute()
            or path.suffix.casefold() != ".json"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValidationError(f"Ruta de manifiesto no válida: {value}")
        return path.as_posix()

    def _cache_snapshot(
        self,
        source: ComponentRepositorySource,
        snapshot: dict[str, Any],
    ) -> None:
        revision = str(snapshot["revision"]).casefold()
        if not _REVISION.fullmatch(revision):
            raise ValidationError(
                f"Revisión no válida para {source.id}: {revision}"
            )
        source_root = self._source_root(source.id)
        revisions_root = source_root / "revisions"
        target = revisions_root / revision
        if not target.is_dir():
            staging = self.staging_root / f"{source.id}-{uuid4().hex}"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                atomic_write_text(
                    staging / "catalog.json", snapshot["catalogText"]
                )
                for manifest_path, content in snapshot["manifests"].items():
                    destination = staging.joinpath(
                        *PurePosixPath(manifest_path).parts
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(destination, content)
                atomic_write_json(
                    staging / "repository.json",
                    {
                        "schemaVersion": 1,
                        "id": source.id,
                        "repositoryUrl": source.repository_url,
                        "catalogUrl": snapshot["catalogUrl"],
                        "sourceType": source.source_type,
                        "revision": revision,
                    },
                )
                revisions_root.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(staging, target)
                except FileExistsError:
                    shutil.rmtree(staging, ignore_errors=True)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        source_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            source_root / "active.json",
            {
                "schemaVersion": 1,
                "repositoryUrl": source.repository_url,
                "revision": revision,
            },
        )

    def _source_root(self, source_id: str) -> Path:
        safe_id = validate_id(source_id, "id de repositorio de componentes")
        return self.paths.require_within_root(self.cache_root / safe_id)


def update_component_repository_property(
    path: Path, source_id: str, repository_url: str | None
) -> None:
    source_id = validate_id(
        source_id, "id de repositorio de componentes"
    )
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
