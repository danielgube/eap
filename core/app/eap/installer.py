from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .catalog import ComponentDefinition
from .config import Settings
from .errors import IntegrityError, TransactionError, ValidationError
from .locks import FileLock
from .network import HttpClient
from .paths import EapPaths
from .resolvers import ResolvedArtifact
from .util import (
    atomic_write_json,
    hash_file,
    utc_now,
    validate_id,
    validate_version,
)

StatusCallback = Callable[[str], None]

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ComponentInstaller:
    def __init__(
        self,
        paths: EapPaths,
        settings: Settings,
        client: HttpClient,
        status: StatusCallback | None = None,
    ):
        self.paths = paths
        self.settings = settings
        self.client = client
        self.status = status or (lambda message: None)

    def install(
        self,
        component: ComponentDefinition,
        artifact: ResolvedArtifact,
        process_environment: dict[str, str] | None = None,
    ) -> Path:
        validate_id(artifact.provider, "id de proveedor")
        validate_version(artifact.version)
        lock_path = (
            self.paths.temp
            / "locks"
            / f"install-{component.id}-{artifact.provider}-{artifact.track}.lock"
        )
        with FileLock(lock_path):
            return self._install_locked(
                component, artifact, process_environment
            )

    def _install_locked(
        self,
        component: ComponentDefinition,
        artifact: ResolvedArtifact,
        process_environment: dict[str, str] | None,
    ) -> Path:
        transaction_id = uuid.uuid4().hex
        journal_path = (
            self.paths.temp / "transactions" / f"{transaction_id}.json"
        )
        staging_root = self.paths.temp / "staging" / transaction_id
        target = self._target_path(component, artifact)
        journal: dict[str, Any] = {
            "schemaVersion": 1,
            "transactionId": transaction_id,
            "operation": "install",
            "component": component.id,
            "provider": artifact.provider,
            "track": artifact.track,
            "version": artifact.version,
            "target": target.relative_to(self.paths.root).as_posix(),
            "state": "planned",
            "startedAt": utc_now(),
        }

        def transition(state: str, **extra: Any) -> None:
            journal["state"] = state
            journal["updatedAt"] = utc_now()
            journal.update(extra)
            atomic_write_json(journal_path, journal)

        transition("planned")
        try:
            if self._is_ready(
                target,
                artifact.checksum_algorithm,
                artifact.checksum,
            ):
                self.status(f"Ya está instalado: {target}")
                transition("completed", reused=True)
                return target
            if target.exists():
                raise IntegrityError(
                    f"Existe una instalación divergente en {target}; use repair"
                )

            self._check_disk_space(artifact)
            archive = self._obtain_archive(artifact, transition)
            transition("verified", archive=str(archive.relative_to(self.paths.root)))

            extract_root = staging_root / "extract"
            extract_root.mkdir(parents=True, exist_ok=False)
            self.status("Extrayendo el ZIP en staging...")
            self._safe_extract_zip(archive, extract_root)
            transition("staged")

            candidate = self._select_candidate_root(component, extract_root)
            self._validate_payload(
                component,
                artifact,
                candidate,
                process_environment,
            )
            transition("validated")

            install_state = {
                "schemaVersion": 1,
                "component": component.id,
                "provider": artifact.provider,
                "track": artifact.track,
                "version": artifact.version,
                "checksumAlgorithm": artifact.checksum_algorithm,
                "artifactChecksum": artifact.checksum,
                "installedAt": utc_now(),
                "status": "ready",
                "source": {
                    "url": artifact.url,
                    "fileName": artifact.file_name,
                    "metadataUrl": artifact.metadata_url,
                    "size": artifact.size,
                },
            }
            if artifact.sha256 is not None:
                install_state["artifactSha256"] = artifact.sha256
            if artifact.sha512 is not None:
                install_state["artifactSha512"] = artifact.sha512
            atomic_write_json(
                candidate / ".eap-install.json",
                install_state,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, target)
            transition("committed")
            self.status(f"Instalado en {target}")
            if not self.settings.get_bool("download.keepArchives"):
                archive.unlink(missing_ok=True)
            transition("completed")
            return target
        except Exception as exc:
            try:
                transition("failed", error=f"{type(exc).__name__}: {exc}")
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def _target_path(
        self, component: ComponentDefinition, artifact: ResolvedArtifact
    ) -> Path:
        template = str(component.value["install"]["directoryTemplate"])
        relative = template.format(
            provider=artifact.provider,
            version=artifact.version,
        )
        target = self.paths.require_within_root(self.paths.components / relative)
        try:
            target.relative_to(self.paths.components)
        except ValueError as exc:
            raise ValidationError(f"Destino de componente inválido: {target}") from exc
        return target

    def _check_disk_space(self, artifact: ResolvedArtifact) -> None:
        available = shutil.disk_usage(self.paths.root).free
        archive_size = artifact.size or 350 * 1024 * 1024
        required = archive_size * 3
        if available < required:
            raise TransactionError(
                f"Espacio insuficiente: se requieren aproximadamente "
                f"{required / (1024 * 1024):.0f} MiB y hay "
                f"{available / (1024 * 1024):.0f} MiB libres"
            )
        self.status(
            f"Espacio disponible: {available / (1024 * 1024 * 1024):.1f} GiB"
        )

    @staticmethod
    def _is_ready(
        target: Path,
        checksum_algorithm: str,
        expected_checksum: str,
    ) -> bool:
        state_path = target / ".eap-install.json"
        if not target.is_dir() or not state_path.is_file():
            return False
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if state.get("status") != "ready":
            return False
        if (
            state.get("checksumAlgorithm") == checksum_algorithm
            and state.get("artifactChecksum") == expected_checksum
        ):
            return True
        return (
            checksum_algorithm == "sha256"
            and state.get("artifactSha256") == expected_checksum
        )

    def _obtain_archive(
        self,
        artifact: ResolvedArtifact,
        transition: Callable[..., None],
    ) -> Path:
        archive_root = (
            self.paths.temp
            / "downloads"
            / artifact.family
            / artifact.provider
            / artifact.version
        )
        archive = archive_root / artifact.file_name
        partial = archive.with_suffix(archive.suffix + ".partial")
        archive_root.mkdir(parents=True, exist_ok=True)
        if (
            archive.is_file()
            and hash_file(
                archive, artifact.checksum_algorithm
            ).lower()
            == artifact.checksum
        ):
            self.status(f"Reutilizando descarga verificada: {archive.name}")
            return archive
        archive.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
        transition("downloading")

        last_percent = -1

        def progress(downloaded: int, total: int | None) -> None:
            nonlocal last_percent
            if total:
                percent = int(downloaded * 100 / total)
                if percent // 5 != last_percent // 5:
                    self.status(
                        f"Descargando {artifact.file_name}: {percent}%"
                    )
                    last_percent = percent

        _, downloaded = self.client.download(
            artifact.url,
            partial,
            progress=progress,
        )
        transition("downloaded", downloadedBytes=downloaded)
        checksum_label = artifact.checksum_algorithm.upper()
        self.status(f"Verificando {checksum_label}...")
        calculated = hash_file(
            partial, artifact.checksum_algorithm
        ).lower()
        if calculated != artifact.checksum:
            partial.unlink(missing_ok=True)
            raise IntegrityError(
                f"{checksum_label} incorrecto: "
                "el archivo descargado no es el esperado"
            )
        os.replace(partial, archive)
        return archive

    def _safe_extract_zip(self, archive: Path, destination: Path) -> None:
        max_bytes = self.settings.get_int("install.maxExtractBytes", minimum=1)
        max_ratio = self.settings.get_int("install.maxCompressionRatio", minimum=1)
        total = 0
        with zipfile.ZipFile(archive, "r") as source:
            entries = source.infolist()
            for entry in entries:
                relative = self._validate_zip_entry(entry)
                total += entry.file_size
                if total > max_bytes:
                    raise IntegrityError(
                        "El tamaño extraído supera install.maxExtractBytes"
                    )
                if entry.file_size and entry.compress_size == 0:
                    raise IntegrityError(
                        f"Entrada ZIP con ratio inválido: {entry.filename}"
                    )
                if (
                    entry.compress_size
                    and entry.file_size / entry.compress_size > max_ratio
                ):
                    raise IntegrityError(
                        f"Entrada ZIP supera el ratio permitido: {entry.filename}"
                    )
                target = destination.joinpath(*relative.parts)
                self._ensure_destination(destination, target)

            for entry in entries:
                relative = self._validate_zip_entry(entry)
                target = destination.joinpath(*relative.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry, "r") as input_stream, target.open(
                    "wb"
                ) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)

    @staticmethod
    def _validate_zip_entry(entry: zipfile.ZipInfo) -> PurePosixPath:
        raw_name = entry.filename.replace("\\", "/")
        relative = PurePosixPath(raw_name)
        if relative.is_absolute() or not relative.parts:
            raise IntegrityError(f"Ruta ZIP absoluta o vacía: {entry.filename}")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise IntegrityError(f"Path traversal en ZIP: {entry.filename}")
        for part in relative.parts:
            stem = part.split(".", 1)[0].upper()
            if stem in _WINDOWS_RESERVED or part.endswith((" ", ".")):
                raise IntegrityError(
                    f"Nombre no permitido en Windows: {entry.filename}"
                )
            if ":" in part:
                raise IntegrityError(f"Ruta ZIP con unidad: {entry.filename}")
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise IntegrityError(f"Enlace simbólico no permitido: {entry.filename}")
        return relative

    @staticmethod
    def _ensure_destination(root: Path, target: Path) -> None:
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise IntegrityError(f"La extracción sale de staging: {target}") from exc

    @staticmethod
    def _select_candidate_root(
        component: ComponentDefinition, extract_root: Path
    ) -> Path:
        strip_single = bool(component.value["install"].get("stripSingleRoot", False))
        if not strip_single:
            return extract_root
        entries = [entry for entry in extract_root.iterdir()]
        if len(entries) != 1 or not entries[0].is_dir():
            raise IntegrityError(
                "El ZIP no contiene una única carpeta raíz como se esperaba"
            )
        return entries[0]

    def _validate_payload(
        self,
        component: ComponentDefinition,
        artifact: ResolvedArtifact,
        candidate: Path,
        process_environment: dict[str, str] | None,
    ) -> None:
        required = component.value["install"]["requiredFiles"]
        for relative in required:
            target = candidate / str(relative)
            if not target.is_file():
                raise IntegrityError(f"Falta el archivo requerido: {relative}")

        validation = component.value["install"].get("validation", {})
        validation_type = validation.get("type")
        if validation_type == "java-release":
            self._validate_java_payload(
                component, artifact, candidate, process_environment
            )
        elif validation_type == "command":
            self._run_smoke_test(
                component,
                candidate,
                validation,
                process_environment,
            )
        elif validation_type == "files-only":
            return
        else:
            raise ValidationError(
                f"Validación de instalación no soportada para {component.id}: "
                f"{validation_type!r}"
            )

    def _validate_java_payload(
        self,
        component: ComponentDefinition,
        artifact: ResolvedArtifact,
        candidate: Path,
        process_environment: dict[str, str] | None,
    ) -> None:
        release = self._read_release(candidate / "release")
        java_version = release.get("JAVA_VERSION", "")
        numbers = re.findall(r"\d+", java_version)
        if not numbers or str(int(numbers[0])) != str(artifact.track):
            raise IntegrityError(
                f"El ZIP contiene Java {java_version!r}, no Java {artifact.track}"
            )
        expected_implementor = str(
            component.provider(artifact.provider)["verification"].get(
                "implementorContains", ""
            )
        )
        implementor = release.get("IMPLEMENTOR", "")
        if expected_implementor and expected_implementor.lower() not in implementor.lower():
            raise IntegrityError(
                f"Implementor inesperado: {implementor!r}; "
                f"se esperaba {expected_implementor!r}"
            )

        self.status("Ejecutando smoke test de java -version...")
        try:
            completed = subprocess.run(
                [str(candidate / "bin" / "java.exe"), "-version"],
                cwd=candidate,
                env=dict(process_environment or os.environ),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IntegrityError(f"No se pudo ejecutar java -version: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise IntegrityError(
                f"java -version terminó con {completed.returncode}: {detail}"
            )

    def _run_smoke_test(
        self,
        component: ComponentDefinition,
        candidate: Path,
        validation: dict[str, Any],
        process_environment: dict[str, str] | None,
    ) -> None:
        command = validation.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            raise ValidationError(
                f"Comando de validación inválido para {component.id}"
            )
        relative = PurePosixPath(command[0].replace("\\", "/"))
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValidationError(
                f"Ejecutable de validación inválido para {component.id}"
            )
        executable = candidate.joinpath(*relative.parts).resolve()
        try:
            executable.relative_to(candidate.resolve())
        except ValueError as exc:
            raise ValidationError(
                f"El smoke test de {component.id} sale del payload"
            ) from exc
        if not executable.is_file():
            raise IntegrityError(
                f"No existe el ejecutable de validación: {command[0]}"
            )

        environment = dict(process_environment or os.environ)
        arguments = [str(executable), *command[1:]]
        if executable.suffix.lower() in {".cmd", ".bat"}:
            comspec = environment.get(
                "COMSPEC", str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
            )
            arguments = [
                comspec,
                "/D",
                "/S",
                "/C",
                subprocess.list2cmdline(arguments),
            ]
        self.status(f"Ejecutando smoke test de {component.display_name}...")
        try:
            completed = subprocess.run(
                arguments,
                cwd=candidate,
                env=environment,
                capture_output=True,
                text=True,
                timeout=int(validation.get("timeoutSeconds", 30)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IntegrityError(
                f"No se pudo validar {component.display_name}: {exc}"
            ) from exc
        output = "\n".join(
            value for value in (completed.stdout, completed.stderr) if value
        )
        expected = str(validation.get("expectContains", ""))
        if completed.returncode != 0 or (
            expected and expected.casefold() not in output.casefold()
        ):
            detail = output.strip()
            raise IntegrityError(
                f"Smoke test de {component.display_name} fallido "
                f"({completed.returncode}): {detail}"
            )

    @staticmethod
    def _read_release(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise IntegrityError(f"No se pudo leer {path}") from exc
        for line in lines:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
        return values
