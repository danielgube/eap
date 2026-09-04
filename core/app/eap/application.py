from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from . import __version__
from .catalog import Catalog, ComponentDefinition
from .component_repositories import (
    ComponentRepositoryManager,
    update_component_repository_property,
)
from .config import Settings, load_properties
from .console import console_title
from .core_tools import CoreTools
from .environments import EnvironmentStore
from .errors import EapError, TransactionError, ValidationError
from .host_integrations import (
    HostIntegrationChange,
    HostIntegrationManager,
    HostIntegrationStatus,
)
from .installer import ComponentInstaller
from .locks import FileLock
from .network import HttpClient
from .paths import EapPaths
from .pocketools import (
    PocketToolDefinition,
    PocketToolInstallResult,
    PocketToolManager,
    update_repository_property,
)
from .proxy import (
    ProxyAuthenticationResult,
    ProxyAuthenticator,
    ProxyConfiguration,
    apply_proxy_environment,
)
from .releases import (
    EapReleasePublisher,
    EapReleaseResult,
    EapReleaseUpdater,
    EapUpdateResult,
    EapUpdateStatus,
    GitHubApiClient,
)
from .resolvers import ResolvedArtifact, resolve_component
from .shortcuts import ShortcutResult, WindowsShortcutManager
from .terminal import ManagedTerminal, TerminalLaunch
from .transfers import (
    EnvironmentTransfer,
    ExportResult,
    ImportResult,
    ToolExportResult,
)
from .util import (
    atomic_write_json,
    load_json,
    sha256_file,
    utc_now,
    validate_version,
    version_key,
)


@dataclass(frozen=True)
class UpdateInfo:
    family: str
    provider: str
    track: int | str
    current_version: str
    latest: ResolvedArtifact
    major_update: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "provider": self.provider,
            "track": self.track,
            "currentVersion": self.current_version,
            "latestVersion": self.latest.version,
            "majorUpdate": self.major_update,
            "artifact": self.latest.as_json(),
        }


@dataclass(frozen=True)
class LauncherInfo:
    id: str
    component_id: str
    component_name: str
    display_name: str
    executable: Path
    arguments: tuple[str, ...]
    working_directory: Path
    start_mode: str
    environment: dict[str, str]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "component": self.component_id,
            "componentName": self.component_name,
            "displayName": self.display_name,
            "executable": str(self.executable),
            "arguments": list(self.arguments),
            "workingDirectory": str(self.working_directory),
            "startMode": self.start_mode,
        }


@dataclass(frozen=True)
class ComponentUninstallResult:
    component_id: str
    profile_id: str
    payload_path: Path | None
    payload_removed: bool
    shared_profiles: tuple[str, ...] = ()
    residual_path: Path | None = None


@dataclass(frozen=True)
class LocalComponentPayload:
    component_id: str
    display_name: str
    provider: str
    provider_name: str
    track: int | str
    version: str
    install_path: Path
    artifact: ResolvedArtifact
    restorable: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "component": self.component_id,
            "displayName": self.display_name,
            "provider": self.provider,
            "providerName": self.provider_name,
            "track": self.track,
            "version": self.version,
            "installPath": str(self.install_path),
            "restorable": self.restorable,
        }


@dataclass(frozen=True)
class TemporaryStorageUsage:
    bytes: int
    files: int


@dataclass(frozen=True)
class TemporaryCleanupResult:
    bytes_removed: int
    files_removed: int


class EapApplication:
    def __init__(self, status: Callable[[str], None] | None = None):
        self.paths = EapPaths.discover()
        self.paths.ensure_layout()
        self.status = status or (lambda message: None)
        self.settings = Settings.load(self.paths.config)
        declared_settings = load_properties(self.paths.config)
        self.proxy_configuration = ProxyConfiguration.from_properties(
            self.settings.values
        )
        apply_proxy_environment(os.environ, declared_settings)
        self.proxy_authenticator = ProxyAuthenticator(
            self.proxy_configuration,
            user_agent=f"EAP/{self.version}",
            status=self.status,
        )
        self.client = HttpClient(
            self.settings.get_int("network.timeoutSeconds", minimum=1),
            user_agent=f"EAP/{self.version}",
        )
        self.component_repositories = ComponentRepositoryManager(
            self.paths,
            self.settings,
            self.client,
            status=self.status,
        )
        self.catalog = self.component_repositories.load()
        self.environments = EnvironmentStore(self.paths)
        self.host_integrations = HostIntegrationManager(
            self.paths, self.environments
        )
        self.core_tools = CoreTools.load(self.paths)
        self.pocketools = PocketToolManager(
            self.paths,
            self.settings,
            self.client,
            status=self.status,
            reserved_commands=self._component_command_names(),
        )
        self.release_api = GitHubApiClient(
            self.settings.get_int("network.timeoutSeconds", minimum=1),
            user_agent=f"EAP/{self.version}",
        )
        self.release_updater = EapReleaseUpdater(
            self.paths,
            self.client,
            self.release_api,
            status=self.status,
        )
        self.update_cache_path = self.paths.data / "update-checks.json"

    @property
    def version(self) -> str:
        return __version__

    def active_profile_id(self) -> str:
        requested = os.environ.get("EAP_PROFILE")
        if requested and requested in self.environments.list():
            return requested
        selected = self.environments.selected(
            self.settings.get("profile.default")
        )
        if selected is None:
            raise ValidationError(
                "No hay un profile EAP seleccionado para evaluar Pocketools"
            )
        return selected

    def proxy_status(self) -> dict[str, object]:
        configuration = getattr(self, "proxy_configuration", None)
        authenticator = getattr(self, "proxy_authenticator", None)
        if configuration is None:
            configuration = ProxyConfiguration.from_properties({})
        return configuration.status(
            authenticated=bool(
                authenticator is not None and authenticator.authenticated
            )
        )

    def authenticate_proxy(
        self, *, force: bool = False
    ) -> ProxyAuthenticationResult:
        authenticator = getattr(self, "proxy_authenticator", None)
        if authenticator is None:
            return ProxyAuthenticationResult(
                "disabled", "No hay un proxy configurado.", False
            )
        return authenticator.ensure_authenticated(force=force)

    def _ensure_proxy_authenticated(self) -> None:
        authenticator = getattr(self, "proxy_authenticator", None)
        if authenticator is None:
            return
        result = authenticator.ensure_authenticated()
        if result.state in {"authenticated", "already-connected"}:
            self.status(result.detail)

    def refresh_component_catalogs(
        self, repository: str | None = None
    ) -> Catalog:
        EapApplication._ensure_proxy_authenticated(self)
        self.catalog = self.component_repositories.refresh(repository)
        self._reload_pocketool_settings()
        return self.catalog

    def add_component_repository(
        self, source_id: str, repository_url: str
    ) -> None:
        ComponentRepositoryManager._repository_urls(repository_url)
        update_component_repository_property(
            self.paths.config, source_id, repository_url.strip()
        )
        self._reload_component_repository_settings()

    def remove_component_repository(self, source_id: str) -> None:
        source = self.component_repositories.source(source_id)
        update_component_repository_property(
            self.paths.config,
            source.id,
            None,
        )
        self._reload_component_repository_settings()

    def _reload_component_repository_settings(self) -> None:
        self.settings = Settings.load(self.paths.config)
        self.component_repositories = ComponentRepositoryManager(
            self.paths,
            self.settings,
            self.client,
            status=self.status,
        )
        self.catalog = self.component_repositories.load()
        if hasattr(self, "pocketools"):
            self._reload_pocketool_settings()

    def available_pocketools(
        self, *, refresh: bool = False, require_cache: bool = False
    ) -> list[PocketToolDefinition]:
        if refresh or not require_cache:
            EapApplication._ensure_proxy_authenticated(self)
        return self.pocketools.available(
            refresh=refresh,
            require_cache=require_cache,
        )

    def refresh_pocketools(
        self, repository: str | None = None
    ) -> list[PocketToolDefinition]:
        EapApplication._ensure_proxy_authenticated(self)
        return self.pocketools.refresh(repository)

    def install_pocketool(
        self,
        selector: str,
        environment_id: str | None = None,
        *,
        refresh: bool = True,
    ) -> list[PocketToolInstallResult]:
        EapApplication._ensure_proxy_authenticated(self)
        profile_id = environment_id or self.active_profile_id()
        plan = self.pocketools.resolve_installation_plan(
            selector, refresh=refresh
        )
        for definition in plan:
            self._validate_pocketool_component_requirements(
                definition.requirements["components"],
                profile_id,
                definition.selector,
            )
        return self.pocketools.install_plan(plan)

    def update_pocketool(
        self, selector: str, environment_id: str | None = None
    ) -> list[PocketToolInstallResult]:
        self.pocketools.find_installed(selector)
        return self.install_pocketool(
            selector,
            environment_id,
            refresh=True,
        )

    def pocketool_updates(self) -> list[dict[str, Any]]:
        available = {
            (item.source.id.casefold(), item.id.casefold()): item
            for item in self.available_pocketools(refresh=True)
        }
        updates: list[dict[str, Any]] = []
        from .pocketools import semver_key

        for installed in self.pocketools.installed():
            definition = available.get(
                (
                    str(installed["repository"]).casefold(),
                    str(installed["id"]).casefold(),
                )
            )
            if definition is not None and semver_key(
                definition.version
            ) > semver_key(str(installed["version"])):
                updates.append(
                    {
                        "repository": definition.source.id,
                        "id": definition.id,
                        "name": definition.name,
                        "currentVersion": installed["version"],
                        "latestVersion": definition.version,
                    }
                )
        return updates

    def uninstall_pocketool(self, selector: str) -> dict[str, Any]:
        return self.pocketools.uninstall(selector)

    def pocketool_help(self, selector: str) -> dict[str, Any]:
        return self.pocketools.help(selector)

    def run_pocketool(
        self,
        selector: str,
        command_name: str,
        arguments: list[str],
        environment_id: str | None = None,
    ) -> int:
        EapApplication._ensure_proxy_authenticated(self)
        profile_id = environment_id or self.active_profile_id()
        installed = self.pocketools.find_installed(selector)
        self._validate_pocketool_component_requirements(
            installed["manifest"]["requires"]["components"],
            profile_id,
            f"{installed['repository']}/{installed['id']}",
        )
        environment = self.environments.build_process_environment(
            profile_id, self.catalog
        )
        return self.pocketools.run(
            selector,
            command_name,
            arguments,
            environment,
        )

    def add_pocketool_repository(
        self, source_id: str, repository_url: str
    ) -> None:
        PocketToolManager._repository_urls(repository_url)
        update_repository_property(
            self.paths.config, source_id, repository_url.strip()
        )
        self._reload_pocketool_settings()

    def remove_pocketool_repository(self, source_id: str) -> None:
        source = self.pocketools.source(source_id)
        update_repository_property(
            self.paths.config,
            source.id,
            None,
        )
        self._reload_pocketool_settings()

    def _reload_pocketool_settings(self) -> None:
        self.settings = Settings.load(self.paths.config)
        self.pocketools = PocketToolManager(
            self.paths,
            self.settings,
            self.client,
            status=self.status,
            reserved_commands=self._component_command_names(),
        )

    def _component_command_names(self) -> set[str]:
        names = {
            str(command["name"])
            for component in self.catalog.definitions.values()
            for command in component.value["environment"].get("commands", [])
        }
        for component in self.catalog.definitions.values():
            install = component.value["install"]
            candidates = [
                *install.get("requiredFiles", []),
                *install.get("executableNames", []),
            ]
            names.update(
                Path(str(candidate)).stem
                for candidate in candidates
                if Path(str(candidate)).suffix.casefold()
                in {".exe", ".cmd", ".bat", ".com"}
            )
        return names

    def _validate_pocketool_component_requirements(
        self,
        requirements: list[dict[str, Any]],
        environment_id: str,
        selector: str,
    ) -> None:
        inventory = self.inventory(environment_id)
        active_capabilities: dict[str, dict[str, Any]] = {}
        for item in inventory:
            component = self.catalog.component(str(item["id"]))
            capability = component.value.get("capability")
            if isinstance(capability, dict) and isinstance(
                capability.get("id"), str
            ):
                active_capabilities[str(capability["id"])] = item
        missing: list[str] = []
        for requirement in requirements:
            capability = str(requirement["capability"])
            minimum = requirement["minimumTrack"]
            active = active_capabilities.get(capability)
            if active is None or not self._track_satisfies(
                active.get("track"), minimum
            ):
                missing.append(f"{capability} (línea >= {minimum})")
        if missing:
            raise ValidationError(
                f"{selector} no puede usarse en el profile {environment_id}; "
                "faltan componentes: " + ", ".join(missing)
            )

    @staticmethod
    def _track_satisfies(current: Any, minimum: Any) -> bool:
        try:
            return int(current) >= int(minimum)
        except (TypeError, ValueError):
            return str(current).casefold() == str(minimum).casefold()

    def resolve(
        self, component_id: str, provider: str, track: int | str
    ) -> ResolvedArtifact:
        EapApplication._ensure_proxy_authenticated(self)
        component = self.catalog.component(component_id)
        if component.is_external:
            raise ValidationError(
                f"{component.display_name} se vincula mediante una ruta local"
            )
        return resolve_component(component, provider, track, self.client)

    def install(
        self,
        environment_id: str,
        component_id: str,
        provider: str,
        track: int | str,
        artifact: ResolvedArtifact | None = None,
        allow_missing: bool = False,
    ) -> tuple[ResolvedArtifact, Path]:
        EapApplication._ensure_proxy_authenticated(self)
        self.environments.read_desired(environment_id)
        component = self.catalog.component(component_id)
        if component.is_external:
            raise ValidationError(
                f"{component.display_name} no se descarga; vincule su ejecutable"
            )
        resolved = artifact or resolve_component(
            component, provider, track, self.client
        )
        if (
            resolved.family != component_id
            or resolved.provider != provider
            or resolved.track != track
        ):
            raise ValidationError("El artefacto resuelto no coincide con la petición")
        installer = ComponentInstaller(
            self.paths,
            self.settings,
            self.client,
            status=self.status,
        )
        process_environment = self.environments.build_process_environment(
            environment_id,
            self.catalog,
            allow_missing=allow_missing,
        )
        install_path, resolved = installer.install(
            component,
            resolved,
            process_environment=process_environment,
        )
        manifest_sha256 = sha256_file(component.manifest_path)
        self.environments.publish_component(
            environment_id,
            resolved,
            install_path,
            manifest_sha256,
            manifest_source=component.manifest_source(),
        )
        self.environments.build_process_environment(
            environment_id, self.catalog
        )
        self._invalidate_update_cache(environment_id)
        return resolved, install_path

    def link_external_component(
        self,
        environment_id: str,
        component_id: str,
        executable: Path,
    ) -> Path:
        self.environments.read_desired(environment_id)
        component = self.catalog.component(component_id)
        if not component.is_external:
            raise ValidationError(
                f"{component.display_name} no es un componente externo"
            )
        candidate = Path(
            os.path.expandvars(str(executable).strip().strip('"'))
        ).expanduser().resolve()
        allowed_names = {
            str(name).casefold()
            for name in component.value["install"]["executableNames"]
        }
        if candidate.name.casefold() not in allowed_names:
            expected = ", ".join(
                component.value["install"]["executableNames"]
            )
            raise ValidationError(
                f"Ejecutable no válido para {component.display_name}; "
                f"se esperaba {expected}"
            )
        if not candidate.is_file():
            raise ValidationError(
                f"No existe el ejecutable externo: {candidate}"
            )
        self.environments.publish_external_component(
            environment_id,
            component,
            candidate,
            sha256_file(component.manifest_path),
            manifest_source=component.manifest_source(),
        )
        self.environments.build_process_environment(
            environment_id, self.catalog
        )
        self._invalidate_update_cache(environment_id)
        return candidate

    def missing_components(
        self, environment_id: str
    ) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for item in self.inventory(environment_id):
            component_id = str(item["id"])
            component = self.catalog.component(component_id)
            if component.is_external:
                installation = item.get("installation", {})
                executable_text = installation.get("executable")
                reason = None
                if (
                    installation.get("type") != "external-executable"
                    or not isinstance(executable_text, str)
                    or not executable_text
                ):
                    reason = "ejecutable externo sin vincular"
                elif not Path(executable_text).is_file():
                    reason = f"ejecutable externo ausente: {executable_text}"
                if reason is not None:
                    missing.append(
                        {
                            **item,
                            "reason": reason,
                            "restorable": False,
                        }
                    )
                continue
            install_path = self.paths.require_within_root(
                self.paths.root / str(item["installPath"])
            )
            try:
                install_path.relative_to(self.paths.components)
            except ValueError as exc:
                raise ValidationError(
                    f"El componente {component_id} sale de components"
                ) from exc
            reason = None
            if not install_path.is_dir():
                reason = "payload ausente"
            else:
                for relative in component.value["install"]["requiredFiles"]:
                    if not (install_path / str(relative)).is_file():
                        reason = f"falta {relative}"
                        break
                marker_path = install_path / ".eap-install.json"
                if reason is None:
                    try:
                        marker = load_json(marker_path)
                    except ValidationError:
                        marker = {}
                    artifact = item.get("artifact", {})
                    if not (
                        marker.get("status") == "ready"
                        and marker.get("component") == component_id
                        and marker.get("provider") == item.get("provider")
                        and str(marker.get("version"))
                        == str(item.get("version"))
                        and marker.get("artifactChecksum")
                        == artifact.get("checksum")
                    ):
                        reason = "marcador de instalación ausente o divergente"
            if reason is not None:
                missing.append(
                    {**item, "reason": reason, "restorable": True}
                )
        return missing

    def restore_missing_components(
        self, environment_id: str
    ) -> list[tuple[ResolvedArtifact, Path]]:
        restored: list[tuple[ResolvedArtifact, Path]] = []
        missing = [
            item
            for item in self.missing_components(environment_id)
            if item.get("restorable", True)
        ]
        for item in missing:
            artifact = self._artifact_from_lock(item)
            restored.append(
                self.install(
                    environment_id,
                    str(item["id"]),
                    str(item["provider"]),
                    item["track"],
                    artifact=artifact,
                    allow_missing=True,
                )
            )
        return restored

    def export_environment(
        self,
        source_environment_id: str,
        exported_environment_id: str,
        include_components: bool,
        include_configuration: bool = False,
        include_custom_commands: bool = False,
        force: bool = False,
    ) -> ExportResult:
        return self._transfer().export_environment(
            source_environment_id,
            exported_environment_id,
            include_components,
            include_configuration=include_configuration,
            include_custom_commands=include_custom_commands,
            force=force,
        )

    def duplicate_profile(
        self, source_profile_id: str, target_profile_id: str
    ) -> None:
        self.environments.duplicate(source_profile_id, target_profile_id)
        self._invalidate_update_cache(target_profile_id)

    def windows_trust_status(self, profile_id: str) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "profile": profile_id,
            "enabled": self.environments.windows_trust_enabled(profile_id),
        }

    def set_windows_trust(
        self, profile_id: str, enabled: bool
    ) -> dict[str, Any]:
        self.environments.set_windows_trust(profile_id, enabled)
        return self.windows_trust_status(profile_id)

    def delete_profile(self, profile_id: str) -> str | None:
        self._invalidate_update_cache(profile_id)
        return self.environments.delete(profile_id)

    def import_environment(self, archive: Path) -> ImportResult:
        return self._transfer().import_environment(archive)

    def export_tool(
        self,
        name: str,
        include_components: bool,
        force: bool = False,
    ) -> ToolExportResult:
        return self._transfer().export_tool(
            name,
            include_components=include_components,
            force=force,
        )

    def _transfer(self) -> EnvironmentTransfer:
        return EnvironmentTransfer(
            self.paths,
            self.settings,
            self.catalog,
            self.environments,
            self.core_tools,
            status=self.status,
        )

    def _artifact_from_lock(
        self, item: dict[str, Any]
    ) -> ResolvedArtifact:
        component = self.catalog.component(str(item["id"]))
        provider = component.provider(str(item["provider"]))
        artifact = item.get("artifact")
        if not isinstance(artifact, dict):
            raise ValidationError(
                f"El lock de {component.id} no contiene un artefacto"
            )
        if artifact.get("localOnly") is True:
            raise ValidationError(
                f"El payload local de {component.display_name} no conserva "
                "su origen de descarga y no puede restaurarse si se elimina"
            )
        algorithm = str(artifact.get("checksumAlgorithm", ""))
        checksum = str(artifact.get("checksum", ""))
        checksum_origin = str(
            artifact.get("checksumOrigin", "published")
        )
        allow_http = artifact.get("allowHttp", False)
        if algorithm not in {"sha256", "sha512"} or not checksum:
            raise ValidationError(
                f"El lock de {component.id} no contiene un checksum válido"
            )
        if (
            checksum_origin not in {"published", "downloaded"}
            or not isinstance(allow_http, bool)
        ):
            raise ValidationError(
                f"El lock de {component.id} contiene un origen no válido"
            )
        return ResolvedArtifact(
            family=component.id,
            component_id=str(item["componentId"]),
            provider=str(item["provider"]),
            provider_name=str(provider["displayName"]),
            track=item["track"],
            version=str(item["version"]),
            url=str(artifact["url"]),
            file_name=str(artifact["fileName"]),
            sha256=checksum if algorithm == "sha256" else None,
            sha512=checksum if algorithm == "sha512" else None,
            size=(
                int(artifact["size"])
                if isinstance(artifact.get("size"), int)
                and int(artifact["size"]) > 0
                else None
            ),
            metadata_url=str(item.get("metadataUrl", artifact["url"])),
            checksum_origin=checksum_origin,
            allow_http=allow_http,
        )

    def inventory(self, environment_id: str) -> list[dict[str, Any]]:
        return list(self.environments.read_lock(environment_id)["components"])

    def available_component_payloads(
        self, environment_id: str
    ) -> list[LocalComponentPayload]:
        self.environments.read_desired(environment_id)
        active_ids = {
            str(item["id"]) for item in self.inventory(environment_id)
        }
        if not self.paths.components.is_dir():
            return []
        available: list[LocalComponentPayload] = []
        for marker_path in self.paths.components.rglob(
            ".eap-install.json"
        ):
            try:
                payload = self._local_payload_from_marker(marker_path)
            except (OSError, ValidationError, ValueError):
                continue
            if payload.component_id not in active_ids:
                available.append(payload)
        return sorted(
            available,
            key=lambda payload: (
                payload.display_name.casefold(),
                payload.provider_name.casefold(),
                self.catalog.component(
                    payload.component_id
                ).comparable_version_key(payload.version),
            ),
        )

    def activate_component_payload(
        self,
        environment_id: str,
        payload: LocalComponentPayload,
    ) -> LocalComponentPayload:
        selected = next(
            (
                candidate
                for candidate in self.available_component_payloads(
                    environment_id
                )
                if (
                    candidate.component_id == payload.component_id
                    and candidate.provider == payload.provider
                    and candidate.version == payload.version
                    and candidate.install_path == payload.install_path
                )
            ),
            None,
        )
        if selected is None:
            raise ValidationError(
                "El payload ya no está disponible para activarlo"
            )
        component = self.catalog.component(selected.component_id)
        self.environments.publish_component(
            environment_id,
            selected.artifact,
            selected.install_path,
            sha256_file(component.manifest_path),
            artifact_restorable=selected.restorable,
            manifest_source=component.manifest_source(),
        )
        self.environments.build_process_environment(
            environment_id, self.catalog
        )
        self._invalidate_update_cache(environment_id)
        return selected

    def disable_component(
        self, environment_id: str, component_id: str
    ) -> dict[str, Any]:
        active = next(
            (
                item
                for item in self.inventory(environment_id)
                if item.get("id") == component_id
            ),
            None,
        )
        if active is not None:
            self._remember_payload_source(active)
        removed = self.environments.disable_component(
            environment_id, component_id
        )
        self._invalidate_update_cache(environment_id)
        return removed

    def _local_payload_from_marker(
        self, marker_path: Path
    ) -> LocalComponentPayload:
        if marker_path.is_symlink() or not marker_path.is_file():
            raise ValidationError("Marcador de payload no válido")
        marker = load_json(marker_path)
        if marker.get("status") != "ready":
            raise ValidationError("El payload no está listo")
        component = self.catalog.component(str(marker.get("component", "")))
        if component.is_external:
            raise ValidationError("Un componente externo no es un payload")
        provider_id = str(marker.get("provider", ""))
        provider = component.provider(provider_id)
        version = validate_version(str(marker.get("version", "")))
        track = component.compatible_track(marker.get("track"), version)
        install_path = marker_path.parent.resolve()
        if marker_path.parent.is_symlink():
            raise ValidationError("El payload no puede ser un enlace")
        template = str(component.value["install"]["directoryTemplate"])
        expected = self.paths.require_within_root(
            self.paths.components
            / template.format(provider=provider_id, version=version)
        )
        if install_path != expected.resolve():
            raise ValidationError("El payload no está en su ruta declarada")
        for relative in component.value["install"]["requiredFiles"]:
            if not (install_path / str(relative)).is_file():
                raise ValidationError(
                    f"El payload no contiene {relative}"
                )
        algorithm = str(marker.get("checksumAlgorithm", ""))
        checksum = str(marker.get("artifactChecksum", "")).casefold()
        expected_length = {"sha256": 64, "sha512": 128}.get(algorithm)
        if (
            expected_length is None
            or len(checksum) != expected_length
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise ValidationError("Checksum de payload no válido")
        artifact, restorable = self._payload_artifact(
            component,
            provider,
            provider_id,
            track,
            version,
            checksum,
            algorithm,
            install_path,
            marker,
        )
        return LocalComponentPayload(
            component_id=component.id,
            display_name=component.display_name,
            provider=provider_id,
            provider_name=str(provider["displayName"]),
            track=track,
            version=version,
            install_path=install_path,
            artifact=artifact,
            restorable=restorable,
        )

    def _payload_artifact(
        self,
        component: ComponentDefinition,
        provider: dict[str, Any],
        provider_id: str,
        track: int | str,
        version: str,
        checksum: str,
        algorithm: str,
        install_path: Path,
        marker: dict[str, Any],
    ) -> tuple[ResolvedArtifact, bool]:
        source = marker.get("source")
        if isinstance(source, dict):
            try:
                return (
                    self._artifact_from_payload_source(
                        component,
                        provider,
                        provider_id,
                        track,
                        version,
                        checksum,
                        algorithm,
                        source,
                    ),
                    True,
                )
            except ValidationError:
                pass
        locked = self._matching_locked_artifact(
            component.id,
            provider_id,
            track,
            version,
            checksum,
            install_path,
        )
        if locked is not None:
            return locked, True
        file_name = f"{component.id}-{version}.payload"
        local_url = (
            f"https://local.invalid/{component.id}/{provider_id}/"
            f"{version}/{file_name}"
        )
        return (
            ResolvedArtifact(
                family=component.id,
                component_id=str(provider["componentId"]),
                provider=provider_id,
                provider_name=str(provider["displayName"]),
                track=track,
                version=version,
                url=local_url,
                file_name=file_name,
                sha256=checksum if algorithm == "sha256" else None,
                sha512=checksum if algorithm == "sha512" else None,
                size=None,
                metadata_url=local_url,
            ),
            False,
        )

    @staticmethod
    def _artifact_from_payload_source(
        component: ComponentDefinition,
        provider: dict[str, Any],
        provider_id: str,
        track: int | str,
        version: str,
        checksum: str,
        algorithm: str,
        source: dict[str, Any],
    ) -> ResolvedArtifact:
        url = source.get("url")
        file_name = source.get("fileName")
        metadata_url = source.get("metadataUrl", url)
        size = source.get("size")
        checksum_origin = source.get("checksumOrigin", "published")
        allow_http = source.get("allowHttp", False)
        if (
            not isinstance(url, str)
            or not url
            or not isinstance(file_name, str)
            or not file_name
            or Path(file_name).name != file_name
            or not isinstance(metadata_url, str)
            or not metadata_url
            or (size is not None and (not isinstance(size, int) or size <= 0))
            or checksum_origin not in {"published", "downloaded"}
            or not isinstance(allow_http, bool)
        ):
            raise ValidationError("Origen de payload no válido")
        HttpClient.require_web_url(url, allow_http=allow_http)
        HttpClient.require_web_url(metadata_url, allow_http=allow_http)
        return ResolvedArtifact(
            family=component.id,
            component_id=str(provider["componentId"]),
            provider=provider_id,
            provider_name=str(provider["displayName"]),
            track=track,
            version=version,
            url=url,
            file_name=file_name,
            sha256=checksum if algorithm == "sha256" else None,
            sha512=checksum if algorithm == "sha512" else None,
            size=size,
            metadata_url=metadata_url,
            checksum_origin=str(checksum_origin),
            allow_http=allow_http,
        )

    def _matching_locked_artifact(
        self,
        component_id: str,
        provider: str,
        track: int | str,
        version: str,
        checksum: str,
        install_path: Path,
    ) -> ResolvedArtifact | None:
        component = self.catalog.component(component_id)
        for environment_id in self.environments.list():
            for item in self.environments.read_lock(environment_id)[
                "components"
            ]:
                if (
                    item.get("id") != component_id
                    or item.get("provider") != provider
                    or str(item.get("version")) != version
                    or not isinstance(item.get("installPath"), str)
                ):
                    continue
                try:
                    locked_track = component.compatible_track(
                        item.get("track"), version
                    )
                    if str(locked_track) != str(track):
                        continue
                    locked_path = self.paths.require_within_root(
                        self.paths.root / str(item["installPath"])
                    )
                    artifact = self._artifact_from_lock(item)
                except (KeyError, ValidationError):
                    continue
                if (
                    locked_path == install_path
                    and artifact.checksum.casefold() == checksum
                ):
                    return replace(artifact, track=track)
        return None

    def _remember_payload_source(self, item: dict[str, Any]) -> None:
        if not isinstance(item.get("installPath"), str):
            return
        try:
            artifact = self._artifact_from_lock(item)
            install_path = self.paths.require_within_root(
                self.paths.root / str(item["installPath"])
            )
            marker_path = install_path / ".eap-install.json"
            marker = load_json(marker_path)
            if (
                marker.get("status") != "ready"
                or marker.get("component") != item.get("id")
                or marker.get("provider") != item.get("provider")
                or str(marker.get("version")) != str(item.get("version"))
                or marker.get("artifactChecksum") != artifact.checksum
            ):
                return
            marker["source"] = {
                "url": artifact.url,
                "fileName": artifact.file_name,
                "metadataUrl": artifact.metadata_url,
                "size": artifact.size,
                "checksumOrigin": artifact.checksum_origin,
                "allowHttp": artifact.allow_http,
            }
            atomic_write_json(marker_path, marker)
        except (KeyError, OSError, ValidationError):
            return

    def uninstall_component(
        self,
        environment_id: str,
        component_id: str,
    ) -> ComponentUninstallResult:
        component = self.catalog.component(component_id)
        if component.is_external:
            raise ValidationError(
                f"{component.display_name} es externo: EAP sólo puede "
                "desvincularlo, no desinstalarlo del equipo"
            )
        active = next(
            (
                item
                for item in self.inventory(environment_id)
                if item.get("id") == component_id
            ),
            None,
        )
        if active is None:
            raise ValidationError(
                f"{component_id} no está activo en el profile {environment_id}"
            )
        lock_path = (
            self.paths.temp
            / "locks"
            / (
                f"install-{component.id}-{active['provider']}-"
                f"{active['track']}.lock"
            )
        )
        with FileLock(lock_path):
            return self._uninstall_component_locked(
                environment_id, component_id
            )

    def _uninstall_component_locked(
        self,
        environment_id: str,
        component_id: str,
    ) -> ComponentUninstallResult:
        active = next(
            (
                item
                for item in self.inventory(environment_id)
                if item.get("id") == component_id
            ),
            None,
        )
        if active is None:
            raise ValidationError(
                f"{component_id} ya no está activo en el profile "
                f"{environment_id}"
            )
        install_path = self._locked_install_path(active)
        shared_profiles = tuple(
            profile_id
            for profile_id in self.environments.list()
            if profile_id != environment_id
            and any(
                self._locked_install_path(item) == install_path
                for item in self.environments.read_lock(profile_id)[
                    "components"
                ]
                if isinstance(item.get("installPath"), str)
            )
        )
        if shared_profiles:
            self.disable_component(environment_id, component_id)
            return ComponentUninstallResult(
                component_id=component_id,
                profile_id=environment_id,
                payload_path=install_path,
                payload_removed=False,
                shared_profiles=shared_profiles,
            )
        if not install_path.exists():
            self.disable_component(environment_id, component_id)
            return ComponentUninstallResult(
                component_id=component_id,
                profile_id=environment_id,
                payload_path=install_path,
                payload_removed=False,
            )
        if install_path.is_symlink() or not install_path.is_dir():
            raise ValidationError(
                f"El payload no es un directorio EAP válido: {install_path}"
            )

        stage_root = (
            self.paths.temp
            / "transactions"
            / f"uninstall-{uuid4().hex}"
        )
        staged_payload = stage_root / install_path.name
        stage_root.mkdir(parents=True)
        try:
            os.replace(install_path, staged_payload)
        except OSError as exc:
            shutil.rmtree(stage_root, ignore_errors=True)
            raise TransactionError(
                f"No se pudo preparar la desinstalación de {component_id}"
            ) from exc
        try:
            self.disable_component(environment_id, component_id)
        except Exception:
            try:
                os.replace(staged_payload, install_path)
                stage_root.rmdir()
            except OSError as rollback_exc:
                raise TransactionError(
                    "Falló la desinstalación y también la restauración "
                    f"del payload desde {staged_payload}"
                ) from rollback_exc
            raise

        try:
            shutil.rmtree(stage_root)
        except OSError:
            return ComponentUninstallResult(
                component_id=component_id,
                profile_id=environment_id,
                payload_path=install_path,
                payload_removed=False,
                residual_path=staged_payload,
            )
        return ComponentUninstallResult(
            component_id=component_id,
            profile_id=environment_id,
            payload_path=install_path,
            payload_removed=True,
        )

    def _locked_install_path(self, item: dict[str, Any]) -> Path:
        relative = item.get("installPath")
        if not isinstance(relative, str) or not relative:
            raise ValidationError("El lock no contiene un installPath válido")
        candidate = self.paths.root / relative
        if candidate.is_symlink():
            raise ValidationError(
                f"El payload no puede ser un enlace simbólico: {candidate}"
            )
        resolved = self.paths.require_within_root(candidate)
        try:
            component_relative = resolved.relative_to(self.paths.components)
        except ValueError as exc:
            raise ValidationError(
                f"El payload queda fuera de components: {resolved}"
            ) from exc
        if not component_relative.parts:
            raise ValidationError(
                "El directorio components completo no puede desinstalarse"
            )
        return resolved

    def check_updates(
        self,
        environment_id: str,
        persist: bool = True,
        errors: dict[str, str] | None = None,
    ) -> list[UpdateInfo]:
        lock = self.environments.read_lock(environment_id)
        updates: list[UpdateInfo] = []
        for item in lock["components"]:
            family = str(item["id"])
            if self.catalog.component(family).is_external:
                continue
            try:
                update = self._resolve_locked_update(item)
            except EapError as exc:
                if errors is None:
                    raise
                errors[family] = str(exc)
                continue
            if update is not None:
                updates.append(update)
        if persist and not errors:
            self._save_update_cache(environment_id, updates)
        return updates

    def resolve_update(
        self, environment_id: str, component_id: str
    ) -> UpdateInfo | None:
        current = next(
            (
                item
                for item in self.inventory(environment_id)
                if item.get("id") == component_id
            ),
            None,
        )
        if current is None:
            raise ValidationError(
                f"{component_id} no está instalado en {environment_id}"
            )
        return self._resolve_locked_update(current)

    def _resolve_locked_update(
        self, current: dict[str, Any]
    ) -> UpdateInfo | None:
        family = str(current["id"])
        component = self.catalog.component(family)
        if component.is_external:
            return None
        provider = str(current["provider"])
        current_version = str(current["version"])
        track = self._effective_update_track(
            component,
            current["track"],
            current_version,
        )
        latest = self.resolve(family, provider, track)
        major_track = self._newer_declared_major_track(
            component, current_version
        )
        if major_track is not None:
            major_candidate = self.resolve(family, provider, major_track)
            if component.comparable_version_key(
                major_candidate.version
            ) > component.comparable_version_key(latest.version):
                latest = major_candidate
                track = major_track
        if component.comparable_version_key(
            latest.version
        ) <= component.comparable_version_key(current_version):
            return None
        major_update = (
            version_key(latest.version)[0]
            > version_key(current_version)[0]
        )
        return UpdateInfo(
            family=family,
            provider=provider,
            track=track,
            current_version=current_version,
            latest=latest,
            major_update=major_update,
        )

    @staticmethod
    def _newer_declared_major_track(
        component: ComponentDefinition, current_version: str
    ) -> int | None:
        if not component.offers_major_updates:
            return None
        current_major = version_key(current_version)[0]
        candidates = [
            int(item["id"])
            for item in component.tracks
            if int(item["id"]) > current_major
        ]
        return max(candidates) if candidates else None

    @staticmethod
    def _effective_update_track(
        component: ComponentDefinition,
        locked_track: int | str,
        current_version: str,
    ) -> int | str:
        return component.compatible_track(locked_track, current_version)

    def update(
        self,
        environment_id: str,
        component_id: str,
        major_confirmation: str | None = None,
    ) -> tuple[ResolvedArtifact, Path] | None:
        if self.catalog.component(component_id).is_external:
            raise ValidationError(
                "Los componentes externos no se actualizan desde EAP; "
                "puede cambiar la ruta de su ejecutable"
            )
        update = self.resolve_update(environment_id, component_id)
        if update is None:
            return None
        if (
            update.major_update
            and major_confirmation != component_id
        ):
            raise ValidationError(
                "La actualización mayor requiere confirmar el nombre "
                f"del componente: {component_id}"
            )
        return self.install(
            environment_id,
            component_id,
            update.provider,
            update.track,
            artifact=update.latest,
        )

    def check_eap_update(self) -> EapUpdateStatus:
        EapApplication._ensure_proxy_authenticated(self)
        return self.release_updater.check(self.version)

    def install_eap_update(
        self, update: EapUpdateStatus | None = None
    ) -> EapUpdateResult:
        selected = update or self.check_eap_update()
        return self.release_updater.install(selected)

    def publish_eap_release(self) -> EapReleaseResult:
        EapApplication._ensure_proxy_authenticated(self)
        publisher = EapReleasePublisher(
            self.paths,
            self.settings.get_int("network.timeoutSeconds", minimum=1),
            user_agent=f"EAP/{self.version}",
            status=self.status,
        )
        return publisher.publish()

    def temporary_storage_usage(self) -> TemporaryStorageUsage:
        size, files = self._storage_usage(self.paths.temp)
        return TemporaryStorageUsage(bytes=size, files=files)

    def clean_temporary_storage(self) -> TemporaryCleanupResult:
        lock_path = self.paths.temp / "locks" / "temp-cleanup.lock"
        bytes_removed = 0
        files_removed = 0
        with FileLock(lock_path):
            active_locks = sorted(
                path.name
                for path in (self.paths.temp / "locks").glob("*.lock")
                if path != lock_path
            )
            if active_locks:
                raise ValidationError(
                    "No se pueden limpiar los temporales mientras hay "
                    "operaciones activas: " + ", ".join(active_locks)
                )
            for entry in self.paths.temp.iterdir():
                if entry == lock_path.parent:
                    continue
                size, files = self._storage_usage(entry)
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        entry.unlink()
                    else:
                        resolved = self.paths.require_within_root(entry)
                        try:
                            resolved.relative_to(self.paths.temp)
                        except ValueError as exc:
                            raise ValidationError(
                                f"Temporal fuera de temp: {resolved}"
                            ) from exc
                        shutil.rmtree(resolved)
                except OSError as exc:
                    raise TransactionError(
                        f"No se pudo eliminar el temporal {entry}"
                    ) from exc
                bytes_removed += size
                files_removed += files
            self.paths.ensure_layout()
        return TemporaryCleanupResult(
            bytes_removed=bytes_removed,
            files_removed=files_removed,
        )

    @staticmethod
    def _storage_usage(path: Path) -> tuple[int, int]:
        if not path.exists() and not path.is_symlink():
            return 0, 0
        if path.is_symlink():
            try:
                return path.lstat().st_size, 1
            except OSError:
                return 0, 1
        if path.is_file():
            try:
                return path.stat().st_size, 1
            except OSError:
                return 0, 1
        total_bytes = 0
        total_files = 0
        for directory, directory_names, file_names in os.walk(
            path, followlinks=False
        ):
            current = Path(directory)
            retained_directories: list[str] = []
            for name in directory_names:
                candidate = current / name
                if candidate.is_symlink():
                    total_files += 1
                    try:
                        total_bytes += candidate.lstat().st_size
                    except OSError:
                        pass
                else:
                    retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in file_names:
                candidate = current / name
                total_files += 1
                try:
                    total_bytes += (
                        candidate.lstat().st_size
                        if candidate.is_symlink()
                        else candidate.stat().st_size
                    )
                except OSError:
                    pass
        return total_bytes, total_files

    def should_check_updates(self, environment_id: str) -> bool:
        if not self.settings.get_bool("update.checkOnStartup"):
            return False
        interval = timedelta(
            hours=self.settings.get_int("update.checkIntervalHours", minimum=0)
        )
        if interval.total_seconds() == 0:
            return True
        cache = self._load_update_cache()
        entry = cache.get("environments", {}).get(environment_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("checkedAt"), str):
            return True
        try:
            checked_at = datetime.fromisoformat(entry["checkedAt"])
        except ValueError:
            return True
        return datetime.now(UTC) - checked_at >= interval

    def cached_updates(self, environment_id: str) -> list[dict[str, Any]]:
        cache = self._load_update_cache()
        entry = cache.get("environments", {}).get(environment_id, {})
        updates = entry.get("updates", []) if isinstance(entry, dict) else []
        return updates if isinstance(updates, list) else []

    def has_cached_update_check(self, environment_id: str) -> bool:
        cache = self._load_update_cache()
        entry = cache.get("environments", {}).get(environment_id)
        return bool(
            isinstance(entry, dict)
            and isinstance(entry.get("checkedAt"), str)
        )

    def _load_update_cache(self) -> dict[str, Any]:
        if not self.update_cache_path.is_file():
            return {"schemaVersion": 1, "environments": {}}
        try:
            cache = load_json(self.update_cache_path)
        except ValidationError:
            return {"schemaVersion": 1, "environments": {}}
        if not isinstance(cache.get("environments"), dict):
            cache["environments"] = {}
        return cache

    def _save_update_cache(
        self, environment_id: str, updates: list[UpdateInfo]
    ) -> None:
        cache = self._load_update_cache()
        cache["schemaVersion"] = 1
        cache["environments"][environment_id] = {
            "checkedAt": utc_now(),
            "updates": [item.as_json() for item in updates],
        }
        atomic_write_json(self.update_cache_path, cache)

    def _invalidate_update_cache(self, environment_id: str) -> None:
        cache = self._load_update_cache()
        environments = cache.get("environments", {})
        if isinstance(environments, dict) and environment_id in environments:
            del environments[environment_id]
            atomic_write_json(self.update_cache_path, cache)

    def open_shell(self, environment_id: str, shell_type: str) -> int:
        EapApplication._ensure_proxy_authenticated(self)
        process_environment = self.environments.build_process_environment(
            environment_id,
            self.catalog,
            allow_missing=True,
        )
        workspace = self.environments.workspace_path(environment_id)
        if shell_type == "cmd":
            executable = Path(process_environment.get("COMSPEC", "cmd.exe"))
            command = [str(executable), "/K"]
            shell_name = "CMD"
        elif shell_type == "powershell":
            system_root = Path(process_environment.get("SystemRoot", r"C:\Windows"))
            executable = (
                system_root
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            command = [str(executable), "-NoLogo", "-NoExit"]
            shell_name = "PowerShell"
        else:
            raise ValidationError(f"Shell no soportado: {shell_type}")
        with console_title(f"EAP ({environment_id}) · {shell_name}"):
            return subprocess.call(
                command,
                cwd=workspace,
                env=process_environment,
            )

    def start_managed_terminal(self, environment_id: str) -> TerminalLaunch:
        EapApplication._ensure_proxy_authenticated(self)
        terminal = ManagedTerminal(
            self.paths,
            self.environments,
            self.catalog,
            self.core_tools,
        )
        return terminal.start(environment_id)

    def available_launchers(self, environment_id: str) -> list[LauncherInfo]:
        process_environment = self.environments.build_process_environment(
            environment_id, self.catalog, allow_missing=True
        )
        profile = self.environments.ensure_profile(environment_id)
        home = profile / "home"
        temporary = home / "AppData" / "Local" / "Temp"
        result: list[LauncherInfo] = []
        launcher_ids: set[str] = set()
        for locked in self.inventory(environment_id):
            component_id = str(locked["id"])
            component = self.catalog.component(component_id)
            external_executable: Path | None = None
            if component.is_external:
                installation = locked.get("installation", {})
                executable_text = installation.get("executable")
                if not isinstance(executable_text, str) or not executable_text:
                    continue
                external_executable = Path(executable_text).resolve()
                if not external_executable.is_file():
                    continue
                install_path = external_executable.parent
            else:
                install_path = self.paths.require_within_root(
                    self.paths.root / str(locked["installPath"])
                )
                try:
                    install_path.relative_to(self.paths.components)
                except ValueError as exc:
                    raise ValidationError(
                        f"El launcher de {component_id} apunta fuera de "
                        "components"
                    ) from exc
                if not install_path.is_dir():
                    continue
            component_data = self.paths.require_within_root(
                profile / "components" / component_id
            )
            for declaration in component.value["launchers"]:
                component_data.mkdir(parents=True, exist_ok=True)
                launcher_id = str(declaration["id"])
                if launcher_id in launcher_ids:
                    raise ValidationError(
                        f"Launcher ambiguo en el profile: {launcher_id}"
                    )
                launcher_ids.add(launcher_id)
                working_directory = (
                    self.environments.launcher_working_directory(
                        environment_id,
                        component_id,
                        str(declaration["workspaceMode"]),
                    )
                )
                tokens = {
                    "{{component.root}}": str(install_path),
                    "{{component.provider}}": str(locked["provider"]),
                    "{{component.version}}": str(locked["version"]),
                    "{{external.executable}}": (
                        str(external_executable)
                        if external_executable is not None
                        else ""
                    ),
                    "{{data.component}}": str(component_data),
                    "{{data.component.uri}}": component_data.as_uri(),
                    "{{profile.root}}": str(profile),
                    "{{profile.home}}": str(home),
                    "{{profile.temp}}": str(temporary),
                    "{{workspace.selected}}": str(working_directory),
                    "{{workspace.root}}": str(working_directory),
                    "{{eap.root}}": str(self.paths.root),
                    "{{profile.id}}": environment_id,
                    "{{environment.id}}": environment_id,
                }
                executable = Path(
                    self._render_launcher_template(
                        str(declaration["executable"]),
                        tokens,
                        component_id,
                        launcher_id,
                    )
                ).resolve()
                if component.is_external:
                    if executable != external_executable:
                        raise ValidationError(
                            f"El launcher {launcher_id} no coincide con el "
                            "ejecutable externo vinculado"
                        )
                else:
                    try:
                        executable.relative_to(install_path)
                    except ValueError as exc:
                        raise ValidationError(
                            f"El ejecutable de {launcher_id} sale del payload"
                        ) from exc
                if not executable.is_file():
                    raise ValidationError(
                        f"No existe el ejecutable de {launcher_id}: {executable}"
                    )
                arguments = tuple(
                    self._render_launcher_template(
                        argument,
                        tokens,
                        component_id,
                        launcher_id,
                    )
                    for argument in declaration["arguments"]
                )
                launcher_environment = dict(process_environment)
                for name in declaration.get("unset", []):
                    for existing in tuple(launcher_environment):
                        if existing.casefold() == str(name).casefold():
                            launcher_environment.pop(existing, None)
                for name, template in declaration.get("environment", {}).items():
                    rendered = self._render_launcher_template(
                        str(template),
                        tokens,
                        component_id,
                        launcher_id,
                    )
                    if str(self.paths.core).casefold() in rendered.casefold():
                        raise ValidationError(
                            f"{launcher_id} intenta publicar core en {name}"
                        )
                    launcher_environment[str(name)] = rendered
                for template in declaration.get("dataDirectories", []):
                    directory = Path(
                        self._render_launcher_template(
                            str(template),
                            tokens,
                            component_id,
                            launcher_id,
                        )
                    ).resolve()
                    try:
                        directory.relative_to(component_data)
                    except ValueError as exc:
                        raise ValidationError(
                            f"El directorio mutable de {launcher_id} sale "
                            "de los datos del componente"
                        ) from exc
                    directory.mkdir(parents=True, exist_ok=True)
                for data_copy in declaration.get("dataCopies", []):
                    source = Path(
                        self._render_launcher_template(
                            str(data_copy["source"]),
                            tokens,
                            component_id,
                            launcher_id,
                        )
                    ).resolve()
                    target = Path(
                        self._render_launcher_template(
                            str(data_copy["target"]),
                            tokens,
                            component_id,
                            launcher_id,
                        )
                    ).resolve()
                    try:
                        source.relative_to(install_path)
                    except ValueError as exc:
                        raise ValidationError(
                            f"La plantilla mutable de {launcher_id} sale "
                            "del payload"
                        ) from exc
                    try:
                        target.relative_to(component_data)
                    except ValueError as exc:
                        raise ValidationError(
                            f"La copia mutable de {launcher_id} sale de "
                            "los datos del componente"
                        ) from exc
                    if not source.is_dir():
                        raise ValidationError(
                            f"No existe la plantilla mutable de "
                            f"{launcher_id}: {source}"
                        )
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(source, target)
                private_home = launcher_environment.get("USERPROFILE")
                if private_home:
                    drive, home_path = os.path.splitdrive(private_home)
                    if drive:
                        launcher_environment["HOMEDRIVE"] = drive
                        launcher_environment["HOMEPATH"] = home_path
                    launcher_environment.pop("HOMESHARE", None)
                result.append(
                    LauncherInfo(
                        id=launcher_id,
                        component_id=component_id,
                        component_name=component.display_name,
                        display_name=str(declaration["displayName"]),
                        executable=executable,
                        arguments=arguments,
                        working_directory=working_directory,
                        start_mode=str(declaration["startMode"]),
                        environment=launcher_environment,
                    )
                )
        return result

    def launch(self, environment_id: str, launcher_id: str) -> int:
        EapApplication._ensure_proxy_authenticated(self)
        matches = [
            launcher
            for launcher in self.available_launchers(environment_id)
            if launcher.id == launcher_id
        ]
        if not matches:
            raise ValidationError(
                f"Launcher {launcher_id!r} no disponible en {environment_id}"
            )
        launcher = matches[0]
        command = [str(launcher.executable), *launcher.arguments]
        if launcher.start_mode == "wait":
            return subprocess.call(
                command,
                cwd=launcher.working_directory,
                env=launcher.environment,
            )
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        process = subprocess.Popen(
            command,
            cwd=launcher.working_directory,
            env=launcher.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        return int(process.pid)

    def create_launcher_shortcut(
        self, environment_id: str, launcher_id: str
    ) -> ShortcutResult:
        launcher = next(
            (
                item
                for item in self.available_launchers(environment_id)
                if item.id == launcher_id
            ),
            None,
        )
        if launcher is None:
            raise ValidationError(
                f"Launcher {launcher_id!r} no disponible en {environment_id}"
            )
        return WindowsShortcutManager(self.paths).create_desktop_shortcut(
            environment_id=environment_id,
            launcher_id=launcher.id,
            display_name=launcher.display_name,
            icon=launcher.executable,
        )

    def host_integration_statuses(
        self, environment_id: str
    ) -> list[HostIntegrationStatus]:
        return self.host_integrations.statuses(environment_id)

    def configured_host_integration_statuses(
        self, environment_id: str
    ) -> list[HostIntegrationStatus]:
        return self.host_integrations.configured_statuses(environment_id)

    def enable_host_integration(
        self,
        environment_id: str,
        integration_id: str,
        *,
        delete_existing: bool = False,
    ) -> HostIntegrationChange:
        return self.host_integrations.enable(
            environment_id,
            integration_id,
            delete_existing=delete_existing,
        )

    def disable_host_integration(
        self, environment_id: str, integration_id: str
    ) -> HostIntegrationStatus:
        return self.host_integrations.disable(
            environment_id, integration_id
        )

    @staticmethod
    def _render_launcher_template(
        template: str,
        tokens: dict[str, str],
        component_id: str,
        launcher_id: str,
    ) -> str:
        rendered = template
        for token, value in tokens.items():
            rendered = rendered.replace(token, value)
        if "{{" in rendered or "}}" in rendered:
            raise ValidationError(
                f"Token desconocido en {component_id}/{launcher_id}: {template}"
            )
        return rendered

    def doctor(self) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []

        def add(name: str, status: str, detail: str) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        add("root", "ok", str(self.paths.root))
        add(
            "python",
            "ok",
            f"runtime privado; EAP {self.version}",
        )
        try:
            from PIL import __version__ as pillow_version

            add("library:pillow", "ok", f"Pillow {pillow_version}")
        except ImportError:
            add(
                "library:pillow",
                "error",
                "Pillow no está disponible en el runtime privado",
            )
        add(
            "catalog",
            "ok",
            f"{len(self.catalog.definitions)} componente(s) válido(s)",
        )
        component_sources = self.component_repositories.cached_sources()
        cached_component_sources = [
            source for source in component_sources if source["revision"]
        ]
        add(
            "components:sources",
            "ok" if cached_component_sources else "warning",
            (
                f"{len(cached_component_sources)}/"
                f"{len(component_sources)} repositorio(s) cacheado(s)"
                if component_sources
                else "sin repositorios configurados"
            ),
        )
        proxy = self.proxy_status()
        if proxy["configured"]:
            proxy_names = ", ".join(
                str(name) for name in dict(proxy["proxies"])
            )
            authentication = (
                f"; autenticación {proxy['authenticationType']}"
                if proxy["authenticationEnabled"]
                else "; sin autenticación interactiva"
            )
            add("proxy", "ok", proxy_names + authentication)
        else:
            add("proxy", "ok", "conexión directa; sin proxy configurado")
        try:
            sources = self.pocketools.sources()
            add(
                "pocketools:sources",
                "ok" if sources else "warning",
                (
                    f"{len(sources)} repositorio(s) configurado(s)"
                    if sources
                    else "sin repositorios configurados"
                ),
            )
            installed_pocketools = self.pocketools.installed()
            failures = [
                (
                    f"{item.get('repository')}/{item.get('id')}: "
                    f"{reason}"
                )
                for item in installed_pocketools
                if (reason := self.pocketools.check_installation(item))
                is not None
            ]
            add(
                "pocketools:installed",
                "error" if failures else "ok",
                "; ".join(failures)
                if failures
                else f"{len(installed_pocketools)} Pocketool(s) válida(s)",
            )
        except ValidationError as exc:
            add("pocketools", "error", str(exc))
        selected = self.environments.selected(
            self.settings.get("profile.default")
        )
        if selected is not None:
            for integration in self.configured_host_integration_statuses(
                selected
            ):
                if integration.ok:
                    integration_status = "ok"
                elif integration.state in {"conflict", "partial"}:
                    integration_status = "error"
                else:
                    integration_status = "warning"
                add(
                    f"host-integration:{integration.id}",
                    integration_status,
                    integration.detail,
                )
        for tool in self.core_tools.definitions.values():
            add(
                f"core-tool:{tool.id}",
                "ok",
                f"{tool.display_name} · {tool.root}",
            )
        for environment_id in self.environments.list():
            try:
                lock = self.environments.read_lock(environment_id)
                configured_variables = (
                    self.environments.configured_environment_variables(
                        environment_id
                    )
                )
                missing = self.missing_components(environment_id)
                if missing:
                    add(
                        f"profile:{environment_id}",
                        "error",
                        "faltan instalaciones: "
                        + ", ".join(str(item["id"]) for item in missing),
                    )
                else:
                    add(
                        f"profile:{environment_id}",
                        "ok",
                        f"{len(lock['components'])} componente(s) · "
                        f"{len(configured_variables)} variable(s) configurada(s)",
                    )
            except ValidationError as exc:
                add(f"profile:{environment_id}", "error", str(exc))

        abandoned_locks = list((self.paths.temp / "locks").glob("*.lock"))
        if abandoned_locks:
            add(
                "locks",
                "warning",
                f"{len(abandoned_locks)} lock(s); comprobar procesos activos",
            )
        else:
            add("locks", "ok", "sin locks pendientes")
        return checks
