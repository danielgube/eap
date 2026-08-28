from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__
from .catalog import Catalog
from .config import DEFAULTS, Settings
from .core_tools import CoreTools
from .environments import EnvironmentStore
from .errors import IntegrityError, TransactionError, ValidationError
from .locks import FileLock
from .paths import EapPaths
from .util import (
    atomic_write_json,
    load_json,
    sha256_file,
    utc_now,
    validate_id,
)


@dataclass(frozen=True)
class ExportResult:
    archive: Path
    environment_id: str
    workspace_id: str
    components_included: bool
    configuration_included: bool
    size: int
    sha256: str


@dataclass(frozen=True)
class ImportResult:
    archive: Path
    environment_id: str
    workspace_id: str
    components_copied: int
    components_missing: int
    configuration_included: bool


@dataclass(frozen=True)
class ToolExportResult:
    archive: Path
    components_included: bool
    size: int
    sha256: str


class EnvironmentTransfer:
    def __init__(
        self,
        paths: EapPaths,
        settings: Settings,
        catalog: Catalog,
        environments: EnvironmentStore,
        core_tools: CoreTools,
        status: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        self.settings = settings
        self.catalog = catalog
        self.environments = environments
        self.core_tools = core_tools
        self.status = status or (lambda message: None)

    def export_environment(
        self,
        source_environment_id: str,
        exported_environment_id: str,
        include_components: bool,
        include_configuration: bool = False,
        force: bool = False,
    ) -> ExportResult:
        exported_environment_id = self._normalize_export_name(
            exported_environment_id
        )
        source_desired = self.environments.read_desired(source_environment_id)
        source_lock = self.environments.read_lock(source_environment_id)
        if include_components:
            missing = self._missing_locked_paths(
                source_lock, include_external=False
            )
            if missing:
                raise ValidationError(
                    "No se pueden incluir components ausentes: "
                    + ", ".join(missing)
                )
        archive = (
            self.paths.exports
            / "envs"
            / f"{exported_environment_id}.7z"
        )
        if archive.exists() and not force:
            raise ValidationError(
                f"Ya existe la exportación: {archive}; use --force para reemplazarla"
            )
        operation_id = uuid.uuid4().hex
        staging_root = self.paths.require_within_root(
            self.paths.temp / "exports" / operation_id
        )
        package_root = staging_root / "package"
        partial_archive = staging_root / f"{exported_environment_id}.7z.partial"
        lock_path = self.paths.temp / "locks" / "environment-export.lock"
        with FileLock(lock_path):
            try:
                package_root.mkdir(parents=True, exist_ok=False)
                self.status("Preparando el paquete de profile...")
                self._copy_components(
                    package_root, source_lock, include_components
                )
                self._copy_exported_environment(
                    package_root,
                    source_environment_id,
                    exported_environment_id,
                )
                self._prepare_exported_environment_config(
                    package_root / "envs" / exported_environment_id,
                    include_configuration,
                )
                exported_workspace = exported_environment_id
                self._copy_workspace(
                    package_root,
                    str(source_desired["workspace"]),
                    exported_workspace,
                )
                package_manifest = {
                    "schemaVersion": 2,
                    "format": "eap-environment-package",
                    "createdAt": utc_now(),
                    "eapVersion": __version__,
                    "environment": exported_environment_id,
                    "workspace": exported_workspace,
                    "componentsIncluded": include_components,
                    "configurationIncluded": include_configuration,
                    "components": [
                        {
                            "id": str(item["id"]),
                            "provider": str(item["provider"]),
                            "track": item["track"],
                            "version": str(item["version"]),
                            "installationType": (
                                item.get("installation", {}).get("type")
                                or "archive"
                            ),
                            **(
                                {}
                                if self._is_external_locked(item)
                                else {"installPath": str(item["installPath"])}
                            ),
                            "included": (
                                include_components
                                and not self._is_external_locked(item)
                            ),
                        }
                        for item in source_lock["components"]
                    ],
                    "excluded": [
                        "data",
                        "temp",
                        "exports",
                        "EAP (eap.cmd y core)",
                        "config.properties general",
                        *(
                            []
                            if include_configuration
                            else ["config.properties privado del profile"]
                        ),
                        "otros profiles",
                        "otros workspaces",
                    ],
                }
                atomic_write_json(
                    package_root / "eap-env-package.json", package_manifest
                )
                self.status("Comprimiendo con 7-Zip...")
                self._run_7zip(
                    [
                        "a",
                        "-t7z",
                        "-mx=7",
                        "-mmt=on",
                        "-bsp1",
                        "-y",
                        str(partial_archive),
                        ".",
                    ],
                    cwd=package_root,
                    operation="crear la exportación",
                    live_output=True,
                )
                if not partial_archive.is_file():
                    raise TransactionError(
                        "7-Zip no generó el archivo de exportación"
                    )
                self.status("Verificando el archivo 7z...")
                self._run_7zip(
                    ["t", "-bd", "-y", str(partial_archive)],
                    cwd=staging_root,
                    operation="verificar la exportación",
                )
                archive.parent.mkdir(parents=True, exist_ok=True)
                os.replace(partial_archive, archive)
                return ExportResult(
                    archive=archive,
                    environment_id=exported_environment_id,
                    workspace_id=exported_workspace,
                    components_included=include_components,
                    configuration_included=include_configuration,
                    size=archive.stat().st_size,
                    sha256=sha256_file(archive),
                )
            finally:
                self._remove_staging(staging_root)

    def export_tool(
        self,
        name: str,
        include_components: bool,
        force: bool = False,
    ) -> ToolExportResult:
        export_name = self._normalize_archive_name(name, "nombre de EAP")
        archive = self.paths.exports / "eap" / f"{export_name}.7z"
        if archive.exists() and not force:
            raise ValidationError(
                f"Ya existe la exportación: {archive}; use --force para reemplazarla"
            )
        operation_id = uuid.uuid4().hex
        staging_root = self.paths.require_within_root(
            self.paths.temp / "exports" / operation_id
        )
        package_root = staging_root / "package"
        partial_archive = staging_root / f"{export_name}.7z.partial"
        lock_path = self.paths.temp / "locks" / "tool-export.lock"
        with FileLock(lock_path):
            try:
                package_root.mkdir(parents=True, exist_ok=False)
                self.status("Preparando la distribución portable de EAP...")
                self._copy_static_root(package_root)
                self._copy_tool_components(
                    package_root, include_components=include_components
                )
                self._write_safe_config(package_root, "default")
                atomic_write_json(
                    package_root / "eap-tool-package.json",
                    {
                        "schemaVersion": 1,
                        "format": "eap-tool-package",
                        "createdAt": utc_now(),
                        "eapVersion": __version__,
                        "componentsIncluded": include_components,
                        "excluded": [
                            "data",
                            "temp",
                            "exports",
                            "envs",
                            "pocketools",
                            "workspaces",
                            "config.properties local",
                        ],
                    },
                )
                self.status("Comprimiendo EAP con 7-Zip...")
                self._run_7zip(
                    [
                        "a",
                        "-t7z",
                        "-mx=7",
                        "-mmt=on",
                        "-bsp1",
                        "-y",
                        str(partial_archive),
                        ".",
                    ],
                    cwd=package_root,
                    operation="crear la distribución de EAP",
                    live_output=True,
                )
                if not partial_archive.is_file():
                    raise TransactionError(
                        "7-Zip no generó la distribución de EAP"
                    )
                self.status("Verificando el archivo 7z...")
                self._run_7zip(
                    ["t", "-bd", "-y", str(partial_archive)],
                    cwd=staging_root,
                    operation="verificar la distribución de EAP",
                )
                archive.parent.mkdir(parents=True, exist_ok=True)
                os.replace(partial_archive, archive)
                return ToolExportResult(
                    archive=archive,
                    components_included=include_components,
                    size=archive.stat().st_size,
                    sha256=sha256_file(archive),
                )
            finally:
                self._remove_staging(staging_root)

    def import_environment(self, archive: Path) -> ImportResult:
        archive = archive.expanduser().resolve()
        if not archive.is_file():
            raise ValidationError(f"No existe la exportación: {archive}")
        operation_id = uuid.uuid4().hex
        staging_root = self.paths.require_within_root(
            self.paths.temp / "imports" / operation_id
        )
        extraction_root = staging_root / "extracted"
        lock_path = self.paths.temp / "locks" / "environment-import.lock"
        with FileLock(lock_path):
            try:
                staging_root.mkdir(parents=True, exist_ok=False)
                self.status("Inspeccionando el archivo 7z...")
                listing = self._run_7zip(
                    ["l", "-slt", "-ba", str(archive)],
                    cwd=staging_root,
                    operation="inspeccionar la importación",
                )
                self._validate_archive_listing(listing)
                extraction_root.mkdir()
                self.status("Extrayendo la importación...")
                self._run_7zip(
                    [
                        "x",
                        "-bd",
                        "-y",
                        f"-o{extraction_root}",
                        str(archive),
                    ],
                    cwd=staging_root,
                    operation="extraer la importación",
                )
                self._validate_extracted_tree(extraction_root)
                manifest = self._load_environment_package_manifest(
                    extraction_root
                )
                self._validate_package_manifest(manifest)
                environment_id = validate_id(
                    str(manifest["environment"]), "profile importado"
                )
                workspace_id = validate_id(
                    str(manifest["workspace"]), "workspace importado"
                )
                source_environment = extraction_root / "envs" / environment_id
                desired = load_json(source_environment / "environment.json")
                lock = load_json(source_environment / "environment.lock.json")
                if (
                    desired.get("id") != environment_id
                    or desired.get("workspace") != workspace_id
                    or (
                        manifest.get("schemaVersion") == 2
                        and desired.get("dataProfile") != environment_id
                    )
                    or lock.get("environmentId") != environment_id
                ):
                    raise IntegrityError(
                        "La identidad interna del profile exportado no coincide"
                    )
                target_environment = self.paths.envs / environment_id
                if target_environment.exists():
                    raise ValidationError(
                        f"Ya existe el profile {environment_id}; "
                        "elija otro nombre al exportar"
                    )
                self._validate_imported_lock(lock)
                source_workspace = extraction_root / "workspaces" / workspace_id
                target_workspace = self.paths.workspaces / workspace_id
                if target_workspace.exists() and any(target_workspace.iterdir()):
                    raise ValidationError(
                        f"El workspace de destino no está vacío: {target_workspace}"
                    )
                components_copied = self._import_components(
                    extraction_root,
                    lock,
                    bool(manifest["componentsIncluded"]),
                )
                if source_workspace.is_dir():
                    if target_workspace.exists():
                        shutil.copytree(
                            source_workspace,
                            target_workspace,
                            dirs_exist_ok=True,
                        )
                    else:
                        shutil.copytree(source_workspace, target_workspace)
                else:
                    target_workspace.mkdir(parents=True, exist_ok=True)
                importing_environment = self.paths.envs / (
                    f".{environment_id}.import-{operation_id}"
                )
                shutil.copytree(source_environment, importing_environment)
                importing_environment.replace(target_environment)
                self.environments.ensure_config(environment_id)
                self.environments.ensure_profile(environment_id)
                self.environments.select(environment_id)
                missing = self._missing_locked_paths(lock)
                return ImportResult(
                    archive=archive,
                    environment_id=environment_id,
                    workspace_id=workspace_id,
                    components_copied=components_copied,
                    components_missing=len(missing),
                    configuration_included=bool(
                        manifest.get("configurationIncluded", False)
                    ),
                )
            finally:
                self._remove_staging(staging_root)

    def _copy_static_root(self, package_root: Path) -> None:
        excluded = {
            ".agents",
            ".codex",
            ".git",
            "components",
            "pocketools",
            "config.properties",
            "data",
            "envs",
            "exports",
            "temp",
            "workspaces",
        }
        for source in self.paths.root.iterdir():
            if source.name.casefold() in excluded:
                continue
            target = package_root / source.name
            if source.is_dir():
                shutil.copytree(
                    source,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            elif source.is_file():
                if source.suffix.casefold() in {".7z", ".zip", ".partial"}:
                    continue
                shutil.copy2(source, target)

    def _copy_components(
        self,
        package_root: Path,
        lock: dict[str, Any],
        include_components: bool,
    ) -> None:
        if not include_components:
            return
        for item in lock["components"]:
            if self._is_external_locked(item):
                continue
            relative = Path(str(item["installPath"]))
            source = self.paths.require_within_root(self.paths.root / relative)
            try:
                source.relative_to(self.paths.components)
            except ValueError as exc:
                raise ValidationError(
                    f"El payload de {item['id']} sale de components"
                ) from exc
            destination = package_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

    def _copy_tool_components(
        self, package_root: Path, include_components: bool
    ) -> None:
        target = package_root / "components"
        if include_components:
            shutil.copytree(
                self.paths.components,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            self._enrich_exported_payload_markers(target)
            return
        target.mkdir()

    def _enrich_exported_payload_markers(
        self, exported_components: Path
    ) -> None:
        for environment_id in self.environments.list():
            lock = self.environments.read_lock(environment_id)
            for item in lock["components"]:
                if self._is_external_locked(item):
                    continue
                artifact = item.get("artifact")
                relative_text = item.get("installPath")
                if (
                    not isinstance(artifact, dict)
                    or artifact.get("localOnly") is True
                    or not isinstance(relative_text, str)
                ):
                    continue
                try:
                    source_path = self.paths.require_within_root(
                        self.paths.root / relative_text
                    )
                    component_relative = source_path.relative_to(
                        self.paths.components
                    )
                except (OSError, ValidationError, ValueError):
                    continue
                marker_path = (
                    exported_components
                    / component_relative
                    / ".eap-install.json"
                )
                try:
                    marker = load_json(marker_path)
                except ValidationError:
                    continue
                if not self._marker_matches(marker_path, item):
                    continue
                url = artifact.get("url")
                file_name = artifact.get("fileName")
                metadata_url = item.get("metadataUrl", url)
                if not all(
                    isinstance(value, str) and value
                    for value in (url, file_name, metadata_url)
                ):
                    continue
                marker["source"] = {
                    "url": url,
                    "fileName": file_name,
                    "metadataUrl": metadata_url,
                    "size": (
                        artifact.get("size")
                        if isinstance(artifact.get("size"), int)
                        else None
                    ),
                }
                atomic_write_json(marker_path, marker)

    def _copy_exported_environment(
        self,
        package_root: Path,
        source_environment_id: str,
        exported_environment_id: str,
    ) -> None:
        self.environments.ensure_config(source_environment_id)
        source = self.environments.files(source_environment_id).root
        target = package_root / "envs" / exported_environment_id
        target.parent.mkdir()
        shutil.copytree(source, target)
        desired_path = target / "environment.json"
        desired = load_json(desired_path)
        desired["id"] = exported_environment_id
        desired["displayName"] = exported_environment_id
        desired["dataProfile"] = exported_environment_id
        desired["workspace"] = exported_environment_id
        atomic_write_json(desired_path, desired)
        lock_path = target / "environment.lock.json"
        lock = load_json(lock_path)
        lock["environmentId"] = exported_environment_id
        for item in lock.get("components", []):
            if self._is_external_locked(item):
                item["installation"]["executable"] = None
        atomic_write_json(lock_path, lock)
        state_path = target / "state.json"
        state = load_json(state_path)
        state["lastActivatedAt"] = None
        atomic_write_json(state_path, state)

    @staticmethod
    def _prepare_exported_environment_config(
        environment_root: Path, include_configuration: bool
    ) -> None:
        if include_configuration:
            return
        (environment_root / "config.properties").write_text(
            "# Configuración privada no incluida en la exportación.\n"
            "# Use env.NOMBRE=valor; por ejemplo: env.GITHUB_TOKEN=...\n",
            encoding="utf-8",
            newline="\n",
        )

    def _copy_workspace(
        self,
        package_root: Path,
        source_workspace_id: str,
        exported_workspace_id: str,
    ) -> None:
        source = self.paths.workspaces / source_workspace_id
        target = package_root / "workspaces" / exported_workspace_id
        target.parent.mkdir()
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir()

    @staticmethod
    def _write_safe_config(package_root: Path, environment_id: str) -> None:
        values = dict(DEFAULTS)
        values["profile.default"] = environment_id
        values["environment.default"] = environment_id
        content = [
            "# Configuración segura generada por EAP para una exportación.",
            "# Añada localmente proxies, tokens u otros valores privados.",
            *(f"{key}={value}" for key, value in values.items()),
            "",
        ]
        (package_root / "config.properties").write_text(
            "\n".join(content), encoding="utf-8", newline="\n"
        )

    def _validate_imported_lock(self, lock: dict[str, Any]) -> None:
        components = lock.get("components")
        if not isinstance(components, list):
            raise IntegrityError("El lock importado no contiene components")
        for item in components:
            if not isinstance(item, dict):
                raise IntegrityError("Componente importado no válido")
            component_id = str(item.get("id", ""))
            component = self.catalog.component(component_id)
            component.provider(str(item.get("provider", "")))
            component.validate_track(item.get("track"))
            if component.is_external:
                installation = item.get("installation")
                executable = (
                    installation.get("executable")
                    if isinstance(installation, dict)
                    else None
                )
                if (
                    not isinstance(installation, dict)
                    or installation.get("type") != "external-executable"
                    or (
                        executable is not None
                        and not isinstance(executable, str)
                    )
                ):
                    raise IntegrityError(
                        f"Vinculación externa no válida para {component_id}"
                    )
                if isinstance(executable, str):
                    executable_path = Path(executable)
                    allowed_names = {
                        str(name).casefold()
                        for name in component.value["install"][
                            "executableNames"
                        ]
                    }
                    if (
                        not executable_path.is_absolute()
                        or executable_path.name.casefold()
                        not in allowed_names
                    ):
                        raise IntegrityError(
                            "Ruta externa importada no válida para "
                            f"{component_id}"
                        )
                continue
            install_path = self.paths.require_within_root(
                self.paths.root / str(item.get("installPath", ""))
            )
            try:
                install_path.relative_to(self.paths.components)
            except ValueError as exc:
                raise IntegrityError(
                    f"El componente {component_id} sale de components"
                ) from exc

    def _import_components(
        self,
        extraction_root: Path,
        lock: dict[str, Any],
        components_included: bool,
    ) -> int:
        if not components_included:
            return 0
        copied = 0
        for item in lock["components"]:
            if self._is_external_locked(item):
                continue
            relative = Path(str(item["installPath"]))
            source = extraction_root / relative
            target = self.paths.require_within_root(self.paths.root / relative)
            if not source.is_dir():
                raise IntegrityError(
                    f"El paquete no incluye {item['id']}: {relative}"
                )
            if target.exists():
                marker = target / ".eap-install.json"
                if not self._marker_matches(marker, item):
                    raise IntegrityError(
                        f"La instalación existente diverge: {target}"
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
            marker = target / ".eap-install.json"
            if not self._marker_matches(marker, item):
                raise IntegrityError(
                    f"La instalación importada no coincide: {target}"
                )
            copied += 1
        return copied

    def _missing_locked_paths(
        self,
        lock: dict[str, Any],
        include_external: bool = True,
    ) -> list[str]:
        missing: list[str] = []
        for item in lock.get("components", []):
            if self._is_external_locked(item):
                if not include_external:
                    continue
                executable = item.get("installation", {}).get("executable")
                if not isinstance(executable, str) or not Path(
                    executable
                ).is_file():
                    missing.append(str(item["id"]))
                continue
            target = self.paths.require_within_root(
                self.paths.root / str(item["installPath"])
            )
            if not target.is_dir() or not self._marker_matches(
                target / ".eap-install.json", item
            ):
                missing.append(str(item["id"]))
        return missing

    @staticmethod
    def _is_external_locked(locked: dict[str, Any]) -> bool:
        installation = locked.get("installation")
        return bool(
            isinstance(installation, dict)
            and installation.get("type") == "external-executable"
        )

    @staticmethod
    def _marker_matches(marker_path: Path, locked: dict[str, Any]) -> bool:
        if not marker_path.is_file():
            return False
        try:
            marker = load_json(marker_path)
        except ValidationError:
            return False
        artifact = locked.get("artifact", {})
        return bool(
            marker.get("status") == "ready"
            and marker.get("component") == locked.get("id")
            and marker.get("provider") == locked.get("provider")
            and str(marker.get("track")) == str(locked.get("track"))
            and str(marker.get("version")) == str(locked.get("version"))
            and marker.get("artifactChecksum") == artifact.get("checksum")
        )

    def _run_7zip(
        self,
        arguments: list[str],
        cwd: Path,
        operation: str,
        live_output: bool = False,
    ) -> str:
        executable = self.core_tools.tool("7zip").executable("7z.exe")
        attached_terminal = live_output and sys.stdout.isatty()
        try:
            completed = subprocess.run(
                [str(executable), *arguments],
                cwd=cwd,
                capture_output=not attached_terminal,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=None,
                check=False,
            )
        except OSError as exc:
            raise TransactionError(
                f"No se pudo ejecutar 7-Zip para {operation}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = "\n".join(
                value.strip()
                for value in (completed.stdout, completed.stderr)
                if value and value.strip()
            )
            raise TransactionError(
                f"7-Zip no pudo {operation} ({completed.returncode}): {detail}"
            )
        return "\n".join(
            value for value in (completed.stdout, completed.stderr) if value
        )

    def _validate_archive_listing(self, listing: str) -> None:
        total_size = 0
        entry_count = 0
        for raw_line in listing.splitlines():
            if raw_line.startswith("Path = "):
                self._validate_archive_path(raw_line.removeprefix("Path = "))
                entry_count += 1
            elif raw_line.startswith("Size = "):
                try:
                    total_size += int(raw_line.removeprefix("Size = "))
                except ValueError as exc:
                    raise IntegrityError(
                        "Tamaño inválido en el índice 7z"
                    ) from exc
        if entry_count == 0:
            raise IntegrityError("El archivo 7z está vacío")
        maximum_files = self.settings.get_int(
            "transfer.maxFiles", minimum=1
        )
        maximum_bytes = self.settings.get_int(
            "transfer.maxExtractBytes", minimum=1
        )
        if entry_count > maximum_files:
            raise IntegrityError(
                f"La importación contiene demasiadas entradas: {entry_count}"
            )
        if total_size > maximum_bytes:
            raise IntegrityError(
                f"La importación excede el límite descomprimido: {total_size} bytes"
            )

    @staticmethod
    def _validate_archive_path(raw_path: str) -> None:
        normalized = raw_path.replace("\\", "/").rstrip("/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise IntegrityError(f"Ruta no segura en el archivo 7z: {raw_path}")

    def _validate_extracted_tree(self, extraction_root: Path) -> None:
        for path in extraction_root.rglob("*"):
            if path.is_symlink():
                raise IntegrityError(
                    f"La importación contiene un enlace: {path}"
                )
            try:
                path.resolve().relative_to(extraction_root.resolve())
            except ValueError as exc:
                raise IntegrityError(
                    f"La extracción sale de staging: {path}"
                ) from exc

    @staticmethod
    def _load_environment_package_manifest(
        extraction_root: Path,
    ) -> dict[str, Any]:
        current = extraction_root / "eap-env-package.json"
        if current.is_file():
            return load_json(current)
        legacy = extraction_root / "eap-export.json"
        if legacy.is_file():
            return load_json(legacy)
        raise IntegrityError("El 7z no es un paquete de profile EAP")

    @staticmethod
    def _validate_package_manifest(manifest: dict[str, Any]) -> None:
        package_format = manifest.get("format")
        valid_identity = (
            isinstance(manifest.get("environment"), str)
            and isinstance(manifest.get("workspace"), str)
            and isinstance(manifest.get("componentsIncluded"), bool)
        )
        version_2 = (
            manifest.get("schemaVersion") == 2
            and package_format == "eap-environment-package"
            and isinstance(manifest.get("configurationIncluded"), bool)
        )
        legacy = (
            manifest.get("schemaVersion") == 1
            and package_format == "eap-environment-export"
        )
        if not valid_identity or not (version_2 or legacy):
            raise IntegrityError("Manifiesto de paquete de profile no válido")

    @staticmethod
    def _normalize_export_name(value: str) -> str:
        return EnvironmentTransfer._normalize_archive_name(
            value, "nombre de exportación"
        )

    @staticmethod
    def _normalize_archive_name(value: str, label: str) -> str:
        normalized = value[:-3] if value.lower().endswith(".7z") else value
        return validate_id(normalized, label)

    def _remove_staging(self, staging_root: Path) -> None:
        if not staging_root.exists():
            return
        resolved = self.paths.require_within_root(staging_root)
        try:
            resolved.relative_to(self.paths.temp)
        except ValueError as exc:
            raise TransactionError(
                f"Staging fuera de temp: {resolved}"
            ) from exc
        delays = (0.1, 0.25, 0.5, 1.0, 2.0)
        for attempt, delay in enumerate(delays, start=1):
            try:
                shutil.rmtree(resolved)
                return
            except FileNotFoundError:
                return
            except PermissionError as exc:
                if attempt == len(delays):
                    raise TransactionError(
                        f"Windows mantiene bloqueado el staging: {resolved}"
                    ) from exc
                time.sleep(delay)
