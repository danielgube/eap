from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .errors import NetworkError, ValidationError

ProgressCallback = Callable[[int, int | None], None]


class HttpClient:
    def __init__(self, timeout_seconds: int, user_agent: str = "EAP/0.1"):
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        self.opener.addheaders = [
            ("User-Agent", user_agent),
            ("Accept", "application/json, application/octet-stream;q=0.9, */*;q=0.1"),
        ]

    @staticmethod
    def require_https(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() != "https":
            raise ValidationError(f"EAP solo admite HTTPS para fuentes remotas: {url}")
        if not parsed.hostname:
            raise ValidationError(f"URL remota inválida: {url}")

    def get_json(self, url: str, maximum_bytes: int = 5 * 1024 * 1024) -> Any:
        self.require_https(url)
        request = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > maximum_bytes:
                    raise NetworkError(
                        f"La respuesta JSON supera el límite permitido: {url}"
                    )
                payload = response.read(maximum_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"No se pudo consultar {url}: {exc}") from exc
        if len(payload) > maximum_bytes:
            raise NetworkError(f"La respuesta JSON supera el límite permitido: {url}")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(f"Respuesta JSON inválida de {url}") from exc

    def get_text(
        self,
        url: str,
        maximum_bytes: int = 5 * 1024 * 1024,
    ) -> str:
        self.require_https(url)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "text/plain, text/html;q=0.9, */*;q=0.1"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > maximum_bytes:
                    raise NetworkError(
                        f"La respuesta supera el límite permitido: {url}"
                    )
                payload = response.read(maximum_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"No se pudo consultar {url}: {exc}") from exc
        if len(payload) > maximum_bytes:
            raise NetworkError(f"La respuesta supera el límite permitido: {url}")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NetworkError(f"Respuesta de texto inválida de {url}") from exc

    def download(
        self,
        url: str,
        destination: Path,
        progress: ProgressCallback | None = None,
        maximum_bytes: int | None = None,
    ) -> tuple[str, int]:
        self.require_https(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/octet-stream"},
        )
        downloaded = 0
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                self.require_https(final_url)
                raw_total = response.headers.get("Content-Length")
                total = int(raw_total) if raw_total and raw_total.isdigit() else None
                if (
                    maximum_bytes is not None
                    and total is not None
                    and total > maximum_bytes
                ):
                    raise NetworkError(
                        f"La descarga supera el límite permitido: {url}"
                    )
                with destination.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        downloaded += len(chunk)
                        if (
                            maximum_bytes is not None
                            and downloaded > maximum_bytes
                        ):
                            raise NetworkError(
                                f"La descarga supera el límite permitido: {url}"
                            )
                        if progress:
                            progress(downloaded, total)
                    output.flush()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"No se pudo descargar {url}: {exc}") from exc
        return final_url, downloaded
