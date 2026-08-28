from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ctypes import wintypes

from .environments import EnvironmentStore
from .errors import TransactionError, ValidationError
from .paths import EapPaths
from .util import atomic_write_json, load_json, utc_now, validate_id


_BOOTSTRAP_VARIABLES = {
    "userProfile": "EAP_BOOTSTRAP_HOST_USERPROFILE",
    "roamingAppData": "EAP_BOOTSTRAP_HOST_APPDATA",
    "localAppData": "EAP_BOOTSTRAP_HOST_LOCALAPPDATA",
}
_HOST_ROOTS = {
    "host.userProfile": "user_profile",
    "host.roamingAppData": "roaming_app_data",
    "host.localAppData": "local_app_data",
}
_PROFILE_ROOTS = {
    "profile.userProfile": (),
    "profile.roamingAppData": ("AppData", "Roaming"),
    "profile.localAppData": ("AppData", "Local"),
}
_CREATE_JUNCTION = r"""
$ErrorActionPreference = 'Stop'
New-Item `
    -ItemType Junction `
    -Path $env:EAP_JUNCTION_PATH `
    -Target $env:EAP_JUNCTION_TARGET | Out-Null
""".strip()


@dataclass(frozen=True)
class HostContext:
    machine: str
    account: str
    user_profile: Path
    roaming_app_data: Path
    local_app_data: Path


@dataclass(frozen=True)
class HostLinkDefinition:
    source_root: str
    source: PurePosixPath
    destination_root: str
    destination: PurePosixPath
    required_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class HostIntegrationDefinition:
    id: str
    display_name: str
    description: str
    processes: tuple[str, ...]
    links: tuple[HostLinkDefinition, ...]


@dataclass(frozen=True)
class ResolvedHostLink:
    source: Path
    destination: Path
    required_paths: tuple[Path, ...]


@dataclass(frozen=True)
class HostIntegrationStatus:
    id: str
    display_name: str
    description: str
    data_profile: str
    state: str
    ok: bool
    detail: str
    links: tuple[ResolvedHostLink, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "description": self.description,
            "dataProfile": self.data_profile,
            "state": self.state,
            "ok": self.ok,
            "detail": self.detail,
            "links": [
                {
                    "source": str(link.source),
                    "destination": str(link.destination),
                }
                for link in self.links
            ],
        }


@dataclass(frozen=True)
class HostIntegrationChange:
    status: HostIntegrationStatus
    deleted_directories: tuple[Path, ...] = ()
    deleted_files: int = 0
    deleted_bytes: int = 0


class HostIntegrationCatalog:
    def __init__(self, definitions: tuple[HostIntegrationDefinition, ...]):
        self.definitions = definitions
        self.by_id = {definition.id: definition for definition in definitions}

    @classmethod
    def load(cls, path: Path) -> "HostIntegrationCatalog":
        value = load_json(path)
        if value.get("schemaVersion") != 1:
            raise ValidationError(
                f"Schema de integraciones con el host no soportado: {path}"
            )
        items = value.get("integrations")
        if not isinstance(items, list):
            raise ValidationError(
                f"integrations debe ser una lista en {path}"
            )
        definitions: list[HostIntegrationDefinition] = []
        identifiers: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError(f"Integración no válida en {path}")
            integration_id = validate_id(
                str(item.get("id", "")), "id de integración"
            )
            if integration_id in identifiers:
                raise ValidationError(
                    f"Integración duplicada {integration_id!r} en {path}"
                )
            identifiers.add(integration_id)
            display_name = item.get("displayName")
            description = item.get("description")
            processes = item.get("processes", [])
            links = item.get("links")
            if (
                not isinstance(display_name, str)
                or not display_name
                or not isinstance(description, str)
                or not description
                or not isinstance(processes, list)
                or not all(
                    isinstance(process, str)
                    and process
                    and Path(process).name == process
                    for process in processes
                )
                or not isinstance(links, list)
                or not links
            ):
                raise ValidationError(
                    f"Definición de integración no válida en {path}"
                )
            definitions.append(
                HostIntegrationDefinition(
                    id=integration_id,
                    display_name=display_name,
                    description=description,
                    processes=tuple(processes),
                    links=tuple(
                        cls._parse_link(link, path) for link in links
                    ),
                )
            )
        return cls(tuple(definitions))

    @staticmethod
    def _parse_link(value: Any, path: Path) -> HostLinkDefinition:
        if not isinstance(value, dict) or value.get("kind") != "junction":
            raise ValidationError(f"Enlace de host no válido en {path}")
        source_root = value.get("sourceRoot")
        destination_root = value.get("destinationRoot")
        if source_root not in _HOST_ROOTS or destination_root not in _PROFILE_ROOTS:
            raise ValidationError(f"Raíz de enlace no válida en {path}")
        source = _relative_path(value.get("source"), "source", path)
        destination = _relative_path(
            value.get("destination"), "destination", path
        )
        required = value.get("requiredPaths", [])
        if not isinstance(required, list):
            raise ValidationError(f"requiredPaths no es una lista en {path}")
        return HostLinkDefinition(
            source_root=str(source_root),
            source=source,
            destination_root=str(destination_root),
            destination=destination,
            required_paths=tuple(
                _relative_path(item, "requiredPath", path)
                for item in required
            ),
        )

    def definition(self, integration_id: str) -> HostIntegrationDefinition:
        try:
            return self.by_id[integration_id]
        except KeyError as exc:
            raise ValidationError(
                f"Integración con el host desconocida: {integration_id}"
            ) from exc


class HostIntegrationManager:
    def __init__(self, paths: EapPaths, environments: EnvironmentStore):
        self.paths = paths
        self.environments = environments
        self.catalog = HostIntegrationCatalog.load(
            paths.core / "catalog" / "host-integrations.json"
        )
        self.context_path = paths.data / "host-context.json"
        self.context, self.context_error = self._load_host_context()

    def statuses(self, environment_id: str) -> list[HostIntegrationStatus]:
        return [
            self.status(environment_id, definition.id)
            for definition in self.catalog.definitions
        ]

    def configured_statuses(
        self, environment_id: str
    ) -> list[HostIntegrationStatus]:
        statuses = self.statuses(environment_id)
        if not statuses:
            return []
        data_profile = statuses[0].data_profile
        enabled = self._enabled_integrations(data_profile)
        adopted = {
            status.id for status in statuses if status.ok
        }
        if not adopted.issubset(enabled):
            enabled.update(adopted)
            self._write_enabled_integrations(data_profile, enabled)
        return [
            status for status in statuses if status.id in enabled
        ]

    def status(
        self, environment_id: str, integration_id: str
    ) -> HostIntegrationStatus:
        definition = self.catalog.definition(integration_id)
        desired = self.environments.read_desired(environment_id)
        data_profile = str(desired["dataProfile"])
        profile = self.environments.ensure_profile(environment_id)
        links = self._resolve_links(definition, profile)
        if self.context is None:
            return self._status(
                definition,
                data_profile,
                "host-context-unavailable",
                False,
                self.context_error or "contexto del host no disponible",
                links,
            )
        source_problem = self._source_problem(links)
        states = [self._destination_state(link) for link in links]
        if all(state == "active" for state in states) and source_problem is None:
            return self._status(
                definition,
                data_profile,
                "active",
                True,
                "datos compartidos correctamente con el host",
                links,
            )
        if any(state == "wrong-junction" for state in states):
            detail = "existe un junction dirigido a otro destino"
            state = "conflict"
        elif any(state == "file" for state in states):
            detail = "el destino está ocupado por un archivo"
            state = "conflict"
        elif any(state == "active" for state in states):
            detail = "la integración está aplicada sólo parcialmente"
            state = "partial"
        elif source_problem is not None:
            detail = source_problem
            state = "source-unavailable"
        elif any(state == "directory" for state in states):
            detail = "hay datos portables locales; integración no activa"
            state = "inactive-with-data"
        else:
            detail = "integración no activa"
            state = "inactive"
        return self._status(
            definition, data_profile, state, False, detail, links
        )

    def enable(
        self,
        environment_id: str,
        integration_id: str,
        *,
        delete_existing: bool = False,
    ) -> HostIntegrationChange:
        definition = self.catalog.definition(integration_id)
        before = self.status(environment_id, integration_id)
        if before.ok:
            self._set_integration_enabled(
                before.data_profile, integration_id, True
            )
            return HostIntegrationChange(status=before)
        if self.context is None:
            raise ValidationError(before.detail)
        links = before.links
        source_problem = self._source_problem(links)
        if source_problem is not None:
            raise ValidationError(source_problem)
        self._require_processes_stopped(definition)

        existing_directories: list[Path] = []
        for link in links:
            destination_state = self._destination_state(link)
            if destination_state == "directory":
                existing_directories.append(link.destination)
            elif destination_state == "active":
                continue
            elif destination_state != "missing":
                raise ValidationError(
                    f"No se puede sustituir el destino: {link.destination}"
                )
        if existing_directories and not delete_existing:
            joined = ", ".join(str(path) for path in existing_directories)
            raise ValidationError(
                "El directorio de destino ya existe; se requiere confirmar "
                f"su borrado: {joined}"
            )

        deleted_files = 0
        deleted_bytes = 0
        for directory in existing_directories:
            files, size = _directory_usage(directory)
            deleted_files += files
            deleted_bytes += size

        staged: list[tuple[ResolvedHostLink, Path]] = []
        trash_root = self.paths.temp / "host-integration-trash" / uuid.uuid4().hex
        moved_to_trash: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            for link in links:
                if self._destination_state(link) == "active":
                    continue
                link.destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_link = (
                    link.destination.parent
                    / f".{link.destination.name}.eap-{uuid.uuid4().hex}"
                )
                self._create_junction(temporary_link, link.source)
                if self._junction_target(temporary_link) != _path_key(
                    link.source.resolve()
                ):
                    raise TransactionError(
                        f"El junction de prueba no apunta a {link.source}"
                    )
                staged.append((link, temporary_link))

            if existing_directories:
                trash_root.mkdir(parents=True, exist_ok=False)
            for index, directory in enumerate(existing_directories):
                trash = trash_root / f"{index}-{directory.name}"
                directory.rename(trash)
                moved_to_trash.append((directory, trash))

            for link, temporary_link in staged:
                temporary_link.rename(link.destination)
                installed.append(link.destination)
            after = self.status(environment_id, integration_id)
            if not after.ok:
                raise TransactionError(
                    f"La integración no ha quedado activa: {after.detail}"
                )
            self._set_integration_enabled(
                after.data_profile, integration_id, True
            )
        except Exception:
            for destination in reversed(installed):
                if destination.is_junction():
                    os.rmdir(destination)
            for destination, trash in reversed(moved_to_trash):
                if trash.exists() and not destination.exists():
                    trash.rename(destination)
            for _link, temporary_link in staged:
                if temporary_link.is_junction():
                    os.rmdir(temporary_link)
            if trash_root.exists() and not any(trash_root.iterdir()):
                trash_root.rmdir()
            raise

        if trash_root.exists():
            try:
                shutil.rmtree(trash_root)
            except OSError as exc:
                raise TransactionError(
                    "La integración está activa, pero no se pudo completar "
                    f"el borrado temporal: {exc}"
                ) from exc
        return HostIntegrationChange(
            status=self.status(environment_id, integration_id),
            deleted_directories=tuple(existing_directories),
            deleted_files=deleted_files,
            deleted_bytes=deleted_bytes,
        )

    def disable(
        self, environment_id: str, integration_id: str
    ) -> HostIntegrationStatus:
        definition = self.catalog.definition(integration_id)
        before = self.status(environment_id, integration_id)
        self._require_processes_stopped(definition)
        removed: list[ResolvedHostLink] = []
        try:
            for link in before.links:
                state = self._destination_state(link)
                if state == "active":
                    os.rmdir(link.destination)
                    removed.append(link)
                elif state not in {"missing", "directory"}:
                    raise ValidationError(
                        "No se puede retirar de forma segura: "
                        f"{link.destination}"
                    )
            self._set_integration_enabled(
                before.data_profile, integration_id, False
            )
        except Exception:
            for link in removed:
                if not link.destination.exists():
                    self._create_junction(link.destination, link.source)
            raise
        return self.status(environment_id, integration_id)

    def profiles_using_data(self, data_profile: str) -> tuple[str, ...]:
        result = [
            environment_id
            for environment_id in self.environments.list()
            if self.environments.read_desired(environment_id).get(
                "dataProfile"
            )
            == data_profile
        ]
        return tuple(result)

    def _enabled_integrations(self, data_profile: str) -> set[str]:
        path = self._integration_state_path(data_profile)
        if not path.is_file():
            return set()
        value = load_json(path)
        if value.get("schemaVersion") != 1:
            raise ValidationError(
                f"Schema de integraciones activas no soportado: {path}"
            )
        enabled = value.get("enabled")
        if not isinstance(enabled, list) or not all(
            isinstance(item, str) and item in self.catalog.by_id
            for item in enabled
        ):
            raise ValidationError(
                f"Estado de integraciones activas no válido: {path}"
            )
        return set(enabled)

    def _set_integration_enabled(
        self,
        data_profile: str,
        integration_id: str,
        enabled: bool,
    ) -> None:
        identifiers = self._enabled_integrations(data_profile)
        if enabled:
            identifiers.add(integration_id)
        else:
            identifiers.discard(integration_id)
        self._write_enabled_integrations(data_profile, identifiers)

    def _write_enabled_integrations(
        self, data_profile: str, enabled: set[str]
    ) -> None:
        path = self._integration_state_path(data_profile)
        atomic_write_json(
            path,
            {
                "schemaVersion": 1,
                "updatedAt": utc_now(),
                "enabled": sorted(enabled),
            },
        )

    def _integration_state_path(self, data_profile: str) -> Path:
        validate_id(data_profile, "id de datos")
        profile = self.paths.require_within_root(
            self.paths.data / "profiles" / data_profile
        )
        return profile / "host-integrations.json"

    def _load_host_context(self) -> tuple[HostContext | None, str | None]:
        bootstrap = {
            key: os.environ.get(variable, "").strip()
            for key, variable in _BOOTSTRAP_VARIABLES.items()
        }
        if all(bootstrap.values()):
            try:
                context = self._validated_context(
                    machine=os.environ.get("COMPUTERNAME", ""),
                    account=_account_name(),
                    values=bootstrap,
                )
                atomic_write_json(
                    self.context_path,
                    {
                        "schemaVersion": 1,
                        "machine": context.machine,
                        "account": context.account,
                        "capturedAt": utc_now(),
                        "userProfile": str(context.user_profile),
                        "roamingAppData": str(context.roaming_app_data),
                        "localAppData": str(context.local_app_data),
                    },
                )
                return context, None
            except (OSError, ValidationError) as exc:
                return None, f"contexto del host no válido: {exc}"
        if not self.context_path.is_file():
            return (
                None,
                "contexto del host no capturado; abra EAP desde Windows",
            )
        try:
            saved = load_json(self.context_path)
            if saved.get("schemaVersion") != 1:
                raise ValidationError("schema de contexto no soportado")
            machine = str(saved.get("machine", ""))
            account = str(saved.get("account", ""))
            current_machine = os.environ.get("COMPUTERNAME", "")
            current_account = _account_name()
            if (
                machine
                and current_machine
                and machine.casefold() != current_machine.casefold()
            ):
                raise ValidationError("el contexto pertenece a otro equipo")
            if (
                account
                and current_account
                and account.casefold() != current_account.casefold()
            ):
                raise ValidationError("el contexto pertenece a otro usuario")
            return (
                self._validated_context(
                    machine=machine,
                    account=account,
                    values={
                        "userProfile": saved.get("userProfile"),
                        "roamingAppData": saved.get("roamingAppData"),
                        "localAppData": saved.get("localAppData"),
                    },
                ),
                None,
            )
        except (OSError, ValidationError) as exc:
            return None, f"contexto del host no válido: {exc}"

    def _validated_context(
        self,
        *,
        machine: str,
        account: str,
        values: dict[str, Any],
    ) -> HostContext:
        resolved: dict[str, Path] = {}
        for name in ("userProfile", "roamingAppData", "localAppData"):
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"falta {name}")
            path = Path(value).resolve()
            if not path.is_dir():
                raise ValidationError(f"no existe {path}")
            try:
                path.relative_to(self.paths.root)
            except ValueError:
                pass
            else:
                raise ValidationError(
                    f"{name} apunta dentro de EAP: {path}"
                )
            resolved[name] = path
        return HostContext(
            machine=machine,
            account=account,
            user_profile=resolved["userProfile"],
            roaming_app_data=resolved["roamingAppData"],
            local_app_data=resolved["localAppData"],
        )

    def _resolve_links(
        self,
        definition: HostIntegrationDefinition,
        profile: Path,
    ) -> tuple[ResolvedHostLink, ...]:
        links: list[ResolvedHostLink] = []
        for link in definition.links:
            if self.context is None:
                source_root = Path("<host-no-disponible>")
            else:
                source_root = Path(
                    getattr(self.context, _HOST_ROOTS[link.source_root])
                )
            destination_root = profile / "home"
            for part in _PROFILE_ROOTS[link.destination_root]:
                destination_root /= part
            source = source_root.joinpath(*link.source.parts)
            destination = destination_root.joinpath(*link.destination.parts)
            _require_lexically_within(destination, profile / "home")
            links.append(
                ResolvedHostLink(
                    source=source,
                    destination=destination,
                    required_paths=tuple(
                        source.joinpath(*required.parts)
                        for required in link.required_paths
                    ),
                )
            )
        return tuple(links)

    @staticmethod
    def _source_problem(links: tuple[ResolvedHostLink, ...]) -> str | None:
        for link in links:
            if str(link.source).startswith("<host-no-disponible>"):
                return "contexto del host no disponible"
            if _is_network_path(link.source):
                return f"los junctions no admiten destinos de red: {link.source}"
            if not _directory_exists(link.source):
                return f"no existe el directorio del host: {link.source}"
            for required in link.required_paths:
                if not _path_exists(required):
                    return f"Firefox no está inicializado en el host: {required}"
        return None

    @staticmethod
    def _destination_state(link: ResolvedHostLink) -> str:
        if link.destination.is_junction():
            return (
                "active"
                if HostIntegrationManager._junction_target(link.destination)
                == _path_key(link.source.resolve())
                else "wrong-junction"
            )
        if link.destination.exists():
            return "directory" if link.destination.is_dir() else "file"
        return "missing"

    @staticmethod
    def _junction_target(path: Path) -> str:
        return _path_key(path.resolve(strict=False))

    def _create_junction(self, path: Path, target: Path) -> None:
        if path.exists() or path.is_junction():
            raise ValidationError(f"Ya existe el destino del junction: {path}")
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            raise ValidationError(
                f"No se encuentra Windows PowerShell: {powershell}"
            )
        environment = dict(os.environ)
        environment["EAP_JUNCTION_PATH"] = str(path)
        environment["EAP_JUNCTION_TARGET"] = str(target)
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _CREATE_JUNCTION,
            ],
            env=environment,
            cwd=self.paths.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not path.is_junction():
            detail = "\n".join(
                item.strip()
                for item in (completed.stdout, completed.stderr)
                if item.strip()
            )
            raise TransactionError(
                "Windows no pudo crear el junction"
                + (f": {detail}" if detail else "")
            )

    def _require_processes_stopped(
        self, definition: HostIntegrationDefinition
    ) -> None:
        running = self._running_processes(definition.processes)
        if running:
            raise ValidationError(
                "Cierre antes los procesos: " + ", ".join(running)
            )

    @staticmethod
    def _running_processes(processes: tuple[str, ...]) -> tuple[str, ...]:
        if not processes:
            return ()
        tasklist = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "tasklist.exe"
        )
        if not tasklist.is_file():
            return ()
        running: list[str] = []
        for process in processes:
            completed = subprocess.run(
                [
                    str(tasklist),
                    "/FI",
                    f"IMAGENAME eq {process}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0 and f'"{process}"'.casefold() in (
                completed.stdout.casefold()
            ):
                running.append(process)
        return tuple(running)

    @staticmethod
    def _status(
        definition: HostIntegrationDefinition,
        data_profile: str,
        state: str,
        ok: bool,
        detail: str,
        links: tuple[ResolvedHostLink, ...],
    ) -> HostIntegrationStatus:
        return HostIntegrationStatus(
            id=definition.id,
            display_name=definition.display_name,
            description=definition.description,
            data_profile=data_profile,
            state=state,
            ok=ok,
            detail=detail,
            links=links,
        )


def _relative_path(value: Any, field: str, manifest: Path) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} no válido en {manifest}")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValidationError(f"{field} debe ser relativo en {manifest}")
    return path


def _require_lexically_within(candidate: Path, root: Path) -> None:
    candidate_key = os.path.normcase(os.path.abspath(candidate))
    root_key = os.path.normcase(os.path.abspath(root))
    try:
        Path(candidate_key).relative_to(Path(root_key))
    except ValueError as exc:
        raise ValidationError(
            f"La ruta del junction sale del home del profile: {candidate}"
        ) from exc


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_network_path(path: Path) -> bool:
    return str(path).startswith(("\\\\", "//"))


def _path_attributes(path: Path) -> int | None:
    if os.name != "nt":
        try:
            return 0x10 if path.is_dir() else (0 if path.exists() else None)
        except OSError:
            return None
    get_attributes = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attributes = int(get_attributes(str(path)))
    return None if attributes == 0xFFFFFFFF else attributes


def _path_exists(path: Path) -> bool:
    return _path_attributes(path) is not None


def _directory_exists(path: Path) -> bool:
    attributes = _path_attributes(path)
    return attributes is not None and bool(attributes & 0x10)


def _directory_usage(path: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_junction() or item.is_symlink():
            raise ValidationError(
                f"El directorio contiene otro enlace y no puede borrarse: {item}"
            )
        if item.is_file():
            files += 1
            size += item.stat().st_size
    return files, size


def _account_name() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    return f"{domain}\\{user}" if domain and user else user
