from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import IntegrityError, NetworkError, TransactionError, ValidationError
from .locks import FileLock
from .network import HttpClient
from .paths import EapPaths
from .util import atomic_write_json, atomic_write_text, load_json, sha256_file


REPOSITORY = "danielgube/eap"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
ASSET_TEMPLATE = "eap-{version}-windows-x64.zip"
_SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PYTHON_VERSION = re.compile(
    r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$'
)
_MAX_RELEASE_FILES = 10_000
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_RELEASE_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_REQUIRED_MANAGED_PATHS = {
    "README.md",
    "eap.cmd",
    "core/app",
    "core/bootstrap.ps1",
    "core/catalog",
    "core/commands",
    "core/core_tools.json",
    "core/release.json",
    "core/version.json",
}


def parse_semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        raise ValidationError(f"Versión de release no válida: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def format_semver(value: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in value)


def next_patch(value: str) -> str:
    major, minor, patch = parse_semver(value)
    return format_semver((major, minor, patch + 1))


@dataclass(frozen=True)
class GitHubAsset:
    id: int
    name: str
    browser_download_url: str
    digest: str | None
    size: int


@dataclass(frozen=True)
class GitHubRelease:
    id: int
    tag_name: str
    name: str
    html_url: str
    published_at: str | None
    assets: tuple[GitHubAsset, ...]

    @property
    def version(self) -> str:
        return format_semver(parse_semver(self.tag_name))

    def asset(self, name: str) -> GitHubAsset:
        matches = [asset for asset in self.assets if asset.name == name]
        if len(matches) != 1:
            raise IntegrityError(
                f"La release {self.tag_name} no contiene exactamente un "
                f"asset {name}"
            )
        return matches[0]


@dataclass(frozen=True)
class EapUpdateStatus:
    current_version: str
    latest_version: str | None
    update_available: bool
    release: GitHubRelease | None
    asset: GitHubAsset | None

    def as_json(self) -> dict[str, Any]:
        return {
            "currentVersion": self.current_version,
            "latestVersion": self.latest_version,
            "updateAvailable": self.update_available,
            "releaseUrl": self.release.html_url if self.release else None,
            "asset": self.asset.name if self.asset else None,
            "publishedAt": (
                self.release.published_at if self.release else None
            ),
        }


@dataclass(frozen=True)
class EapUpdateResult:
    previous_version: str
    version: str
    archive: Path
    sha256: str

    def as_json(self) -> dict[str, str]:
        return {
            "previousVersion": self.previous_version,
            "version": self.version,
            "archive": str(self.archive),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class EapReleaseResult:
    version: str
    tag: str
    archive: Path
    sha256: str
    release_url: str
    created: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "archive": str(self.archive),
            "sha256": self.sha256,
            "releaseUrl": self.release_url,
            "created": self.created,
        }


@dataclass(frozen=True)
class GitPreflight:
    head: str
    remote_head: str
    pending_release: str | None


class GitHubApiClient:
    def __init__(
        self,
        timeout_seconds: int,
        user_agent: str,
        credential: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.credential = credential
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def latest_release(self) -> GitHubRelease | None:
        value = self._request_json(
            "GET", f"{API_ROOT}/releases/latest", allow_not_found=True
        )
        return None if value is None else self._parse_release(value)

    def release_by_tag(self, tag: str) -> GitHubRelease | None:
        encoded = urllib.parse.quote(tag, safe="")
        value = self._request_json(
            "GET",
            f"{API_ROOT}/releases/tags/{encoded}",
            allow_not_found=True,
        )
        return None if value is None else self._parse_release(value)

    def repository(self) -> dict[str, Any]:
        value = self._request_json("GET", API_ROOT)
        if not isinstance(value, dict):
            raise NetworkError("GitHub devolvió un repositorio no válido")
        return value

    def create_release(
        self,
        tag: str,
        commit: str,
        body: str,
    ) -> GitHubRelease:
        value = self._request_json(
            "POST",
            f"{API_ROOT}/releases",
            {
                "tag_name": tag,
                "target_commitish": commit,
                "name": f"EAP {tag}",
                "body": body,
                "draft": False,
                "prerelease": False,
            },
        )
        return self._parse_release(value)

    def upload_asset(
        self,
        release_id: int,
        path: Path,
        content_type: str,
    ) -> GitHubAsset:
        query = urllib.parse.urlencode({"name": path.name})
        url = (
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/"
            f"{release_id}/assets?{query}"
        )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise TransactionError(
                f"No se pudo leer el asset que se va a subir: {path}"
            ) from exc
        value = self._request_json(
            "POST", url, payload, content_type=content_type
        )
        return self._parse_asset(value)

    def _request_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | bytes | None = None,
        *,
        content_type: str = "application/json",
        allow_not_found: bool = False,
    ) -> Any:
        if not url.startswith(
            ("https://api.github.com/", "https://uploads.github.com/")
        ):
            raise ValidationError(f"Endpoint GitHub no permitido: {url}")
        data: bytes | None
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
        else:
            data = body
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(
                request, timeout=self.timeout_seconds
            ) as response:
                payload = response.read(10 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            detail = ""
            try:
                value = json.loads(exc.read().decode("utf-8"))
                if isinstance(value, dict):
                    detail = str(value.get("message", "")).strip()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            suffix = f": {detail}" if detail else ""
            raise NetworkError(
                f"GitHub rechazó la operación ({exc.code}){suffix}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"No se pudo conectar con GitHub: {exc}") from exc
        if len(payload) > 10 * 1024 * 1024:
            raise NetworkError("La respuesta de GitHub supera el límite permitido")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError("GitHub devolvió JSON no válido") from exc

    @classmethod
    def _parse_release(cls, value: Any) -> GitHubRelease:
        if not isinstance(value, dict):
            raise NetworkError("GitHub devolvió una release no válida")
        assets = value.get("assets")
        if not isinstance(assets, list):
            raise NetworkError("GitHub devolvió assets no válidos")
        try:
            return GitHubRelease(
                id=int(value["id"]),
                tag_name=str(value["tag_name"]),
                name=str(value.get("name") or value["tag_name"]),
                html_url=str(value["html_url"]),
                published_at=(
                    str(value["published_at"])
                    if value.get("published_at")
                    else None
                ),
                assets=tuple(cls._parse_asset(item) for item in assets),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("GitHub devolvió una release incompleta") from exc

    @staticmethod
    def _parse_asset(value: Any) -> GitHubAsset:
        if not isinstance(value, dict):
            raise NetworkError("GitHub devolvió un asset no válido")
        try:
            digest = value.get("digest")
            return GitHubAsset(
                id=int(value["id"]),
                name=str(value["name"]),
                browser_download_url=str(value["browser_download_url"]),
                digest=str(digest).lower() if digest else None,
                size=int(value["size"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("GitHub devolvió un asset incompleto") from exc


class EapReleaseUpdater:
    def __init__(
        self,
        paths: EapPaths,
        http: HttpClient,
        api: GitHubApiClient,
        status: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        self.http = http
        self.api = api
        self.status = status or (lambda message: None)

    def check(self, current_version: str) -> EapUpdateStatus:
        current = format_semver(parse_semver(current_version))
        release = self.api.latest_release()
        if release is None:
            return EapUpdateStatus(current, None, False, None, None)
        latest = release.version
        if parse_semver(latest) <= parse_semver(current):
            return EapUpdateStatus(current, latest, False, release, None)
        asset_name = ASSET_TEMPLATE.format(version=latest)
        asset = release.asset(asset_name)
        self._expected_digest(asset)
        return EapUpdateStatus(
            current_version=current,
            latest_version=latest,
            update_available=parse_semver(latest) > parse_semver(current),
            release=release,
            asset=asset,
        )

    def install(self, update: EapUpdateStatus) -> EapUpdateResult:
        if (self.paths.root / ".git").exists():
            raise ValidationError(
                "Esta instalación es un checkout Git; actualícela con git pull"
            )
        if not update.update_available or update.asset is None:
            raise ValidationError("No hay una actualización de EAP pendiente")
        if update.latest_version is None:
            raise IntegrityError("La release no declara una versión")
        version = update.latest_version
        if parse_semver(version) <= parse_semver(update.current_version):
            raise ValidationError("La release no es posterior a la versión actual")
        if update.release is None:
            raise IntegrityError("Falta la metadata de la release")
        self._validate_asset_url(update.release, update.asset)
        expected = self._expected_digest(update.asset)
        download_root = self.paths.temp / "downloads" / "eap" / version
        archive = download_root / update.asset.name
        partial = archive.with_suffix(archive.suffix + ".partial")
        staging = self.paths.temp / "staging" / f"eap-update-{uuid.uuid4().hex}"
        extracted = staging / "payload"
        lock_path = self.paths.temp / "locks" / "eap-update.lock"
        with FileLock(lock_path):
            try:
                installed_version = _read_local_version(self.paths.root)
                if installed_version != update.current_version:
                    raise ValidationError(
                        "La versión instalada cambió desde la comprobación; "
                        "vuelva a consultar la actualización"
                    )
                if not archive.is_file() or sha256_file(archive) != expected:
                    archive.unlink(missing_ok=True)
                    partial.unlink(missing_ok=True)
                    self.status(f"Descargando EAP {version}...")
                    self.http.download(
                        update.asset.browser_download_url,
                        partial,
                        maximum_bytes=_MAX_ARCHIVE_BYTES,
                    )
                    if partial.stat().st_size != update.asset.size:
                        partial.unlink(missing_ok=True)
                        raise IntegrityError(
                            "El tamaño descargado no coincide con GitHub"
                        )
                    actual = sha256_file(partial)
                    if actual != expected:
                        partial.unlink(missing_ok=True)
                        raise IntegrityError(
                            "SHA-256 incorrecto para la actualización de EAP"
                        )
                    os.replace(partial, archive)
                staging.mkdir(parents=True, exist_ok=False)
                self.status("Validando la release de EAP...")
                self._extract_release(archive, extracted)
                managed = self._validate_release_payload(
                    extracted, version, update.asset.name
                )
                self.status("Instalando la actualización de EAP...")
                self._commit(extracted, managed, staging)
                return EapUpdateResult(
                    previous_version=update.current_version,
                    version=version,
                    archive=archive,
                    sha256=expected,
                )
            finally:
                partial.unlink(missing_ok=True)
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _expected_digest(asset: GitHubAsset) -> str:
        if asset.size <= 0 or asset.size > _MAX_ARCHIVE_BYTES:
            raise IntegrityError(
                f"El tamaño de {asset.name} no es válido para una release EAP"
            )
        digest = (asset.digest or "").lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise IntegrityError(
                f"GitHub no proporciona un SHA-256 válido para {asset.name}"
            )
        return digest.removeprefix("sha256:")

    @staticmethod
    def _validate_asset_url(
        release: GitHubRelease, asset: GitHubAsset
    ) -> None:
        parsed = urllib.parse.urlparse(asset.browser_download_url)
        tag = urllib.parse.quote(release.tag_name, safe="")
        name = urllib.parse.quote(asset.name, safe="")
        expected_path = f"/{REPOSITORY}/releases/download/{tag}/{name}"
        try:
            port = parsed.port
        except ValueError as exc:
            raise IntegrityError(
                f"URL de descarga no válida para {asset.name}"
            ) from exc
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").casefold() != "github.com"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != expected_path
            or parsed.query
            or parsed.fragment
        ):
            raise IntegrityError(
                f"URL de descarga no válida para {asset.name}"
            )

    @staticmethod
    def _extract_release(archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        destination_resolved = destination.resolve()
        seen: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(archive) as package:
                entries = package.infolist()
                if not entries or len(entries) > _MAX_RELEASE_FILES:
                    raise IntegrityError(
                        "La release está vacía o contiene demasiadas entradas"
                    )
                for entry in entries:
                    relative = _safe_archive_path(entry.filename)
                    normalized = relative.as_posix().casefold()
                    if normalized in seen:
                        raise IntegrityError(
                            f"Ruta duplicada en la release: {entry.filename}"
                        )
                    seen.add(normalized)
                    mode = (entry.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise IntegrityError(
                            "La release no puede contener enlaces simbólicos"
                        )
                    if entry.flag_bits & 0x1:
                        raise IntegrityError(
                            "La release no puede contener archivos cifrados"
                        )
                    total += entry.file_size
                    if total > _MAX_RELEASE_BYTES:
                        raise IntegrityError(
                            "La release supera el tamaño descomprimido permitido"
                        )
                    if entry.file_size > 0 and entry.compress_size == 0:
                        raise IntegrityError(
                            f"Ratio de compresión no válido: {entry.filename}"
                        )
                    if (
                        entry.file_size
                        > entry.compress_size * _MAX_COMPRESSION_RATIO
                    ):
                        raise IntegrityError(
                            f"Ratio de compresión sospechoso: {entry.filename}"
                        )
                    target = destination.joinpath(*relative.parts).resolve()
                    try:
                        target.relative_to(destination_resolved)
                    except ValueError as exc:
                        raise IntegrityError(
                            f"Ruta fuera de la release: {entry.filename}"
                        ) from exc
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
        except (OSError, zipfile.BadZipFile) as exc:
            raise IntegrityError(f"ZIP de release no válido: {exc}") from exc

    @classmethod
    def _validate_release_payload(
        cls,
        payload: Path,
        version: str,
        asset_name: str,
    ) -> tuple[PurePosixPath, ...]:
        if (payload / ".gitignore").exists() or (payload / ".git").exists():
            raise IntegrityError("La release contiene metadatos Git")
        manifest = load_json(payload / "core" / "release.json")
        if manifest.get("schemaVersion") != 1:
            raise IntegrityError("Schema de release EAP no soportado")
        if manifest.get("repository") != REPOSITORY:
            raise IntegrityError("La release procede de otro repositorio")
        if manifest.get("assetName") != ASSET_TEMPLATE:
            raise IntegrityError("El patrón de asset de la release no coincide")
        if ASSET_TEMPLATE.format(version=version) != asset_name:
            raise IntegrityError("El nombre del asset no coincide con su versión")
        version_manifest = load_json(payload / "core" / "version.json")
        if str(version_manifest.get("version")) != version:
            raise IntegrityError("core/version.json no coincide con la release")
        init_path = payload / "core" / "app" / "eap" / "__init__.py"
        try:
            init_text = init_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IntegrityError("La release no contiene la versión Python") from exc
        match = _PYTHON_VERSION.search(init_text)
        if match is None or match.group(1) != version:
            raise IntegrityError("La versión Python no coincide con la release")
        raw_managed = manifest.get("managedPaths")
        if not isinstance(raw_managed, list) or not raw_managed:
            raise IntegrityError("La release no declara managedPaths")
        managed: list[PurePosixPath] = []
        seen: set[str] = set()
        for raw in raw_managed:
            if not isinstance(raw, str):
                raise IntegrityError("managedPaths contiene una ruta no válida")
            relative = _safe_archive_path(raw)
            normalized = relative.as_posix().casefold()
            if normalized in seen:
                raise IntegrityError(f"managedPath duplicado: {raw}")
            cls._validate_managed_path(relative)
            seen.add(normalized)
            managed.append(relative)
        declared = {path.as_posix() for path in managed}
        missing_required = sorted(_REQUIRED_MANAGED_PATHS - declared)
        if missing_required:
            raise IntegrityError(
                "La release no administra rutas obligatorias: "
                + ", ".join(missing_required)
            )
        for index, left in enumerate(managed):
            for right in managed[index + 1 :]:
                if _contains_path(left, right) or _contains_path(right, left):
                    raise IntegrityError(
                        "managedPaths contiene rutas solapadas: "
                        f"{left.as_posix()} y {right.as_posix()}"
                    )
        for relative in managed:
            if not payload.joinpath(*relative.parts).exists():
                raise IntegrityError(
                    f"Falta una ruta administrada: {relative.as_posix()}"
                )
        for file in payload.rglob("*"):
            if not file.is_file():
                continue
            relative = PurePosixPath(file.relative_to(payload).as_posix())
            if not any(_contains_path(root, relative) for root in managed):
                raise IntegrityError(
                    f"Archivo no administrado en la release: {relative}"
                )
        return tuple(managed)

    @staticmethod
    def _validate_managed_path(path: PurePosixPath) -> None:
        text = path.as_posix()
        if text in {"README.md", "eap.cmd"}:
            return
        if path.parts[0] != "core" or len(path.parts) < 2:
            raise IntegrityError(f"Ruta administrada no permitida: {text}")
        if path.parts[1].casefold() == "tools":
            raise IntegrityError("core/tools no puede formar parte de una release")

    def _commit(
        self,
        payload: Path,
        managed: tuple[PurePosixPath, ...],
        transaction: Path,
    ) -> None:
        backup_root = transaction / "backup"
        committed: list[PurePosixPath] = []
        try:
            for relative in managed:
                source = payload.joinpath(*relative.parts)
                target = self.paths.root.joinpath(*relative.parts)
                self._require_update_target(target, relative)
                backup = backup_root.joinpath(*relative.parts)
                if target.exists() or target.is_symlink():
                    if target.is_symlink():
                        raise IntegrityError(
                            f"La ruta administrada es un enlace: {target}"
                        )
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    target.replace(backup)
                committed.append(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
        except Exception as exc:
            try:
                self._rollback(committed, backup_root)
            except OSError as rollback_exc:
                raise TransactionError(
                    "La actualización falló y no se pudo restaurar por completo"
                ) from rollback_exc
            if isinstance(exc, (IntegrityError, TransactionError)):
                raise
            raise TransactionError(
                f"No se pudo publicar la actualización: {exc}"
            ) from exc

    def _rollback(
        self,
        committed: list[PurePosixPath],
        backup_root: Path,
    ) -> None:
        for relative in reversed(committed):
            target = self.paths.root.joinpath(*relative.parts)
            backup = backup_root.joinpath(*relative.parts)
            self._remove_exact_target(target, relative)
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                backup.replace(target)

    def _require_update_target(
        self, target: Path, relative: PurePosixPath
    ) -> None:
        resolved = target.resolve()
        try:
            resolved.relative_to(self.paths.root)
        except ValueError as exc:
            raise IntegrityError(
                f"La actualización sale de EAP: {relative.as_posix()}"
            ) from exc

    def _remove_exact_target(
        self, target: Path, relative: PurePosixPath
    ) -> None:
        self._require_update_target(target, relative)
        if target.is_symlink() or not target.is_dir():
            target.unlink(missing_ok=True)
        elif target.exists():
            shutil.rmtree(target)


class GitRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.git = shutil.which("git")
        if self.git is None:
            raise ValidationError("eap release requiere Git para Windows")

    def preflight(self) -> GitPreflight:
        if not (self.root / ".git").exists():
            raise ValidationError(
                "eap release sólo puede ejecutarse desde el checkout Git"
            )
        top = Path(self.run("rev-parse", "--show-toplevel")).resolve()
        if top != self.root:
            raise ValidationError(f"El checkout Git esperado es {self.root}")
        remote = self.run("remote", "get-url", "origin")
        normalized = remote.removesuffix(".git").rstrip("/").casefold()
        allowed = {
            REPOSITORY_URL.casefold(),
            f"git@github.com:{REPOSITORY}".casefold(),
            f"ssh://git@github.com/{REPOSITORY}".casefold(),
        }
        if normalized not in allowed:
            raise ValidationError(f"origin no apunta a {REPOSITORY_URL}")
        branch = self.run("branch", "--show-current")
        if branch != "main":
            raise ValidationError("eap release debe ejecutarse desde main")
        if self.run("status", "--porcelain"):
            raise ValidationError(
                "El checkout contiene cambios; haga commit y push antes "
                "de publicar"
            )
        self.run("fetch", "--quiet", "origin", "main")
        head = self.run("rev-parse", "HEAD")
        remote_head = self.run("rev-parse", "origin/main")
        if head == remote_head:
            return GitPreflight(head, remote_head, None)
        ancestor_code, _, _ = self.run_optional(
            "merge-base", "--is-ancestor", "origin/main", "HEAD"
        )
        ahead = self.run("rev-list", "--count", "origin/main..HEAD")
        subject = self.run("show", "-s", "--format=%s", "HEAD")
        changed = {
            line
            for line in self.run(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
            ).splitlines()
            if line
        }
        match = re.fullmatch(r"release: (v\d+\.\d+\.\d+)", subject)
        expected_files = {
            "core/app/eap/__init__.py",
            "core/version.json",
        }
        if (
            ancestor_code != 0
            or ahead != "1"
            or match is None
            or changed != expected_files
        ):
            raise ValidationError(
                "main local no coincide con origin/main y no es un commit "
                "de release reanudable"
            )
        local_version = _read_local_version(self.root)
        if match.group(1) != f"v{local_version}":
            raise ValidationError(
                "El commit de release pendiente no coincide con la versión local"
            )
        return GitPreflight(head, remote_head, match.group(1))

    def github_credential(self) -> str:
        completed = subprocess.run(
            [self.git, "credential", "fill"],
            cwd=self.root,
            input=(
                "protocol=https\n"
                "host=github.com\n"
                f"path={REPOSITORY}.git\n\n"
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            raise ValidationError(
                "GitHub no pudo completar la autenticación en el navegador"
            )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"username", "password"}:
                values[key] = value
        credential = values.get("password")
        if not credential:
            raise ValidationError(
                "Git Credential Manager no devolvió una sesión de GitHub"
            )
        return credential

    def run(self, *arguments: str) -> str:
        returncode, stdout, stderr = self.run_optional(*arguments)
        if returncode != 0:
            detail = "\n".join(
                value.strip()
                for value in (stdout, stderr)
                if value.strip()
            )
            raise TransactionError(
                f"Git no pudo ejecutar {' '.join(arguments)}"
                + (f": {detail}" if detail else "")
            )
        return stdout.strip()

    def run_optional(self, *arguments: str) -> tuple[int, str, str]:
        completed = subprocess.run(
            [self.git, *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def run_tests(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-I",
                "-X",
                "utf8",
                "-m",
                "unittest",
                "discover",
                "-s",
                "core/tests",
                "-v",
            ],
            cwd=self.root,
            check=False,
        )
        if completed.returncode != 0:
            raise TransactionError(
                "La suite de pruebas ha fallado; la release se cancela"
            )

    def commit_release(self, tag: str) -> None:
        name_code, name, _ = self.run_optional("config", "--get", "user.name")
        email_code, email, _ = self.run_optional(
            "config", "--get", "user.email"
        )
        options: tuple[str, ...] = ()
        if (
            name_code != 0
            or email_code != 0
            or not name.strip()
            or not email.strip()
        ):
            identity = self.run("show", "-s", "--format=%an%x00%ae", "HEAD")
            author, separator, address = identity.partition("\0")
            if not separator or not author.strip() or not address.strip():
                raise ValidationError(
                    "Configure user.name y user.email de Git para publicar"
                )
            options = (
                "-c",
                f"user.name={author.strip()}",
                "-c",
                f"user.email={address.strip()}",
            )
        self.run(*options, "commit", "-m", f"release: {tag}")


class EapReleasePublisher:
    def __init__(
        self,
        paths: EapPaths,
        timeout_seconds: int,
        user_agent: str,
        status: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.status = status or (lambda message: None)

    def publish(self) -> EapReleaseResult:
        repository = GitRepository(self.paths.root)
        self.status("Comprobando el checkout y origin/main...")
        git_state = repository.preflight()
        head = git_state.head
        self.status("Autenticando con GitHub...")
        credential = repository.github_credential()
        api = GitHubApiClient(
            self.timeout_seconds, self.user_agent, credential=credential
        )
        metadata = api.repository()
        permissions = metadata.get("permissions")
        if (
            str(metadata.get("full_name", "")).casefold()
            != REPOSITORY.casefold()
            or not isinstance(permissions, dict)
            or not bool(permissions.get("push") or permissions.get("admin"))
        ):
            raise ValidationError(
                f"La cuenta autenticada no puede escribir en {REPOSITORY}"
            )
        latest = api.latest_release()
        local_version = _read_local_version(self.paths.root)
        target_version = self._target_version(local_version, latest)
        target_tag = f"v{target_version}"
        if (
            git_state.pending_release is not None
            and git_state.pending_release != target_tag
        ):
            raise ValidationError(
                f"El commit pendiente {git_state.pending_release} no coincide "
                f"con la release esperada {target_tag}"
            )
        version_changed = target_version != local_version
        original_version = (self.paths.core / "version.json").read_bytes()
        init_path = self.paths.core / "app" / "eap" / "__init__.py"
        original_init = init_path.read_bytes()
        committed = False
        if version_changed:
            self.status(f"Incrementando EAP a {target_version}...")
            _write_local_version(self.paths.root, target_version)
        try:
            self.status("Ejecutando la suite de pruebas...")
            repository.run_tests()
            if version_changed:
                repository.run(
                    "add", "core/version.json", "core/app/eap/__init__.py"
                )
                repository.run("diff", "--cached", "--check")
                repository.commit_release(target_tag)
                committed = True
                repository.run("push", "origin", "main")
                head = repository.run("rev-parse", "HEAD")
            elif git_state.pending_release is not None:
                self.status(f"Reanudando la release {target_tag}...")
                repository.run("push", "origin", "main")
        except Exception:
            if version_changed and not committed:
                (self.paths.core / "version.json").write_bytes(original_version)
                init_path.write_bytes(original_init)
                repository.run_optional(
                    "restore",
                    "--staged",
                    "--",
                    "core/version.json",
                    "core/app/eap/__init__.py",
                )
            raise
        self._ensure_tag(repository, target_tag, head)
        archive = self._build_archive(repository, target_tag, target_version)
        digest = sha256_file(archive)
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        atomic_write_text(checksum, f"{digest}  {archive.name}\n")
        release = api.release_by_tag(target_tag)
        created = release is None
        if release is None:
            self.status(f"Creando la release {target_tag}...")
            release = api.create_release(
                target_tag,
                head,
                f"EAP {target_version}\n\nSHA-256: `{digest}`",
            )
        self._upload_if_missing(
            api, release, archive, "application/zip", digest
        )
        self._upload_if_missing(
            api, release, checksum, "text/plain", sha256_file(checksum)
        )
        return EapReleaseResult(
            version=target_version,
            tag=target_tag,
            archive=archive,
            sha256=digest,
            release_url=release.html_url,
            created=created,
        )

    @staticmethod
    def _target_version(
        local_version: str, latest: GitHubRelease | None
    ) -> str:
        local = format_semver(parse_semver(local_version))
        if latest is None:
            return local
        latest_version = latest.version
        candidate = next_patch(latest_version)
        if local == latest_version:
            if EapReleasePublisher._release_is_complete(latest):
                return candidate
            return local
        if local == candidate:
            return local
        raise ValidationError(
            f"La versión local {local} no coincide con la última release "
            f"{latest_version} ni con su siguiente parche {candidate}"
        )

    @staticmethod
    def _release_is_complete(release: GitHubRelease) -> bool:
        version = release.version
        archive_name = ASSET_TEMPLATE.format(version=version)
        required = {archive_name, f"{archive_name}.sha256"}
        for name in required:
            matches = [asset for asset in release.assets if asset.name == name]
            if len(matches) != 1:
                return False
            if not re.fullmatch(
                r"sha256:[0-9a-f]{64}", (matches[0].digest or "").lower()
            ):
                return False
        return True

    @staticmethod
    def _ensure_tag(
        repository: GitRepository, tag: str, expected_commit: str
    ) -> None:
        remote = repository.run(
            "ls-remote", "--tags", "origin", f"refs/tags/{tag}"
        )
        if remote:
            remote_commit = remote.split()[0]
            if remote_commit != expected_commit:
                raise ValidationError(
                    f"El tag remoto {tag} apunta a otro commit"
                )
        local_code, local_output, _ = repository.run_optional(
            "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"
        )
        local = local_output.strip() if local_code == 0 else ""
        if local and local != expected_commit:
            raise ValidationError(f"El tag local {tag} apunta a otro commit")
        if not local:
            repository.run("tag", tag, expected_commit)
        if not remote:
            repository.run("push", "origin", f"refs/tags/{tag}")

    def _build_archive(
        self,
        repository: GitRepository,
        tag: str,
        version: str,
    ) -> Path:
        output_root = self.paths.exports / "releases"
        output_root.mkdir(parents=True, exist_ok=True)
        archive = output_root / ASSET_TEMPLATE.format(version=version)
        raw_archive = self.paths.temp / "staging" / (
            f".{archive.name}.{uuid.uuid4().hex}.raw.zip"
        )
        verification = self.paths.temp / "staging" / (
            f".{archive.name}.{uuid.uuid4().hex}.verify"
        )
        partial = archive.with_suffix(archive.suffix + ".partial")
        raw_archive.parent.mkdir(parents=True, exist_ok=True)
        partial.unlink(missing_ok=True)
        try:
            repository.run(
                "archive",
                "--format=zip",
                f"--output={raw_archive}",
                tag,
            )
            with zipfile.ZipFile(raw_archive) as source, zipfile.ZipFile(
                partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as target:
                for entry in source.infolist():
                    relative = _safe_archive_path(entry.filename)
                    if relative.as_posix() == ".gitignore":
                        continue
                    if entry.is_dir():
                        continue
                    target.writestr(entry, source.read(entry))
            with zipfile.ZipFile(partial) as package:
                names = {
                    _safe_archive_path(item.filename).as_posix()
                    for item in package.infolist()
                    if not item.is_dir()
                }
            expected = {
                line
                for line in repository.run(
                    "ls-tree", "-r", "--name-only", tag
                ).splitlines()
                if line and line != ".gitignore"
            }
            if names != expected:
                raise IntegrityError(
                    "El asset generado no coincide con los archivos del tag"
                )
            EapReleaseUpdater._extract_release(partial, verification)
            EapReleaseUpdater._validate_release_payload(
                verification, version, archive.name
            )
            os.replace(partial, archive)
            return archive
        finally:
            raw_archive.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            if verification.exists():
                shutil.rmtree(verification, ignore_errors=True)

    @staticmethod
    def _upload_if_missing(
        api: GitHubApiClient,
        release: GitHubRelease,
        path: Path,
        content_type: str,
        expected_sha256: str,
    ) -> None:
        matches = [asset for asset in release.assets if asset.name == path.name]
        if matches:
            if len(matches) != 1:
                raise IntegrityError(f"Asset duplicado en GitHub: {path.name}")
            digest = matches[0].digest or ""
            if digest != f"sha256:{expected_sha256}":
                raise IntegrityError(
                    f"El asset remoto {path.name} tiene otro SHA-256"
                )
            return
        uploaded = api.upload_asset(release.id, path, content_type)
        if uploaded.digest != f"sha256:{expected_sha256}":
            raise IntegrityError(
                f"GitHub no confirmó el SHA-256 de {path.name}"
            )


def _read_local_version(root: Path) -> str:
    manifest = load_json(root / "core" / "version.json")
    version = format_semver(parse_semver(str(manifest.get("version", ""))))
    init_path = root / "core" / "app" / "eap" / "__init__.py"
    try:
        content = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"No se pudo leer {init_path}") from exc
    match = _PYTHON_VERSION.search(content)
    if match is None or match.group(1) != version:
        raise ValidationError(
            "core/version.json y eap.__version__ no coinciden"
        )
    return version


def _write_local_version(root: Path, version: str) -> None:
    normalized = format_semver(parse_semver(version))
    version_path = root / "core" / "version.json"
    manifest = load_json(version_path)
    manifest["version"] = normalized
    atomic_write_json(version_path, manifest)
    init_path = root / "core" / "app" / "eap" / "__init__.py"
    try:
        content = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TransactionError(f"No se pudo leer {init_path}") from exc
    updated, replacements = _PYTHON_VERSION.subn(
        f'__version__ = "{normalized}"', content
    )
    if replacements != 1:
        raise TransactionError("No se pudo actualizar eap.__version__")
    atomic_write_text(init_path, updated)


def _safe_archive_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise IntegrityError(f"Ruta no válida en la release: {value}")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            stem in _WINDOWS_RESERVED
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or any(character in '<>:"|?*' for character in part)
        ):
            raise IntegrityError(
                f"Nombre no permitido en Windows: {value}"
            )
    return path


def _contains_path(root: PurePosixPath, candidate: PurePosixPath) -> bool:
    return candidate == root or root in candidate.parents
