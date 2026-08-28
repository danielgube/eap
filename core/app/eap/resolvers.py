from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from .catalog import ComponentDefinition
from .errors import NetworkError, ValidationError
from .network import HttpClient
from .util import validate_version, version_key

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA512 = re.compile(r"^[0-9a-fA-F]{128}$")


@dataclass(frozen=True)
class ResolvedArtifact:
    family: str
    component_id: str
    provider: str
    provider_name: str
    track: int | str
    version: str
    url: str
    file_name: str
    sha256: str | None
    size: int | None
    metadata_url: str
    sha512: str | None = None

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["checksumAlgorithm"] = self.checksum_algorithm
        value["checksum"] = self.checksum
        return value

    @property
    def checksum_algorithm(self) -> str:
        if self.sha256 is not None:
            return "sha256"
        if self.sha512 is not None:
            return "sha512"
        raise ValidationError("El artefacto resuelto no tiene checksum")

    @property
    def checksum(self) -> str:
        if self.sha256 is not None:
            return self.sha256
        if self.sha512 is not None:
            return self.sha512
        raise ValidationError("El artefacto resuelto no tiene checksum")


def resolve_component(
    component: ComponentDefinition,
    provider_id: str,
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    track = component.validate_track(track)
    provider = component.provider(provider_id)
    resolver = provider["resolver"]
    resolver_type = resolver.get("type")
    if resolver_type == "adoptium-v3":
        return _resolve_adoptium(component, provider, track, client)
    if resolver_type == "corretto-index":
        return _resolve_corretto(component, provider, track, client)
    if resolver_type == "apache-directory":
        return _resolve_apache_maven(component, provider, track, client)
    if resolver_type == "github-release-asset":
        return _resolve_github_release_asset(
            component, provider, track, client
        )
    if resolver_type == "nodejs-index":
        return _resolve_nodejs(component, provider, track, client)
    if resolver_type == "python-install-manager-index":
        return _resolve_pythoncore(component, provider, track, client)
    if resolver_type == "vscode-update-api":
        return _resolve_vscode_update_api(
            component, provider, track, client
        )
    raise ValidationError(f"Resolver no soportado: {resolver_type!r}")


def _resolve_adoptium(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    base_url = str(resolver["baseUrl"]).rstrip("/")
    query = urllib.parse.urlencode(
        {
            "architecture": "x64",
            "heap_size": "normal",
            "image_type": "jdk",
            "jvm_impl": str(resolver.get("jvmImpl", "hotspot")),
            "os": "windows",
            "page": "0",
            "page_size": "1",
            "project": "jdk",
            "release_type": "ga",
            "sort_method": "DEFAULT",
            "sort_order": "DESC",
            "vendor": str(resolver.get("vendor", "eclipse")),
        }
    )
    metadata_url = f"{base_url}/assets/latest/{track}/hotspot?{query}"
    response = client.get_json(metadata_url)
    if not isinstance(response, list) or not response:
        raise NetworkError(
            f"Adoptium no devolvió un JDK para Java {track} en Windows x64"
        )
    release = response[0]
    try:
        binary = release["binary"]
        package = binary["package"]
        release_name = str(release["release_name"])
        version = release_name.removeprefix("jdk-")
        url = str(package["link"])
        file_name = str(package["name"])
        checksum = str(package["checksum"]).lower()
        size = int(package["size"]) if package.get("size") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise NetworkError("Respuesta de Adoptium incompleta") from exc
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum,
        size=size,
        metadata_url=metadata_url,
    )


def _resolve_corretto(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    response = client.get_json(metadata_url)
    try:
        package = response["windows"]["x64"]["jdk"][str(track)]["zip"]
        resource = str(package["resource"])
        checksum = str(package["checksum_sha256"]).lower()
    except (KeyError, TypeError) as exc:
        raise NetworkError(
            f"Corretto no devolvió un JDK para Java {track} en Windows x64"
        ) from exc
    match = re.search(r"/downloads/resources/([^/]+)/", resource)
    if not match:
        raise NetworkError(f"No se pudo obtener la versión de Corretto: {resource}")
    version = match.group(1)
    base_url = str(resolver["resourceBaseUrl"]).rstrip("/")
    url = f"{base_url}/{resource.lstrip('/')}"
    file_name = PurePosixPath(resource).name
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum,
        size=None,
        metadata_url=metadata_url,
    )


def _resolve_apache_maven(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    index = client.get_text(metadata_url)
    versions = {
        match
        for match in re.findall(
            r"""href=["'](\d+\.\d+\.\d+)/["']""",
            index,
            flags=re.IGNORECASE,
        )
        if int(match.split(".", 1)[0]) == int(track)
    }
    if not versions:
        raise NetworkError(
            f"Apache no publicó una versión estable de Maven {track}"
        )
    version = max(versions, key=version_key)
    file_name = f"apache-maven-{version}-bin.zip"
    base_url = str(resolver["downloadBaseUrl"]).rstrip("/")
    url = f"{base_url}/{version}/binaries/{file_name}"
    checksum_url = f"{url}.sha512"
    checksum_response = client.get_text(checksum_url, maximum_bytes=4096)
    checksum_match = re.search(
        r"\b([0-9a-fA-F]{128})\b", checksum_response
    )
    if not checksum_match:
        raise NetworkError(
            f"Apache no proporcionó un SHA-512 válido para Maven {version}"
        )
    checksum = checksum_match.group(1).lower()
    _validate_artifact(
        track, version, url, file_name, checksum, "sha512", client
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=None,
        sha512=checksum,
        size=None,
        metadata_url=metadata_url,
    )


def _resolve_github_release_asset(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["apiUrl"])
    response = client.get_json(metadata_url)
    if isinstance(response, dict):
        releases = [response]
    elif isinstance(response, list):
        releases = response
    else:
        raise NetworkError(
            f"GitHub no devolvió releases para {component.display_name}"
        )
    pattern_text = str(resolver["assetPattern"])
    try:
        pattern = re.compile(pattern_text, flags=re.IGNORECASE)
    except re.error as exc:
        raise ValidationError(
            f"assetPattern inválido para {component.id}"
        ) from exc
    candidates: list[tuple[str, dict[str, Any], re.Match[str]]] = []
    for release in releases:
        if (
            not isinstance(release, dict)
            or release.get("draft")
            or release.get("prerelease")
        ):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            match = pattern.fullmatch(str(asset.get("name", "")))
            if match is None:
                continue
            try:
                version = match.group("version")
            except (IndexError, KeyError) as exc:
                raise ValidationError(
                    f"assetPattern de {component.id} no captura 'version'"
                ) from exc
            if _version_belongs_to_track(track, version):
                candidates.append((version, asset, match))
    if not candidates:
        raise NetworkError(
            f"No existe un ZIP estable de {component.display_name} "
            f"para la línea {track}"
        )
    version, selected, _ = max(
        candidates, key=lambda candidate: version_key(candidate[0])
    )
    file_name = str(selected["name"])
    url = str(selected["browser_download_url"])
    digest = str(selected.get("digest", ""))
    if ":" not in digest:
        raise NetworkError(
            f"GitHub no publicó el checksum de {file_name}"
        )
    checksum_algorithm, checksum = digest.split(":", 1)
    checksum_algorithm = checksum_algorithm.lower()
    checksum = checksum.lower()
    try:
        size = int(selected["size"])
    except (KeyError, TypeError, ValueError):
        size = None
    _validate_artifact(
        track,
        version,
        url,
        file_name,
        checksum,
        checksum_algorithm,
        client,
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum if checksum_algorithm == "sha256" else None,
        sha512=checksum if checksum_algorithm == "sha512" else None,
        size=size,
        metadata_url=metadata_url,
    )


def _resolve_nodejs(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    response = client.get_json(metadata_url)
    if not isinstance(response, list):
        raise NetworkError("Node.js no devolvió un índice de versiones válido")
    candidates: list[tuple[str, dict[str, Any]]] = []
    for release in response:
        if not isinstance(release, dict):
            continue
        version = str(release.get("version", "")).removeprefix("v")
        files = release.get("files", [])
        numbers = re.findall(r"\d+", version)
        if (
            numbers
            and int(numbers[0]) == int(track)
            and isinstance(files, list)
            and "win-x64-zip" in files
        ):
            candidates.append((version, release))
    if not candidates:
        raise NetworkError(
            f"Node.js no publicó un ZIP de Windows x64 para la línea {track}"
        )
    version, release = max(candidates, key=lambda item: version_key(item[0]))
    file_name = f"node-v{version}-win-x64.zip"
    base_url = str(resolver["downloadBaseUrl"]).rstrip("/")
    release_root = f"{base_url}/v{version}"
    url = f"{release_root}/{file_name}"
    checksum_url = f"{release_root}/SHASUMS256.txt"
    checksum_text = client.get_text(checksum_url, maximum_bytes=1024 * 1024)
    checksum_match = re.search(
        rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(file_name)}\s*$",
        checksum_text,
        flags=re.MULTILINE,
    )
    if not checksum_match:
        raise NetworkError(
            f"Node.js no proporcionó el SHA-256 de {file_name}"
        )
    checksum = checksum_match.group(1).lower()
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    size = release.get("size")
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum,
        size=int(size) if isinstance(size, int) else None,
        metadata_url=metadata_url,
    )


def _resolve_pythoncore(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    response = client.get_json(metadata_url)
    if not isinstance(response, dict) or not isinstance(
        response.get("versions"), list
    ):
        raise NetworkError(
            "Python.org no devolvió un índice de runtimes válido"
        )
    company = str(resolver.get("company", "PythonCore"))
    architecture = str(resolver.get("architectureTag", "64"))
    expected_tag = f"{track}-{architecture}"
    candidates: list[tuple[str, dict[str, Any]]] = []
    for release in response["versions"]:
        if not isinstance(release, dict):
            continue
        version = str(release.get("sort-version", ""))
        if (
            release.get("company") == company
            and release.get("tag") == expected_tag
            and re.fullmatch(r"\d+\.\d+\.\d+", version)
        ):
            candidates.append((version, release))
    if not candidates:
        raise NetworkError(
            f"Python.org no publicó Python {track} para Windows x64"
        )
    version, release = max(candidates, key=lambda item: version_key(item[0]))
    try:
        url = str(release["url"])
        checksum = str(release["hash"]["sha256"]).lower()
    except (KeyError, TypeError) as exc:
        raise NetworkError(
            f"El índice oficial está incompleto para Python {version}"
        ) from exc
    file_name = PurePosixPath(urllib.parse.urlparse(url).path).name
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum,
        size=None,
        metadata_url=metadata_url,
    )


def _resolve_vscode_update_api(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["updateUrl"])
    response = client.get_json(metadata_url)
    if not isinstance(response, dict):
        raise NetworkError(
            "Microsoft no devolvió metadatos válidos de Visual Studio Code"
        )
    try:
        version = str(response.get("productVersion") or response["name"])
        url = str(response["url"])
        checksum = str(response["sha256hash"]).lower()
    except (KeyError, TypeError) as exc:
        raise NetworkError(
            "La API de actualización de Visual Studio Code está incompleta"
        ) from exc
    file_name = PurePosixPath(urllib.parse.urlparse(url).path).name
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    return ResolvedArtifact(
        family=component.id,
        component_id=str(provider["componentId"]),
        provider=str(provider["id"]),
        provider_name=str(provider["displayName"]),
        track=track,
        version=version,
        url=url,
        file_name=file_name,
        sha256=checksum,
        size=None,
        metadata_url=metadata_url,
    )


def _validate_artifact(
    track: int | str,
    version: str,
    url: str,
    file_name: str,
    checksum: str,
    checksum_algorithm: str,
    client: HttpClient,
) -> None:
    validate_version(version)
    if not _version_belongs_to_track(track, version):
        raise NetworkError(
            f"La versión resuelta {version!r} no pertenece a la línea {track}"
        )
    client.require_https(url)
    if not file_name.lower().endswith(".zip"):
        raise NetworkError(f"El artefacto no es un ZIP: {file_name}")
    checksum_patterns = {"sha256": _SHA256, "sha512": _SHA512}
    pattern = checksum_patterns.get(checksum_algorithm)
    if pattern is None or not pattern.fullmatch(checksum):
        raise NetworkError(
            f"La fuente no proporcionó un {checksum_algorithm.upper()} válido"
        )


def _version_belongs_to_track(track: int | str, version: str) -> bool:
    version_numbers = tuple(int(value) for value in re.findall(r"\d+", version))
    track_numbers = tuple(int(value) for value in re.findall(r"\d+", str(track)))
    return bool(
        version_numbers
        and track_numbers
        and version_numbers[: len(track_numbers)] == track_numbers
    )
