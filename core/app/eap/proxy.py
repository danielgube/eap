from __future__ import annotations

import getpass
import hashlib
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Mapping, MutableMapping

from .errors import NetworkError, ValidationError

_PROXY_PROPERTIES = {
    "http_proxy": ("http_proxy", "proxy.http"),
    "https_proxy": ("https_proxy", "proxy.https"),
    "all_proxy": ("all_proxy", "proxy.all"),
    "no_proxy": ("no_proxy", "proxy.no_proxy", "proxy.noProxy"),
}
_PROXY_SCHEMES = {"http", "https"}
_MAXIMUM_PAGE_BYTES = 2 * 1024 * 1024


def _property(
    properties: Mapping[str, str], *names: str
) -> tuple[bool, str]:
    folded = {str(key).casefold(): str(value) for key, value in properties.items()}
    for name in names:
        key = name.casefold()
        if key in folded:
            return True, folded[key].strip()
    return False, ""


def _boolean(properties: Mapping[str, str], name: str, default: bool) -> bool:
    found, raw_value = _property(properties, name)
    if not found:
        return default
    value = raw_value.casefold()
    if value in {"true", "yes", "1", "on"}:
        return True
    if value in {"false", "no", "0", "off"}:
        return False
    raise ValidationError(f"Booleano inválido para {name}: {raw_value!r}")


def _integer(
    properties: Mapping[str, str], name: str, default: int, minimum: int = 1
) -> int:
    found, raw_value = _property(properties, name)
    if not found:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValidationError(
            f"Entero inválido para {name}: {raw_value!r}"
        ) from exc
    if value < minimum:
        raise ValidationError(f"{name} debe ser mayor o igual que {minimum}")
    return value


def _validate_remote_url(value: str, property_name: str) -> str:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ValidationError(f"URL no válida en {property_name}")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError(
            f"{property_name} debe ser una URL HTTP o HTTPS completa"
        )
    return value


def _validate_proxy_url(value: str, property_name: str) -> str:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ValidationError(f"Proxy no válido en {property_name}")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.casefold() not in _PROXY_SCHEMES or not parsed.hostname:
        raise ValidationError(
            f"{property_name} debe contener una URL de proxy completa"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise ValidationError(f"Puerto no válido en {property_name}") from exc
    return value


def configured_proxy_values(
    properties: Mapping[str, str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for canonical, aliases in _PROXY_PROPERTIES.items():
        found, value = _property(properties, *aliases)
        if not found:
            continue
        if value and canonical != "no_proxy":
            _validate_proxy_url(value, aliases[0])
        if any(character in value for character in ("\r", "\n", "\0")):
            raise ValidationError(f"Valor no válido en {aliases[0]}")
        values[canonical] = value
    return values


def apply_proxy_environment(
    environment: MutableMapping[str, str], properties: Mapping[str, str]
) -> dict[str, str]:
    """Apply explicitly configured standard proxy variables.

    A declared empty value removes an inherited proxy. Lowercase and uppercase
    spellings are both emitted because command-line tools disagree on which one
    they consume.
    """

    configured = configured_proxy_values(properties)
    for canonical, value in configured.items():
        for existing in tuple(environment):
            if existing.casefold() == canonical.casefold():
                environment.pop(existing, None)
        if value:
            environment[canonical] = value
            environment[canonical.upper()] = value
    return configured


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        host = f"***:***@{host}"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            host,
            parsed.path,
            "***" if parsed.query else "",
            "",
        )
    )


@dataclass(frozen=True)
class ProxyConfiguration:
    proxies: dict[str, str]
    authentication_enabled: bool
    authentication_type: str
    authentication_url: str | None
    check_url: str | None
    username_field: str
    password_field: str
    extra_fields: dict[str, str]
    verify_tls: bool
    require_https: bool
    timeout_seconds: int

    @classmethod
    def from_properties(
        cls, properties: Mapping[str, str]
    ) -> "ProxyConfiguration":
        proxies = configured_proxy_values(properties)
        enabled = _boolean(
            properties, "enable_proxy_authentication", default=False
        )
        _, authentication_type = _property(
            properties, "proxy_authentication_type"
        )
        authentication_type = (authentication_type or "post").casefold()
        if authentication_type not in {"post", "browser"}:
            raise ValidationError(
                "proxy_authentication_type debe ser 'post' o 'browser'"
            )
        _, authentication_url = _property(
            properties, "proxy_authentication_url"
        )
        _, check_url = _property(properties, "proxy_authentication_check_url")
        if authentication_url:
            authentication_url = _validate_remote_url(
                authentication_url, "proxy_authentication_url"
            )
        if check_url:
            check_url = _validate_remote_url(
                check_url, "proxy_authentication_check_url"
            )
        if enabled and not any(
            value for key, value in proxies.items() if key != "no_proxy"
        ):
            raise ValidationError(
                "enable_proxy_authentication requiere configurar http_proxy, "
                "https_proxy o all_proxy"
            )
        if enabled and not authentication_url:
            raise ValidationError(
                "enable_proxy_authentication requiere proxy_authentication_url"
            )
        _, username_field = _property(
            properties, "proxy_authentication_username_field"
        )
        _, password_field = _property(
            properties, "proxy_authentication_password_field"
        )
        username_field = username_field or "username"
        password_field = password_field or "password"
        if not username_field or any(
            character.isspace() for character in username_field
        ):
            raise ValidationError(
                "proxy_authentication_username_field no es válido"
            )
        if not password_field or any(
            character.isspace() for character in password_field
        ):
            raise ValidationError(
                "proxy_authentication_password_field no es válido"
            )
        extra_fields: dict[str, str] = {}
        extra_prefix = "proxy_authentication_form_field."
        for key, value in properties.items():
            key_text = str(key)
            if not key_text.casefold().startswith(extra_prefix):
                continue
            field_name = key_text[len(extra_prefix) :].strip()
            if not field_name or any(
                character.isspace() for character in field_name
            ):
                raise ValidationError(
                    f"Campo adicional proxy no válido: {key_text!r}"
                )
            extra_fields[field_name] = str(value)
        timeout = _integer(
            properties,
            "proxy_authentication_timeout_seconds",
            default=_integer(
                properties, "network.timeoutSeconds", default=20
            ),
        )
        return cls(
            proxies=proxies,
            authentication_enabled=enabled,
            authentication_type=authentication_type,
            authentication_url=authentication_url or None,
            check_url=check_url or None,
            username_field=username_field,
            password_field=password_field,
            extra_fields=extra_fields,
            verify_tls=_boolean(
                properties, "proxy_authentication_verify_tls", default=True
            ),
            require_https=_boolean(
                properties, "proxy_authentication_require_https", default=True
            ),
            timeout_seconds=timeout,
        )

    @property
    def configured(self) -> bool:
        return any(
            value for key, value in self.proxies.items() if key != "no_proxy"
        )

    @property
    def session_token(self) -> str:
        identity = repr(
            (
                sorted(self.proxies.items()),
                self.authentication_type,
                self.authentication_url,
                self.check_url,
                sorted(self.extra_fields.items()),
            )
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()[:24]

    def handler_proxies(self) -> dict[str, str]:
        result: dict[str, str] = {}
        all_proxy = self.proxies.get("all_proxy", "")
        for protocol in ("http", "https"):
            value = self.proxies.get(f"{protocol}_proxy") or all_proxy
            if value:
                result[protocol] = value
        return result

    def status(self, authenticated: bool = False) -> dict[str, object]:
        return {
            "configured": self.configured,
            "proxies": {
                name: _redact_url(value)
                for name, value in self.proxies.items()
                if value
            },
            "authenticationEnabled": self.authentication_enabled,
            "authenticationType": (
                self.authentication_type if self.authentication_enabled else None
            ),
            "authenticationUrl": (
                _redact_url(self.authentication_url)
                if self.authentication_url
                else None
            ),
            "checkUrl": _redact_url(self.check_url) if self.check_url else None,
            "verifyTls": self.verify_tls,
            "requireHttpsPost": self.require_https,
            "authenticatedInProcess": authenticated,
        }


@dataclass(frozen=True)
class ProxyAuthenticationResult:
    state: str
    detail: str
    authenticated: bool

    def as_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "detail": self.detail,
            "authenticated": self.authenticated,
        }


@dataclass(frozen=True)
class _ProxyResponse:
    status: int
    url: str
    body: str


@dataclass(frozen=True)
class _FormInput:
    name: str
    value: str
    input_type: str


@dataclass(frozen=True)
class _HtmlForm:
    action: str
    method: str
    inputs: tuple[_FormInput, ...]


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_HtmlForm] = []
        self._action: str | None = None
        self._method = "post"
        self._inputs: list[_FormInput] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {
            str(name).casefold(): "" if value is None else str(value)
            for name, value in attrs
        }
        if tag.casefold() == "form":
            if self._action is not None:
                self._finish_form()
            self._action = attributes.get("action", "")
            self._method = attributes.get("method", "post").casefold()
            self._inputs = []
            return
        if tag.casefold() != "input" or self._action is None:
            return
        name = attributes.get("name", "").strip()
        if not name:
            return
        self._inputs.append(
            _FormInput(
                name=name,
                value=attributes.get("value", ""),
                input_type=attributes.get("type", "text").casefold(),
            )
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "form" and self._action is not None:
            self._finish_form()

    def close(self) -> None:
        super().close()
        if self._action is not None:
            self._finish_form()

    def _finish_form(self) -> None:
        self.forms.append(
            _HtmlForm(
                action=self._action or "",
                method=self._method,
                inputs=tuple(self._inputs),
            )
        )
        self._action = None
        self._method = "post"
        self._inputs = []


Transport = Callable[[str, str, bytes | None, Mapping[str, str]], _ProxyResponse]


class ProxyAuthenticator:
    def __init__(
        self,
        configuration: ProxyConfiguration,
        *,
        user_agent: str,
        status: Callable[[str], None] | None = None,
        username_reader: Callable[[str], str] | None = None,
        password_reader: Callable[[str], str] | None = None,
        pause_reader: Callable[[str], str] | None = None,
        browser_open: Callable[[str], bool] | None = None,
        transport: Transport | None = None,
    ):
        self.configuration = configuration
        self.user_agent = user_agent
        self.status = status or (lambda message: None)
        self.username_reader = username_reader or input
        self.password_reader = password_reader or getpass.getpass
        self.pause_reader = pause_reader or input
        self.browser_open = browser_open or (
            lambda url: bool(webbrowser.open(url, new=1))
        )
        self.transport = transport or self._request
        self._authenticated = (
            os.environ.get("EAP_PROXY_AUTHENTICATED")
            == configuration.session_token
        )

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def ensure_authenticated(
        self, *, force: bool = False
    ) -> ProxyAuthenticationResult:
        configuration = self.configuration
        if not configuration.configured:
            return ProxyAuthenticationResult(
                "disabled", "No hay un proxy configurado.", False
            )
        if not configuration.authentication_enabled:
            return ProxyAuthenticationResult(
                "not-required",
                "Proxy activado sin autenticación interactiva.",
                False,
            )
        if self._authenticated and not force:
            return ProxyAuthenticationResult(
                "cached",
                "La autenticación del proxy ya se realizó en este proceso.",
                True,
            )
        if configuration.authentication_type == "browser":
            result = self._authenticate_in_browser()
        else:
            result = self._authenticate_with_post()
        self._authenticated = result.authenticated
        if result.authenticated:
            os.environ["EAP_PROXY_AUTHENTICATED"] = configuration.session_token
        return result

    def _authenticate_with_post(self) -> ProxyAuthenticationResult:
        configuration = self.configuration
        self.status("Comprobando la autenticación del proxy...")
        page: _ProxyResponse | None = None
        if configuration.check_url:
            page = self.transport(
                "GET", configuration.check_url, None, self._headers()
            )
            if self._is_connected(page):
                return ProxyAuthenticationResult(
                    "already-connected",
                    "El proxy ya permite el acceso a Internet.",
                    True,
                )
        authentication_url = configuration.authentication_url
        if authentication_url is None:
            raise ValidationError("Falta proxy_authentication_url")
        if page is None or configuration.check_url != authentication_url:
            page = self.transport(
                "GET", authentication_url, None, self._headers()
            )
        form = self._authentication_form(page.body)
        if form is None:
            if self._is_connected(page):
                return ProxyAuthenticationResult(
                    "already-connected",
                    "El proxy ya permite el acceso a Internet.",
                    True,
                )
            raise NetworkError(
                "La página de autenticación no contiene un formulario utilizable"
            )
        username = self._read(self.username_reader, "Usuario del proxy: ").strip()
        password = self._read(
            self.password_reader, "Contraseña del proxy: "
        )
        if not username or not password:
            raise ValidationError(
                "El usuario y la contraseña del proxy no pueden estar vacíos"
            )
        fields = {
            item.name: item.value
            for item in form.inputs
            if item.input_type not in {"button", "file", "image", "reset", "submit"}
        }
        fields.update(configuration.extra_fields)
        fields[configuration.username_field] = username
        fields[configuration.password_field] = password
        action = urllib.parse.urljoin(page.url, form.action or page.url)
        _validate_remote_url(action, "action del formulario proxy")
        if (
            configuration.require_https
            and urllib.parse.urlparse(action).scheme.casefold() != "https"
        ):
            raise ValidationError(
                "El formulario proxy intenta enviar credenciales por HTTP; "
                "use proxy_authentication_require_https=false sólo si la "
                "organización lo exige"
            )
        payload = urllib.parse.urlencode(fields).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.status("Enviando la autenticación al proxy...")
        response = self.transport("POST", action, payload, headers)
        if response.status >= 400:
            raise NetworkError(
                f"El portal del proxy rechazó la autenticación ({response.status})"
            )
        if configuration.check_url:
            verification = self.transport(
                "GET", configuration.check_url, None, self._headers()
            )
            if not self._is_connected(verification):
                raise NetworkError(
                    "El portal respondió, pero el proxy sigue bloqueando Internet"
                )
        return ProxyAuthenticationResult(
            "authenticated", "Autenticación proxy completada.", True
        )

    def _authenticate_in_browser(self) -> ProxyAuthenticationResult:
        configuration = self.configuration
        if configuration.check_url:
            response = self.transport(
                "GET", configuration.check_url, None, self._headers()
            )
            if self._is_connected(response):
                return ProxyAuthenticationResult(
                    "already-connected",
                    "El proxy ya permite el acceso a Internet.",
                    True,
                )
        authentication_url = configuration.authentication_url
        if authentication_url is None:
            raise ValidationError("Falta proxy_authentication_url")
        self.status("Abriendo el portal de autenticación del proxy...")
        if not self.browser_open(authentication_url):
            raise NetworkError(
                "Windows no pudo abrir el portal de autenticación del proxy"
            )
        self._read(
            self.pause_reader,
            "Complete la autenticación en el navegador y pulse Entrar...",
        )
        if configuration.check_url:
            verification = self.transport(
                "GET", configuration.check_url, None, self._headers()
            )
            if not self._is_connected(verification):
                raise NetworkError(
                    "El proxy sigue bloqueando Internet tras cerrar el portal"
                )
        return ProxyAuthenticationResult(
            "authenticated", "Autenticación proxy completada.", True
        )

    @staticmethod
    def _read(reader: Callable[[str], str], prompt: str) -> str:
        try:
            return reader(prompt)
        except EOFError as exc:
            raise ValidationError(
                "La autenticación proxy requiere una terminal interactiva"
            ) from exc

    def _authentication_form(self, body: str) -> _HtmlForm | None:
        parser = _FormParser()
        parser.feed(body)
        parser.close()
        password_field = self.configuration.password_field.casefold()
        for form in parser.forms:
            if any(
                item.name.casefold() == password_field
                or item.input_type == "password"
                for item in form.inputs
            ):
                return form
        return None

    def _is_connected(self, response: _ProxyResponse) -> bool:
        return 200 <= response.status < 400 and self._authentication_form(
            response.body
        ) is None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "User-Agent": self.user_agent,
        }

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: Mapping[str, str],
    ) -> _ProxyResponse:
        configuration = self.configuration
        context = (
            ssl.create_default_context()
            if configuration.verify_tls
            else ssl._create_unverified_context()
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(configuration.handler_proxies()),
            urllib.request.HTTPSHandler(context=context),
        )
        request = urllib.request.Request(
            url, data=data, headers=dict(headers), method=method
        )
        try:
            with opener.open(
                request, timeout=configuration.timeout_seconds
            ) as response:
                payload = response.read(_MAXIMUM_PAGE_BYTES + 1)
                status = int(response.status)
                final_url = str(response.geturl())
                content_type = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            if exc.code == 407:
                raise NetworkError(
                    "El proxy exige autenticación HTTP en su propia URL"
                ) from exc
            payload = exc.read(_MAXIMUM_PAGE_BYTES + 1)
            status = int(exc.code)
            final_url = str(exc.geturl())
            content_type = exc.headers.get_content_charset() or "utf-8"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(
                f"No se pudo acceder al portal del proxy: {exc}"
            ) from exc
        if len(payload) > _MAXIMUM_PAGE_BYTES:
            raise NetworkError(
                "La respuesta del portal proxy supera el límite permitido"
            )
        return _ProxyResponse(
            status=status,
            url=final_url,
            body=payload.decode(content_type, errors="replace"),
        )
