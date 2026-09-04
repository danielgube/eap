from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import replace
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
from .zip_extraction import validate_zip_archive, verify_extracted_zip

StatusCallback = Callable[[str], None]

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
    ) -> tuple[Path, ResolvedArtifact]:
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
    ) -> tuple[Path, ResolvedArtifact]:
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
            ready_artifact = self._ready_artifact(target, artifact)
            if ready_artifact is not None:
                self.status(f"Ya está instalado: {target}")
                transition("completed", reused=True)
                return target, ready_artifact
            if target.exists():
                raise IntegrityError(
                    f"Existe una instalación divergente en {target}; use repair"
                )

            self._check_disk_space(component, artifact)
            archive, artifact = self._obtain_archive(artifact, transition)
            transition("verified", archive=str(archive.relative_to(self.paths.root)))

            extract_root = staging_root / "extract"
            extract_root.mkdir(parents=True, exist_ok=False)
            self.status("Extrayendo el ZIP en staging...")
            self._safe_extract_zip(
                archive,
                extract_root,
                maximum_bytes=component.value["install"].get(
                    "maxExtractBytes"
                ),
            )
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
                "checksumOrigin": artifact.checksum_origin,
                "installedAt": utc_now(),
                "status": "ready",
                "source": {
                    "url": artifact.url,
                    "fileName": artifact.file_name,
                    "metadataUrl": artifact.metadata_url,
                    "size": artifact.size,
                    "checksumOrigin": artifact.checksum_origin,
                    "allowHttp": artifact.allow_http,
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
            return target, artifact
        except Exception as exc:
            try:
                transition("failed", error=f"{type(exc).__name__}: {exc}")
            except OSError:
                pass
            raise
        finally:
            # Do not walk a failed extraction from the EAP process. Endpoint
            # protection may still be handling one of those files and can
            # terminate the process that touches it. Failed staging is temp
            # data and can be removed later with the normal temp cleanup.
            if journal["state"] == "completed":
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

    def _check_disk_space(
        self,
        component: ComponentDefinition,
        artifact: ResolvedArtifact,
    ) -> None:
        available = shutil.disk_usage(self.paths.root).free
        archive_size = artifact.size or 350 * 1024 * 1024
        component_extract_limit = component.value["install"].get(
            "maxExtractBytes"
        )
        required = (
            archive_size + int(component_extract_limit)
            if component_extract_limit is not None
            else archive_size * 3
        )
        if available < required:
            raise TransactionError(
                f"Espacio insuficiente: se requieren aproximadamente "
                f"{required / (1024 * 1024):.0f} MiB y hay "
                f"{available / (1024 * 1024):.0f} MiB libres"
            )
        self.status(
            f"Espacio disponible: {available / (1024 * 1024 * 1024):.1f} GiB"
        )

    def _ready_artifact(
        self, target: Path, artifact: ResolvedArtifact
    ) -> ResolvedArtifact | None:
        algorithm = artifact.checksum_algorithm
        checksum = artifact.checksum
        if (
            algorithm is not None
            and checksum is not None
            and self._is_ready(target, algorithm, checksum)
        ):
            return artifact
        if artifact.checksum_origin != "unavailable":
            return None
        state_path = target / ".eap-install.json"
        if not target.is_dir() or not state_path.is_file():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        source = state.get("source")
        observed_sha256 = str(state.get("artifactSha256", "")).casefold()
        if (
            state.get("status") != "ready"
            or state.get("component") != artifact.family
            or state.get("provider") != artifact.provider
            or str(state.get("track")) != str(artifact.track)
            or str(state.get("version")) != artifact.version
            or state.get("checksumOrigin") != "downloaded"
            or not isinstance(source, dict)
            or source.get("url") != artifact.url
            or source.get("fileName") != artifact.file_name
            or re.fullmatch(r"[0-9a-f]{64}", observed_sha256) is None
        ):
            return None
        return replace(
            artifact,
            sha256=observed_sha256,
            checksum_origin="downloaded",
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
    ) -> tuple[Path, ResolvedArtifact]:
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
        checksum_algorithm = artifact.checksum_algorithm
        expected_checksum = artifact.checksum
        if archive.is_file():
            if checksum_algorithm is not None and expected_checksum is not None:
                if (
                    hash_file(archive, checksum_algorithm).lower()
                    == expected_checksum
                ):
                    self.status(
                        f"Reutilizando descarga verificada: {archive.name}"
                    )
                    return archive, artifact
            elif artifact.checksum_origin == "unavailable":
                observed_sha256 = hash_file(archive, "sha256").lower()
                self.status(
                    "Reutilizando descarga sin checksum publicado; "
                    f"SHA256 local: {observed_sha256}"
                )
                return archive, replace(
                    artifact,
                    sha256=observed_sha256,
                    checksum_origin="downloaded",
                )
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
            allow_http=artifact.allow_http,
        )
        transition("downloaded", downloadedBytes=downloaded)
        if checksum_algorithm is not None and expected_checksum is not None:
            checksum_label = checksum_algorithm.upper()
            self.status(f"Verificando {checksum_label}...")
            calculated = hash_file(partial, checksum_algorithm).lower()
            if calculated != expected_checksum:
                partial.unlink(missing_ok=True)
                raise IntegrityError(
                    f"{checksum_label} incorrecto: "
                    "el archivo descargado no es el esperado"
                )
        elif artifact.checksum_origin == "unavailable":
            calculated = hash_file(partial, "sha256").lower()
            artifact = replace(
                artifact,
                sha256=calculated,
                checksum_origin="downloaded",
            )
            self.status(
                "Sin checksum publicado; se registra sólo el SHA256 local: "
                f"{calculated}"
            )
        else:
            partial.unlink(missing_ok=True)
            raise ValidationError(
                "El artefacto no tiene checksum ni declara su ausencia"
            )
        os.replace(partial, archive)
        return archive, artifact

    def _safe_extract_zip(
        self,
        archive: Path,
        destination: Path,
        maximum_bytes: int | None = None,
    ) -> None:
        max_bytes = (
            maximum_bytes
            if maximum_bytes is not None
            else self.settings.get_int("install.maxExtractBytes", minimum=1)
        )
        max_ratio = self.settings.get_int("install.maxCompressionRatio", minimum=1)
        validate_zip_archive(archive, destination, max_bytes, max_ratio)

        seven_zip = self.paths.core / "tools" / "7zip" / "7z.exe"
        if not seven_zip.is_file():
            raise TransactionError(f"No se encuentra 7-Zip: {seven_zip}")
        command = [
            str(seven_zip),
            "x",
            "-bd",
            "-bb0",
            "-y",
            "-sccUTF-8",
            f"-o{destination}",
            str(archive),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise IntegrityError(
                f"No se pudo iniciar el proceso de extracción: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()
            if not detail:
                detail = f"código {completed.returncode}"
            raise IntegrityError(f"7-Zip no pudo descomprimir el archivo: {detail}")

        verify_extracted_zip(archive, destination)

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
        elif validation_type == "eclipse-package":
            self._validate_eclipse_payload(candidate)
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

    @staticmethod
    def _validate_eclipse_payload(candidate: Path) -> None:
        ini_path = candidate / "eclipse.ini"
        try:
            lines = [
                line.strip()
                for line in ini_path.read_text(encoding="utf-8-sig").splitlines()
            ]
            vm_index = lines.index("-vm")
            vm_value = lines[vm_index + 1]
        except (OSError, ValueError, IndexError) as exc:
            raise IntegrityError(
                "eclipse.ini no declara el JRE incluido mediante -vm"
            ) from exc
        relative = PurePosixPath(vm_value.replace("\\", "/"))
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise IntegrityError(f"Ruta -vm no válida en eclipse.ini: {vm_value}")
        vm_path = candidate.joinpath(*relative.parts).resolve()
        try:
            vm_path.relative_to(candidate.resolve())
        except ValueError as exc:
            raise IntegrityError("El JRE de Eclipse sale del payload") from exc
        javaw = vm_path / "javaw.exe" if vm_path.is_dir() else vm_path
        if not javaw.is_file():
            raise IntegrityError(
                f"No existe el JRE incluido declarado por Eclipse: {vm_value}"
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
