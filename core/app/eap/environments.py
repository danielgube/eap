from __future__ import annotations

import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog, ComponentDefinition
from .config import load_properties
from .core_tools import CoreTools
from .errors import TransactionError, ValidationError
from .paths import EapPaths
from .resolvers import ResolvedArtifact
from .util import (
    atomic_write_json,
    atomic_write_text,
    load_json,
    utc_now,
    validate_id,
)


@dataclass(frozen=True)
class EnvironmentFiles:
    root: Path
    desired: Path
    lock: Path
    state: Path
    config: Path


class EnvironmentStore:
    def __init__(self, paths: EapPaths):
        self.paths = paths
        self.global_state = paths.data / "eap-state.json"

    def files(self, environment_id: str) -> EnvironmentFiles:
        validate_id(environment_id, "id de profile")
        root = self.paths.envs / environment_id
        return EnvironmentFiles(
            root=root,
            desired=root / "environment.json",
            lock=root / "environment.lock.json",
            state=root / "state.json",
            config=root / "config.properties",
        )

    def list(self) -> list[str]:
        if not self.paths.envs.exists():
            return []
        result: list[str] = []
        for child in self.paths.envs.iterdir():
            if child.is_dir() and (child / "environment.json").is_file():
                try:
                    validate_id(child.name, "id de profile")
                except ValidationError:
                    continue
                result.append(child.name)
        return sorted(result, key=str.lower)

    def create(
        self,
        environment_id: str,
        workspace_id: str | None = None,
        data_profile_id: str | None = None,
    ) -> EnvironmentFiles:
        validate_id(environment_id, "id de profile")
        selected_workspace = validate_id(
            workspace_id or environment_id, "workspace"
        )
        selected_profile = validate_id(
            data_profile_id or environment_id, "id de datos"
        )
        files = self.files(environment_id)
        if files.root.exists() and any(files.root.iterdir()):
            raise ValidationError(f"El profile ya existe: {environment_id}")
        files.root.mkdir(parents=True, exist_ok=True)
        self._ensure_data_profile(selected_profile)
        atomic_write_json(
            files.desired,
            {
                "schemaVersion": 1,
                "id": environment_id,
                "displayName": environment_id,
                "dataProfile": selected_profile,
                "workspace": selected_workspace,
                "components": [],
            },
        )
        atomic_write_json(
            files.lock,
            {
                "schemaVersion": 1,
                "environmentId": environment_id,
                "generatedAt": utc_now(),
                "components": [],
            },
        )
        atomic_write_json(
            files.state,
            {
                "schemaVersion": 1,
                "lastActivatedAt": None,
            },
        )
        self.ensure_config(environment_id)
        self._workspace_path(selected_workspace).mkdir(
            parents=True, exist_ok=True
        )
        self.select(environment_id)
        return files

    def duplicate(
        self, source_environment_id: str, target_environment_id: str
    ) -> EnvironmentFiles:
        validate_id(target_environment_id, "id de profile")
        source_desired = self.read_desired(source_environment_id)
        source_lock = self.read_lock(source_environment_id)
        source_files = self.files(source_environment_id)
        source_config = self.ensure_config(source_environment_id).read_text(
            encoding="utf-8"
        )
        target_files = self.files(target_environment_id)
        root_preexisted = target_files.root.exists()
        if root_preexisted and any(target_files.root.iterdir()):
            raise ValidationError(
                f"El profile ya existe: {target_environment_id}"
            )

        desired = deepcopy(source_desired)
        desired["id"] = target_environment_id
        desired["displayName"] = target_environment_id
        desired["workspace"] = target_environment_id
        lock = deepcopy(source_lock)
        lock["environmentId"] = target_environment_id
        lock["generatedAt"] = utc_now()
        target_files.root.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_json(target_files.desired, desired)
            atomic_write_json(target_files.lock, lock)
            atomic_write_json(
                target_files.state,
                {
                    "schemaVersion": 1,
                    "lastActivatedAt": None,
                },
            )
            atomic_write_text(target_files.config, source_config)
            self._ensure_data_profile(str(desired["dataProfile"]))
            self._workspace_path(str(desired["workspace"])).mkdir(
                parents=True, exist_ok=True
            )
            self.select(target_environment_id)
        except OSError as exc:
            for path in (
                target_files.desired,
                target_files.lock,
                target_files.state,
                target_files.config,
            ):
                path.unlink(missing_ok=True)
            if not root_preexisted:
                try:
                    target_files.root.rmdir()
                except OSError:
                    pass
            raise TransactionError(
                f"No se pudo duplicar el profile {source_environment_id}"
            ) from exc
        return target_files

    def delete(self, environment_id: str) -> str | None:
        files = self.files(environment_id)
        self.read_desired(environment_id)
        if files.root.is_symlink():
            raise ValidationError(
                f"El profile no puede ser un enlace: {files.root}"
            )
        resolved = self.paths.require_within_root(files.root)
        try:
            relative = resolved.relative_to(self.paths.envs)
        except ValueError as exc:
            raise ValidationError(
                f"El profile queda fuera de envs: {resolved}"
            ) from exc
        if len(relative.parts) != 1:
            raise ValidationError(
                f"Directorio de profile no válido: {resolved}"
            )

        selected_before: str | None = None
        if self.global_state.is_file():
            try:
                selected = load_json(self.global_state).get(
                    "selectedEnvironment"
                )
                if isinstance(selected, str):
                    selected_before = selected
            except ValidationError:
                pass
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise TransactionError(
                f"No se pudo eliminar el profile {environment_id}"
            ) from exc

        remaining = self.list()
        if not remaining:
            self.global_state.unlink(missing_ok=True)
            return None
        if selected_before == environment_id or selected_before not in remaining:
            selected_after = remaining[0]
            self.select(selected_after)
            return selected_after
        return selected_before

    def select(self, environment_id: str) -> None:
        files = self.files(environment_id)
        if not files.desired.is_file():
            raise ValidationError(f"No existe el profile: {environment_id}")
        atomic_write_json(
            self.global_state,
            {
                "schemaVersion": 1,
                "selectedEnvironment": environment_id,
                "updatedAt": utc_now(),
            },
        )

    def selected(self, configured_default: str) -> str | None:
        environments = self.list()
        if not environments:
            return None
        if self.global_state.is_file():
            state = load_json(self.global_state)
            selected = state.get("selectedEnvironment")
            if isinstance(selected, str) and selected in environments:
                return selected
        if configured_default in environments:
            return configured_default
        if len(environments) == 1:
            return environments[0]
        return None

    def read_desired(self, environment_id: str) -> dict[str, Any]:
        files = self.files(environment_id)
        desired = load_json(files.desired)
        if desired.get("id") != environment_id:
            raise ValidationError(
                f"El id interno no coincide en {files.desired}"
            )
        if not isinstance(desired.get("components"), list):
            raise ValidationError(f"components no es una lista en {files.desired}")
        data_profile = desired.get("dataProfile")
        if not isinstance(data_profile, str):
            raise ValidationError(f"dataProfile no es válido en {files.desired}")
        validate_id(data_profile, "id de datos")
        workspace_id = desired.get("workspace")
        if not isinstance(workspace_id, str):
            raise ValidationError(f"workspace no es válido en {files.desired}")
        validate_id(workspace_id, "workspace")
        return desired

    def workspace_path(self, environment_id: str) -> Path:
        desired = self.read_desired(environment_id)
        workspace = self._workspace_path(str(desired["workspace"]))
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def set_workspace(self, environment_id: str, workspace_id: str) -> Path:
        selected_workspace = validate_id(workspace_id, "workspace")
        desired = self.read_desired(environment_id)
        workspace = self._workspace_path(selected_workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        desired["workspace"] = selected_workspace
        atomic_write_json(self.files(environment_id).desired, desired)
        return workspace

    def list_data_profiles(self) -> list[str]:
        profiles_root = self.paths.data / "profiles"
        if not profiles_root.exists():
            return []
        result: list[str] = []
        for child in profiles_root.iterdir():
            if not child.is_dir():
                continue
            try:
                validate_id(child.name, "id de datos")
            except ValidationError:
                continue
            result.append(child.name)
        return sorted(result, key=str.lower)

    def set_data_profile(
        self, environment_id: str, data_profile_id: str
    ) -> Path:
        selected_profile = validate_id(
            data_profile_id, "id de datos"
        )
        desired = self.read_desired(environment_id)
        profile = self._ensure_data_profile(selected_profile)
        desired["dataProfile"] = selected_profile
        atomic_write_json(self.files(environment_id).desired, desired)
        return profile

    def launcher_working_directory(
        self,
        environment_id: str,
        component_id: str,
        workspace_mode: str,
    ) -> Path:
        if workspace_mode == "environment":
            return self.workspace_path(environment_id)
        if workspace_mode != "component-data":
            raise ValidationError(
                f"Modo de workspace no soportado: {workspace_mode}"
            )
        validate_id(component_id, "id de componente")
        profile = self.ensure_profile(environment_id)
        workspace = self.paths.require_within_root(
            profile / "components" / component_id / "workspace"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _workspace_path(self, workspace_id: str) -> Path:
        validate_id(workspace_id, "workspace")
        workspace = self.paths.require_within_root(
            self.paths.workspaces / workspace_id
        )
        try:
            workspace.relative_to(self.paths.workspaces)
        except ValueError as exc:
            raise ValidationError(
                f"El workspace queda fuera de workspaces: {workspace}"
            ) from exc
        return workspace

    def read_lock(self, environment_id: str) -> dict[str, Any]:
        files = self.files(environment_id)
        lock = load_json(files.lock)
        if lock.get("environmentId") != environment_id:
            raise ValidationError(f"Lockfile de otro profile: {files.lock}")
        if not isinstance(lock.get("components"), list):
            raise ValidationError(f"components no es una lista en {files.lock}")
        return lock

    def publish_component(
        self,
        environment_id: str,
        artifact: ResolvedArtifact,
        install_path: Path,
        manifest_sha256: str,
        artifact_restorable: bool = True,
    ) -> None:
        files = self.files(environment_id)
        desired = self.read_desired(environment_id)
        lock = self.read_lock(environment_id)
        previous_desired = deepcopy(desired)
        previous_lock = deepcopy(lock)

        selection = {
            "id": artifact.family,
            "provider": artifact.provider,
            "track": artifact.track,
            "updatePolicy": "same-track",
        }
        desired["components"] = [
            item
            for item in desired["components"]
            if item.get("id") != artifact.family
        ]
        desired["components"].append(selection)

        relative_install = install_path.resolve().relative_to(self.paths.root).as_posix()
        artifact_data = {
            "url": artifact.url,
            "fileName": artifact.file_name,
            "checksumAlgorithm": artifact.checksum_algorithm,
            "checksum": artifact.checksum,
        }
        if artifact.sha256 is not None:
            artifact_data["sha256"] = artifact.sha256
        if artifact.sha512 is not None:
            artifact_data["sha512"] = artifact.sha512
        if artifact.size is not None:
            artifact_data["size"] = artifact.size
        if not artifact_restorable:
            artifact_data["localOnly"] = True
        locked_component = {
            "id": artifact.family,
            "componentId": artifact.component_id,
            "provider": artifact.provider,
            "track": artifact.track,
            "version": artifact.version,
            "installPath": relative_install,
            "artifact": artifact_data,
            "metadataUrl": artifact.metadata_url,
            "manifestSha256": manifest_sha256,
        }
        lock["components"] = [
            item for item in lock["components"] if item.get("id") != artifact.family
        ]
        lock["components"].append(locked_component)
        lock["generatedAt"] = utc_now()

        try:
            atomic_write_json(files.lock, lock)
            atomic_write_json(files.desired, desired)
        except OSError as exc:
            try:
                atomic_write_json(files.lock, previous_lock)
                atomic_write_json(files.desired, previous_desired)
            except OSError as rollback_exc:
                raise TransactionError(
                    "Falló la publicación del profile y también su rollback"
                ) from rollback_exc
            raise TransactionError("No se pudo publicar el profile") from exc

    def publish_external_component(
        self,
        environment_id: str,
        component: ComponentDefinition,
        executable: Path,
        manifest_sha256: str,
    ) -> None:
        if not component.is_external:
            raise ValidationError(
                f"{component.id} no es un componente externo"
            )
        resolved_executable = executable.expanduser().resolve()
        if not resolved_executable.is_file():
            raise ValidationError(
                f"No existe el ejecutable externo: {resolved_executable}"
            )
        provider = str(component.value["defaultProvider"])
        track = component.value["defaultTrack"]
        provider_definition = component.provider(provider)
        component.validate_track(track)
        files = self.files(environment_id)
        desired = self.read_desired(environment_id)
        lock = self.read_lock(environment_id)
        previous_desired = deepcopy(desired)
        previous_lock = deepcopy(lock)
        selection = {
            "id": component.id,
            "provider": provider,
            "track": track,
            "updatePolicy": "manual",
        }
        desired["components"] = [
            item
            for item in desired["components"]
            if item.get("id") != component.id
        ]
        desired["components"].append(selection)
        locked_component = {
            "id": component.id,
            "componentId": str(provider_definition["componentId"]),
            "provider": provider,
            "track": track,
            "version": "local",
            "installation": {
                "type": "external-executable",
                "executable": str(resolved_executable),
            },
            "manifestSha256": manifest_sha256,
        }
        lock["components"] = [
            item
            for item in lock["components"]
            if item.get("id") != component.id
        ]
        lock["components"].append(locked_component)
        lock["generatedAt"] = utc_now()
        try:
            atomic_write_json(files.lock, lock)
            atomic_write_json(files.desired, desired)
        except OSError as exc:
            try:
                atomic_write_json(files.lock, previous_lock)
                atomic_write_json(files.desired, previous_desired)
            except OSError as rollback_exc:
                raise TransactionError(
                    "Falló la vinculación externa y también su rollback"
                ) from rollback_exc
            raise TransactionError(
                "No se pudo publicar el componente externo"
            ) from exc

    def disable_component(
        self, environment_id: str, component_id: str
    ) -> dict[str, Any]:
        validate_id(component_id, "id de componente")
        files = self.files(environment_id)
        desired = self.read_desired(environment_id)
        lock = self.read_lock(environment_id)
        previous_desired = deepcopy(desired)
        previous_lock = deepcopy(lock)
        removed = next(
            (
                item
                for item in lock["components"]
                if item.get("id") == component_id
            ),
            None,
        )
        if removed is None:
            raise ValidationError(
                f"{component_id} no está activo en el profile {environment_id}"
            )
        desired["components"] = [
            item
            for item in desired["components"]
            if item.get("id") != component_id
        ]
        lock["components"] = [
            item
            for item in lock["components"]
            if item.get("id") != component_id
        ]
        lock["generatedAt"] = utc_now()
        try:
            atomic_write_json(files.lock, lock)
            atomic_write_json(files.desired, desired)
        except OSError as exc:
            try:
                atomic_write_json(files.lock, previous_lock)
                atomic_write_json(files.desired, previous_desired)
            except OSError as rollback_exc:
                raise TransactionError(
                    "Falló la desactivación y también su rollback"
                ) from rollback_exc
            raise TransactionError(
                "No se pudo desactivar el componente del profile"
            ) from exc
        return removed

    def build_process_environment(
        self,
        environment_id: str,
        catalog: Catalog,
        allow_missing: bool = False,
    ) -> dict[str, str]:
        declared_component_variables = {
            str(name).casefold()
            for component in catalog.definitions.values()
            for name in (
                *component.value["environment"]["variables"],
                *component.value["environment"].get("unset", []),
            )
        }
        core_text = str(self.paths.core).casefold()
        environment = {
            key: value
            for key, value in os.environ.items()
            if (
                not key.casefold().startswith("eap_bootstrap_host_")
                and (
                    key.casefold() == "path"
                    or core_text not in value.casefold()
                )
            )
        }
        profile = self.ensure_profile(environment_id)
        workspace = self.workspace_path(environment_id)
        home = profile / "home"
        appdata = home / "AppData" / "Roaming"
        local_appdata = home / "AppData" / "Local"
        temporary = local_appdata / "Temp"
        xdg_config = home / ".config"
        xdg_cache = home / ".cache"
        xdg_data = home / ".local" / "share"
        drive, home_path = os.path.splitdrive(str(home))
        environment.update(
            {
                "EAP_ROOT": str(self.paths.root),
                "EAP_PROFILE": environment_id,
                "EAP_ENV": environment_id,
                "EAP_DATA_PROFILE": str(profile),
                "EAP_WORKSPACE": str(workspace),
                "USERPROFILE": str(home),
                "HOME": str(home),
                "APPDATA": str(appdata),
                "LOCALAPPDATA": str(local_appdata),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
                "XDG_CONFIG_HOME": str(xdg_config),
                "XDG_CACHE_HOME": str(xdg_cache),
                "XDG_DATA_HOME": str(xdg_data),
            }
        )
        if drive:
            environment["HOMEDRIVE"] = drive
            environment["HOMEPATH"] = home_path
        environment.pop("HOMESHARE", None)
        lock = self.read_lock(environment_id)
        path_entries: list[str] = []
        for locked in lock["components"]:
            component_id = str(locked["id"])
            component = catalog.component(component_id)
            external_executable: Path | None = None
            if component.is_external:
                installation = locked.get("installation", {})
                executable_text = installation.get("executable")
                if (
                    installation.get("type") != "external-executable"
                    or not isinstance(executable_text, str)
                    or not executable_text
                ):
                    if allow_missing:
                        continue
                    raise ValidationError(
                        f"{component.display_name} no tiene ejecutable vinculado"
                    )
                external_executable = Path(executable_text).resolve()
                if not external_executable.is_file():
                    if allow_missing:
                        continue
                    raise ValidationError(
                        "No existe el ejecutable externo: "
                        f"{external_executable}"
                    )
                install_path = external_executable.parent
            else:
                install_path = self.paths.require_within_root(
                    self.paths.root / str(locked["installPath"])
                )
                try:
                    install_path.relative_to(self.paths.components)
                except ValueError as exc:
                    raise ValidationError(
                        "El componente apunta fuera de components: "
                        f"{install_path}"
                    ) from exc
                if not install_path.is_dir():
                    if allow_missing:
                        continue
                    raise ValidationError(
                        f"No existe la instalación bloqueada: {install_path}"
                    )
            self.ensure_component_data(
                environment_id, component, locked
            )
            tokens = {
                "{{component.root}}": str(install_path),
                "{{external.executable}}": (
                    str(external_executable)
                    if external_executable is not None
                    else ""
                ),
                "{{profile.root}}": str(profile),
                "{{profile.home}}": str(home),
                "{{profile.temp}}": str(temporary),
                "{{data.component}}": str(
                    profile / "components" / component_id
                ),
                "{{workspace.root}}": str(workspace),
                "{{workspace.selected}}": str(workspace),
                "{{eap.root}}": str(self.paths.root),
                "{{profile.id}}": environment_id,
                "{{environment.id}}": environment_id,
            }
            declaration = component.value["environment"]
            managed_names = (
                *declaration["variables"].keys(),
                *declaration.get("unset", []),
            )
            for name in managed_names:
                normalized_name = str(name).casefold()
                for existing_name in list(environment):
                    if existing_name.casefold() == normalized_name:
                        environment.pop(existing_name)
            for name, template in declaration["variables"].items():
                rendered = self._render_environment_template(
                    str(template), tokens, component_id
                )
                if str(self.paths.core).casefold() in rendered.casefold():
                    raise ValidationError(
                        f"{component_id} intenta publicar core en {name}"
                    )
                environment[str(name)] = rendered
            for template in declaration["path"]:
                rendered = self._render_environment_template(
                    str(template), tokens, component_id
                )
                path_entry = Path(rendered).resolve()
                try:
                    path_entry.relative_to(install_path)
                except ValueError as exc:
                    raise ValidationError(
                        f"{component_id} publica un PATH fuera de su instalación"
                    ) from exc
                path_entries.append(str(path_entry))
            commands = declaration.get("commands", [])
            if commands:
                command_root = self.paths.require_within_root(
                    profile / "components" / component_id / "bin"
                )
                command_root.mkdir(parents=True, exist_ok=True)
                for command in commands:
                    executable_text = self._render_environment_template(
                        str(command["executable"]), tokens, component_id
                    )
                    executable = Path(executable_text).resolve()
                    try:
                        executable.relative_to(install_path)
                    except ValueError as exc:
                        raise ValidationError(
                            f"{component_id} genera un comando fuera de su instalación"
                        ) from exc
                    if not executable.is_file():
                        raise ValidationError(
                            f"No existe el ejecutable de {component_id}: {executable}"
                        )
                    arguments = " ".join(str(item) for item in command["arguments"])
                    escaped_executable = str(executable).replace("%", "%%")
                    content = (
                        "@echo off\n"
                        f'"{escaped_executable}" {arguments} %*\n'
                    )
                    command_path = command_root / f"{command['name']}.cmd"
                    if (
                        not command_path.is_file()
                        or command_path.read_text(encoding="utf-8") != content
                    ):
                        command_path.write_text(
                            content, encoding="utf-8", newline="\r\n"
                        )
                path_entries.append(str(command_root))
            for template in declaration.get("dataPath", []):
                rendered = self._render_environment_template(
                    str(template), tokens, component_id
                )
                path_entry = Path(rendered).resolve()
                try:
                    path_entry.relative_to(profile)
                except ValueError as exc:
                    raise ValidationError(
                        f"{component_id} publica un dataPath fuera de su perfil"
                    ) from exc
                path_entry.mkdir(parents=True, exist_ok=True)
                path_entries.append(str(path_entry))

        if any(core_text in entry.casefold() for entry in path_entries):
            raise ValidationError("El runtime del core no puede entrar en PATH")
        core_tool_entries = [
            str(path.resolve())
            for path in CoreTools.load(self.paths).environment_path_entries()
        ]
        base_entries: list[str] = []
        for entry in environment.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            normalized = Path(entry.strip('"'))
            try:
                normalized.resolve().relative_to(self.paths.core)
                continue
            except (OSError, ValueError):
                pass
            try:
                normalized.resolve().relative_to(self.paths.components)
                continue
            except (OSError, ValueError):
                base_entries.append(entry)
        environment["PATH"] = os.pathsep.join(
            [*path_entries, *core_tool_entries, *base_entries]
        )
        environment["EAP_CORE_TOOLS"] = os.pathsep.join(core_tool_entries)
        self._apply_configured_environment_variables(
            environment,
            environment_id,
            declared_component_variables,
        )
        return environment

    def component_data_entries(
        self,
        environment_id: str,
        component: Any,
        locked: dict[str, Any],
    ) -> list[dict[str, Any]]:
        profile = self.ensure_profile(environment_id)
        home = profile / "home"
        workspace = self.workspace_path(environment_id)
        component_id = str(locked["id"])
        external_executable = ""
        if component.is_external:
            installation = locked.get("installation", {})
            executable_text = installation.get("executable")
            if isinstance(executable_text, str) and executable_text:
                external_path = Path(executable_text).resolve()
                external_executable = str(external_path)
                install_path = external_path.parent
            else:
                install_path = profile / "components" / component_id
        else:
            install_path = self.paths.require_within_root(
                self.paths.root / str(locked["installPath"])
            )
        tokens = {
            "{{component.root}}": str(install_path),
            "{{component.version}}": str(locked["version"]),
            "{{external.executable}}": external_executable,
            "{{profile.root}}": str(profile),
            "{{profile.home}}": str(home),
            "{{profile.temp}}": str(
                home / "AppData" / "Local" / "Temp"
            ),
            "{{data.component}}": str(
                profile / "components" / component_id
            ),
            "{{workspace.root}}": str(workspace),
            "{{workspace.selected}}": str(workspace),
            "{{eap.root}}": str(self.paths.root),
            "{{profile.id}}": environment_id,
            "{{environment.id}}": environment_id,
        }
        result: list[dict[str, Any]] = []
        declaration = component.value.get("data", {})
        for entry_type, collection in (
            ("directory", "directories"),
            ("file", "files"),
        ):
            for entry in declaration.get(collection, []):
                rendered = self._render_environment_template(
                    str(entry["path"]), tokens, component_id
                )
                target = Path(rendered).resolve()
                try:
                    target.relative_to(profile)
                except ValueError as exc:
                    raise ValidationError(
                        f"Los datos de {component_id} salen de su perfil: "
                        f"{target}"
                    ) from exc
                result.append(
                    {
                        "type": entry_type,
                        "path": target,
                        "displayName": str(entry["displayName"]),
                        "role": str(entry["role"]),
                        "showInDashboard": bool(
                            entry.get("showInDashboard", True)
                        ),
                        "mode": entry.get("mode"),
                        "content": entry.get("content"),
                        "tokens": tokens,
                    }
                )
        return result

    def ensure_component_data(
        self,
        environment_id: str,
        component: Any,
        locked: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entries = self.component_data_entries(
            environment_id, component, locked
        )
        component_id = str(locked["id"])
        for entry in entries:
            target = entry["path"]
            if entry["type"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file():
                    raise ValidationError(
                        f"El archivo administrado de {component_id} no es "
                        f"un archivo: {target}"
                    )
                continue
            content = self._render_environment_template(
                str(entry["content"]),
                entry["tokens"],
                component_id,
            )
            atomic_write_text(target, content)
        return entries

    def ensure_config(self, environment_id: str) -> Path:
        files = self.files(environment_id)
        if not files.desired.is_file():
            raise ValidationError(f"No existe el profile: {environment_id}")
        if not files.config.exists():
            files.config.write_text(
                "# Variables privadas de este profile. No comparta este archivo.\n"
                "# Use env.NOMBRE=valor; por ejemplo: env.GITHUB_TOKEN=...\n",
                encoding="utf-8",
                newline="\n",
            )
        return files.config

    def configured_environment_variables(
        self, environment_id: str
    ) -> dict[str, str]:
        """Return merged global/per-environment env.* values, local wins."""
        self.read_desired(environment_id)
        local_path = self.ensure_config(environment_id)
        merged: dict[str, tuple[str, str]] = {}
        for path in (self.paths.config, local_path):
            for key, value in load_properties(path).items():
                if not key.lower().startswith("env."):
                    continue
                name = key[4:].strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    raise ValidationError(
                        f"Variable de entorno inválida {name!r} en {path}"
                    )
                merged[name.casefold()] = (name, value)
        return {name: value for name, value in merged.values()}

    def _apply_configured_environment_variables(
        self,
        environment: dict[str, str],
        environment_id: str,
        declared_component_variables: set[str],
    ) -> None:
        protected = {
            "path",
            "eap_root",
            "eap_profile",
            "eap_env",
            "eap_data_profile",
            "eap_workspace",
            "eap_core_tools",
            "userprofile",
            "home",
            "appdata",
            "localappdata",
            "temp",
            "tmp",
            "tmpdir",
            "homedrive",
            "homepath",
            "homeshare",
            "xdg_config_home",
            "xdg_cache_home",
            "xdg_data_home",
            *declared_component_variables,
        }
        existing_names = {name.casefold(): name for name in environment}
        for name, value in self.configured_environment_variables(
            environment_id
        ).items():
            folded = name.casefold()
            if folded in protected:
                raise ValidationError(
                    f"env.{name} no puede sobrescribir una variable gestionada por EAP"
                )
            previous = existing_names.get(folded)
            if previous is not None and previous != name:
                environment.pop(previous, None)
            environment[name] = value
            existing_names[folded] = name

    @staticmethod
    def _render_environment_template(
        template: str,
        tokens: dict[str, str],
        component_id: str,
    ) -> str:
        rendered = template
        for token, value in tokens.items():
            rendered = rendered.replace(token, value)
        if "{{" in rendered or "}}" in rendered:
            raise ValidationError(
                f"Token desconocido en el entorno de {component_id}: {template}"
            )
        return rendered

    def ensure_profile(self, environment_id: str) -> Path:
        desired = self.read_desired(environment_id)
        profile_id = validate_id(
            str(desired["dataProfile"]), "id de datos"
        )
        return self._ensure_data_profile(profile_id)

    def _ensure_data_profile(self, profile_id: str) -> Path:
        validate_id(profile_id, "id de datos")
        profile = self.paths.require_within_root(
            self.paths.data / "profiles" / profile_id
        )
        directories = (
            profile / "home",
            profile / "home" / "AppData" / "Roaming",
            profile / "home" / "AppData" / "Local",
            profile / "home" / "AppData" / "Local" / "Temp",
            profile / "home" / ".config",
            profile / "home" / ".cache",
            profile / "home" / ".local" / "share",
            profile / "components",
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return profile
