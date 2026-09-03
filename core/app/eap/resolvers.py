from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from .catalog import ComponentDefinition
from .errors import NetworkError, ValidationError
from .network import HttpClient
from .util import validate_version, version_belongs_to_track, version_key

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
    if resolver_type == "json-index":
        return _resolve_json_index(component, provider, track, client)
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
    if resolver_type == "dbeaver-download-page":
        return _resolve_dbeaver_download_page(
            component, provider, track, client
        )
    if resolver_type == "nodejs-index":
        return _resolve_nodejs(component, provider, track, client)
    if resolver_type == "golang-downloads-index":
        return _resolve_golang(component, provider, track, client)
    if resolver_type == "php-windows-releases":
        return _resolve_php_windows(component, provider, track, client)
    if resolver_type == "python-install-manager-index":
        return _resolve_pythoncore(component, provider, track, client)
    if resolver_type == "vscode-update-api":
        return _resolve_vscode_update_api(
            component, provider, track, client
        )
    if resolver_type == "jetbrains-product-release":
        return _resolve_jetbrains_product_release(
            component, provider, track, client
        )
    if resolver_type == "eclipse-epp-release":
        return _resolve_eclipse_epp_release(
            component, provider, track, client
        )
    raise ValidationError(f"Resolver no soportado: {resolver_type!r}")


def _resolve_json_index(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = _render_json_resolver_template(
        str(resolver["indexUrl"]), track=track
    )
    response = client.get_json(metadata_url)
    releases_declaration = resolver["releases"]
    artifacts_declaration = resolver["artifacts"]
    releases = _json_resolver_collection(
        response,
        str(releases_declaration["path"]),
        track,
    )
    candidates: list[tuple[str, dict[str, Any]]] = []
    for release in releases:
        if not isinstance(release, dict) or not _json_resolver_matches(
            release,
            releases_declaration.get("filters", {}),
            track,
        ):
            continue
        raw_version = _json_resolver_scalar(
            release,
            str(releases_declaration["versionPath"]),
            track,
            "versión",
        )
        version = str(raw_version)
        if len(version) > 256:
            continue
        pattern_text = releases_declaration.get("versionPattern")
        if pattern_text is not None:
            try:
                match = re.fullmatch(str(pattern_text), version)
            except re.error as exc:
                raise ValidationError(
                    "versionPattern no es una expresión regular válida"
                ) from exc
            if match is None:
                continue
            version = str(match.group("version"))
        if version_belongs_to_track(track, version):
            candidates.append((version, release))
    if not candidates:
        raise NetworkError(
            f"El índice JSON no publicó un artefacto para "
            f"{component.display_name} en la línea {track}"
        )
    version, release = max(
        candidates,
        key=lambda item: component.comparable_version_key(item[0]),
    )
    artifacts = [
        artifact
        for artifact in _json_resolver_collection(
            release,
            str(artifacts_declaration["path"]),
            track,
        )
        if isinstance(artifact, dict)
        and _json_resolver_matches(
            artifact,
            artifacts_declaration.get("filters", {}),
            track,
        )
    ]
    if not artifacts:
        raise NetworkError(
            f"El índice JSON no publicó el ZIP requerido para "
            f"{component.display_name} {version}"
        )
    selection = str(artifacts_declaration.get("selection", "only"))
    if selection not in {"only", "first", "last"}:
        raise ValidationError(
            f"Selección no soportada en el resolver JSON: {selection!r}"
        )
    if len(artifacts) > 1 and selection == "only":
        raise NetworkError(
            f"El índice JSON devolvió varios ZIP para "
            f"{component.display_name} {version}; el selector es ambiguo"
        )
    artifact = artifacts[-1] if selection == "last" else artifacts[0]

    file_name_path = artifacts_declaration.get("fileNamePath")
    file_name = (
        str(
            _json_resolver_scalar(
                artifact,
                str(file_name_path),
                track,
                "nombre de archivo",
            )
        )
        if file_name_path is not None
        else ""
    )
    url_path = artifacts_declaration.get("urlPath")
    if url_path is not None:
        url = str(
            _json_resolver_scalar(
                artifact, str(url_path), track, "URL"
            )
        )
    else:
        url = _render_json_resolver_template(
            str(artifacts_declaration["urlTemplate"]),
            track=track,
            version=version,
            file_name=file_name,
        )
    if not file_name:
        file_name = PurePosixPath(urllib.parse.urlparse(url).path).name

    sha256_path = artifacts_declaration.get("sha256Path")
    sha512_path = artifacts_declaration.get("sha512Path")
    checksum_algorithm = "sha256" if sha256_path is not None else "sha512"
    checksum_path = sha256_path if sha256_path is not None else sha512_path
    checksum = str(
        _json_resolver_scalar(
            artifact,
            str(checksum_path),
            track,
            checksum_algorithm.upper(),
        )
    ).lower()
    size_path = artifacts_declaration.get("sizePath")
    size: int | None = None
    if size_path is not None:
        raw_size = _json_resolver_scalar(
            artifact, str(size_path), track, "tamaño"
        )
        if (
            not isinstance(raw_size, int)
            or isinstance(raw_size, bool)
            or raw_size <= 0
        ):
            raise NetworkError(
                f"El índice JSON no proporcionó un tamaño válido para "
                f"{file_name}"
            )
        size = raw_size
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


def _json_resolver_collection(
    value: Any, path: str, track: int | str
) -> list[Any]:
    matches = _json_resolver_values(value, path, track)
    if len(matches) == 1 and isinstance(matches[0], list):
        return list(matches[0])
    return matches


def _json_resolver_scalar(
    value: Any,
    path: str,
    track: int | str,
    label: str,
) -> Any:
    matches = _json_resolver_values(value, path, track)
    if len(matches) != 1 or isinstance(matches[0], (dict, list)):
        raise NetworkError(
            f"El índice JSON no proporcionó un valor único para {label}"
        )
    return matches[0]


def _json_resolver_values(
    value: Any, path: str, track: int | str
) -> list[Any]:
    rendered_path = _render_json_resolver_template(path, track=track)
    if rendered_path in {"", "/"}:
        return [value]
    if not rendered_path.startswith("/"):
        raise ValidationError(
            f"Ruta JSON no válida; debe comenzar por '/': {path!r}"
        )
    current = [value]
    for encoded_segment in rendered_path[1:].split("/"):
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        following: list[Any] = []
        for candidate in current:
            if isinstance(candidate, dict):
                if "*" in segment:
                    expression = re.compile(
                        "^" + re.escape(segment).replace(r"\*", ".*") + "$"
                    )
                    following.extend(
                        candidate[key]
                        for key in sorted(candidate)
                        if expression.fullmatch(str(key))
                    )
                elif segment in candidate:
                    following.append(candidate[segment])
            elif isinstance(candidate, list):
                if segment == "*":
                    following.extend(candidate)
                elif segment.isdigit() and int(segment) < len(candidate):
                    following.append(candidate[int(segment)])
        current = following
        if not current:
            break
    return current


def _json_resolver_matches(
    value: dict[str, Any], filters: Any, track: int | str
) -> bool:
    if not isinstance(filters, dict):
        raise ValidationError("filters del resolver JSON debe ser un objeto")
    for path, expected in filters.items():
        rendered_expected = (
            _render_json_resolver_template(expected, track=track)
            if isinstance(expected, str)
            else expected
        )
        if rendered_expected not in _json_resolver_values(
            value, str(path), track
        ):
            return False
    return True


def _render_json_resolver_template(
    template: str,
    *,
    track: int | str,
    version: str | None = None,
    file_name: str | None = None,
) -> str:
    rendered = template.replace("{track}", str(track))
    if version is not None:
        rendered = rendered.replace("{version}", version)
    if file_name is not None:
        rendered = rendered.replace("{fileName}", file_name)
    if "{" in rendered or "}" in rendered:
        raise ValidationError(
            f"Token no soportado en el resolver JSON: {template!r}"
        )
    return rendered


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
            if version_belongs_to_track(track, version):
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


def _resolve_dbeaver_download_page(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["downloadPageUrl"])
    page = client.get_text(metadata_url)
    versions = re.findall(
        r'"title"\s*:\s*"DBeaver Community"\s*,\s*'
        r'"version"\s*:\s*"(\d+\.\d+\.\d+)"',
        page,
        flags=re.IGNORECASE,
    )
    if not versions:
        versions = re.findall(
            r"Download\s+DBeaver\s+Community\s*"
            r"(?:<[^>]+>\s*)*(\d+\.\d+\.\d+)",
            page,
            flags=re.IGNORECASE,
        )
    candidates = [
        version
        for version in versions
        if version_belongs_to_track(track, version)
    ]
    if not candidates:
        raise NetworkError(
            f"DBeaver no publicó una versión estable para la línea {track}"
        )
    version = max(candidates, key=version_key)
    template = str(
        resolver.get(
            "assetTemplate",
            "dbeaver-ce-{version}-windows-x86_64.zip",
        )
    )
    try:
        file_name = template.format(version=version)
    except (KeyError, ValueError) as exc:
        raise ValidationError(
            "assetTemplate inválido para DBeaver"
        ) from exc
    if PurePosixPath(file_name).name != file_name:
        raise ValidationError(
            "assetTemplate de DBeaver debe generar un nombre de archivo"
        )
    files_base_url = str(resolver["filesBaseUrl"]).rstrip("/")
    url = f"{files_base_url}/{version}/{file_name}"
    checksum_url = (
        f"{files_base_url}/{version}/checksum/{file_name}.sha256"
    )
    checksum_response = client.get_text(checksum_url, maximum_bytes=4096)
    checksum_match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_response)
    if not checksum_match:
        raise NetworkError(
            f"DBeaver no proporcionó el SHA-256 de {file_name}"
        )
    checksum = checksum_match.group(1).lower()
    _validate_artifact(
        track,
        version,
        url,
        file_name,
        checksum,
        "sha256",
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
        sha256=checksum,
        size=None,
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


def _resolve_golang(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    response = client.get_json(metadata_url)
    if not isinstance(response, list):
        raise NetworkError("Go no devolvió un índice de versiones válido")
    candidates: list[tuple[str, dict[str, Any]]] = []
    for release in response:
        if not isinstance(release, dict) or release.get("stable") is not True:
            continue
        version = str(release.get("version", "")).removeprefix("go")
        if version_belongs_to_track(track, version):
            candidates.append((version, release))
    if not candidates:
        raise NetworkError(
            f"Go no publicó un ZIP estable para la línea {track}"
        )
    version, release = max(candidates, key=lambda item: version_key(item[0]))
    files = release.get("files")
    if not isinstance(files, list):
        raise NetworkError(
            f"El índice oficial está incompleto para Go {version}"
        )
    archive = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and item.get("os") == "windows"
            and item.get("arch") == "amd64"
            and item.get("kind") == "archive"
            and str(item.get("filename", "")).casefold().endswith(".zip")
        ),
        None,
    )
    if archive is None:
        raise NetworkError(
            f"Go no publicó un ZIP de Windows x64 para la línea {track}"
        )
    try:
        file_name = str(archive["filename"])
        checksum = str(archive["sha256"]).lower()
    except (KeyError, TypeError) as exc:
        raise NetworkError(
            f"El índice oficial está incompleto para Go {version}"
        ) from exc
    download_base_url = str(resolver["downloadBaseUrl"]).rstrip("/")
    url = f"{download_base_url}/{file_name}"
    _validate_artifact(
        track, version, url, file_name, checksum, "sha256", client
    )
    size = archive.get("size")
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


def _resolve_php_windows(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    metadata_url = str(resolver["indexUrl"])
    response = client.get_json(metadata_url)
    release = response.get(str(track)) if isinstance(response, dict) else None
    if not isinstance(release, dict):
        raise NetworkError(
            f"PHP no publicó metadatos de Windows para la línea {track}"
        )
    version = str(release.get("version", ""))
    thread_safety = str(resolver.get("threadSafety", "nts")).casefold()
    architecture = str(resolver.get("architecture", "x64")).casefold()
    build_pattern = re.compile(
        rf"^{re.escape(thread_safety)}-(?:vs|vc)\d+-"
        rf"{re.escape(architecture)}$",
        flags=re.IGNORECASE,
    )
    build_keys = sorted(
        key
        for key, value in release.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and build_pattern.fullmatch(key)
    )
    if not build_keys:
        raise NetworkError(
            "PHP no publicó un ZIP "
            f"{thread_safety.upper()} de Windows {architecture} "
            f"para la línea {track}"
        )
    package = release[build_keys[-1]].get("zip")
    if not isinstance(package, dict):
        raise NetworkError(
            f"El índice oficial está incompleto para PHP {version}"
        )
    try:
        file_name = str(package["path"])
        checksum = str(package["sha256"]).lower()
    except (KeyError, TypeError) as exc:
        raise NetworkError(
            f"El índice oficial está incompleto para PHP {version}"
        ) from exc
    download_base_url = str(resolver["downloadBaseUrl"]).rstrip("/")
    url = f"{download_base_url}/{file_name}"
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


def _resolve_jetbrains_product_release(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    product_code = str(resolver["productCode"])
    download_key = str(resolver.get("downloadKey", "windowsZip"))
    api_url = str(resolver["apiUrl"])
    separator = "&" if urllib.parse.urlparse(api_url).query else "?"
    query = urllib.parse.urlencode(
        {
            "code": product_code,
            "type": "release",
            "latest": "true",
            "majorVersion": str(track),
        }
    )
    metadata_url = f"{api_url}{separator}{query}"
    response = client.get_json(metadata_url)
    releases = response.get(product_code) if isinstance(response, dict) else None
    if not isinstance(releases, list):
        raise NetworkError(
            f"JetBrains no devolvió releases válidas para {component.display_name}"
        )
    candidates: list[tuple[str, dict[str, Any]]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("type") != "release":
            continue
        version = str(release.get("version", ""))
        if version_belongs_to_track(track, version):
            candidates.append((version, release))
    if not candidates:
        raise NetworkError(
            f"JetBrains no publicó {component.display_name} para la línea {track}"
        )
    version, release = max(candidates, key=lambda item: version_key(item[0]))
    try:
        downloads = release["downloads"]
        if not isinstance(downloads, dict):
            raise TypeError("downloads no es un objeto")
        package = downloads[download_key]
        if not isinstance(package, dict):
            raise TypeError("el artefacto no es un objeto")
        url = str(package["link"])
        checksum_url = str(package["checksumLink"])
        size = int(package["size"]) if package.get("size") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise NetworkError(
            f"La API de JetBrains está incompleta para {component.display_name} "
            f"{version}"
        ) from exc
    client.require_https(checksum_url)
    checksum_response = client.get_text(checksum_url, maximum_bytes=4096)
    checksum_match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_response)
    if not checksum_match:
        raise NetworkError(
            f"JetBrains no proporcionó un SHA-256 válido para {version}"
        )
    checksum = checksum_match.group(1).lower()
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
        size=size,
        metadata_url=metadata_url,
    )


def _resolve_eclipse_epp_release(
    component: ComponentDefinition,
    provider: dict[str, Any],
    track: int | str,
    client: HttpClient,
) -> ResolvedArtifact:
    resolver = provider["resolver"]
    package_name = str(resolver["packageName"])
    release_build = str(resolver.get("releaseBuild", "R"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", package_name):
        raise ValidationError(
            f"packageName inválido para {component.id}: {package_name!r}"
        )
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", release_build):
        raise ValidationError(
            f"releaseBuild inválido para {component.id}: {release_build!r}"
        )
    version = str(track)
    base_url = str(resolver["downloadBaseUrl"]).rstrip("/")
    metadata_url = f"{base_url}/{version}/{release_build}/"
    file_name = (
        f"eclipse-{package_name}-{version}-{release_build}-"
        "win32-x86_64.zip"
    )
    url = f"{metadata_url}{file_name}"
    checksum_url = f"{url}.sha512"
    checksum_response = client.get_text(checksum_url, maximum_bytes=4096)
    checksum_match = re.search(
        rf"\b([0-9a-fA-F]{{128}})\b\s+\*?{re.escape(file_name)}\s*$",
        checksum_response,
        flags=re.MULTILINE,
    )
    if not checksum_match:
        raise NetworkError(
            f"Eclipse Foundation no proporcionó el SHA-512 de {file_name}"
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
    if not version_belongs_to_track(track, version):
        raise NetworkError(
            f"La versión resuelta {version!r} no pertenece a la línea {track}"
        )
    client.require_https(url)
    if (
        not file_name
        or PurePosixPath(file_name).name != file_name
        or "\\" in file_name
    ):
        raise NetworkError(
            f"El artefacto tiene un nombre de archivo no seguro: {file_name!r}"
        )
    if not file_name.lower().endswith(".zip"):
        raise NetworkError(f"El artefacto no es un ZIP: {file_name}")
    checksum_patterns = {"sha256": _SHA256, "sha512": _SHA512}
    pattern = checksum_patterns.get(checksum_algorithm)
    if pattern is None or not pattern.fullmatch(checksum):
        raise NetworkError(
            f"La fuente no proporcionó un {checksum_algorithm.upper()} válido"
        )
