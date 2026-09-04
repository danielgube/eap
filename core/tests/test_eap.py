from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import call, patch

from PIL import Image

from eap import cli as cli_module
from eap import shortcut_entry as shortcut_entry_module
from eap import transfers as transfers_module
from eap.application import EapApplication, UpdateInfo
from eap.catalog import Catalog, ComponentCatalogSource, ComponentDefinition
from eap.component_repositories import (
    ComponentRepositoryManager,
    update_component_repository_property,
)
from eap.cli import _is_escape, _render_main_dashboard
from eap.config import DEFAULTS, Settings
from eap.console_log import capture_console_output
from eap.core_tools import CoreTools
from eap.environments import EnvironmentStore
from eap.errors import (
    IntegrityError,
    NetworkError,
    TransactionError,
    ValidationError,
)
from eap.host_integrations import HostIntegrationManager
from eap.installer import ComponentInstaller
from eap.network import HttpClient
from eap.paths import EapPaths
from eap.proxy import (
    ProxyAuthenticator,
    ProxyConfiguration,
    apply_proxy_environment,
)
from eap.releases import (
    ASSET_TEMPLATE,
    EapReleasePublisher,
    EapReleaseResult,
    EapReleaseUpdater,
    EapUpdateResult,
    GitRepository,
    GitHubAsset,
    GitHubRelease,
    next_patch,
)
from eap.resolvers import ResolvedArtifact, resolve_component
from eap.shortcuts import WindowsShortcutManager
from eap.terminal import ManagedTerminal
from eap.transfers import EnvironmentTransfer
from eap.util import (
    atomic_write_json,
    component_version_key,
    java_version_key,
    load_json,
    sha256_file,
)


class FakeHttpClient:
    def __init__(self, response: Any):
        self.response = response

    def get_json(self, url: str, maximum_bytes: int = 5 * 1024 * 1024) -> Any:
        return self.response

    @staticmethod
    def require_https(url: str) -> None:
        if not url.startswith("https://"):
            raise ValidationError("HTTPS required")


class FakeTextHttpClient:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def get_text(
        self, url: str, maximum_bytes: int = 5 * 1024 * 1024
    ) -> str:
        return self.responses[url]

    @staticmethod
    def require_https(url: str) -> None:
        if not url.startswith("https://"):
            raise ValidationError("HTTPS required")


class FakeWebHttpClient:
    def __init__(self, responses: dict[str, str | Exception]):
        self.responses = responses
        self.text_calls: list[tuple[str, bool]] = []
        self.validated_urls: list[tuple[str, bool]] = []

    def get_text(
        self,
        url: str,
        maximum_bytes: int = 5 * 1024 * 1024,
        *,
        allow_http: bool = False,
    ) -> str:
        del maximum_bytes
        self.text_calls.append((url, allow_http))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def require_web_url(self, url: str, allow_http: bool = False) -> None:
        self.validated_urls.append((url, allow_http))
        scheme = urllib.parse.urlparse(url).scheme.casefold()
        if scheme == "https" or (scheme == "http" and allow_http):
            return
        raise ValidationError("HTTP(S) required")


class FakeDownloadHttpClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.download_calls: list[tuple[str, bool]] = []

    def download(
        self,
        url: str,
        destination: Path,
        progress: Any = None,
        maximum_bytes: int | None = None,
        *,
        allow_http: bool = False,
    ) -> tuple[str, int]:
        del maximum_bytes
        self.download_calls.append((url, allow_http))
        destination.write_bytes(self.payload)
        if progress is not None:
            progress(len(self.payload), len(self.payload))
        return url, len(self.payload)


class FakeNodeHttpClient(FakeHttpClient):
    def __init__(self, response: Any, texts: dict[str, str]):
        super().__init__(response)
        self.texts = texts

    def get_text(
        self, url: str, maximum_bytes: int = 5 * 1024 * 1024
    ) -> str:
        return self.texts[url]


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def component_info(
    component_id: str,
    description: str,
    *paths: tuple[str, str, str],
) -> dict[str, Any]:
    return {
        "description": description,
        "paths": [
            {
                "displayName": display_name,
                "base": base,
                "relativePath": relative_path,
            }
            for display_name, base, relative_path in paths
        ]
        or [
            {
                "displayName": "Datos del componente",
                "base": "profile",
                "relativePath": f"components/{component_id}",
            }
        ],
    }


def java_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "java",
        "displayName": "Java JDK",
        "kind": "runtime",
        "info": component_info(
            "java",
            "Distribuciones OpenJDK portables para Windows.",
            ("Home del usuario Java", "profile", "home"),
        ),
        "launchers": [],
        "capability": {"id": "runtime.java", "exclusive": True},
        "versioning": {"scheme": "java"},
        "tracks": [
            {"id": 17, "displayName": "Java 17 LTS"},
            {"id": 21, "displayName": "Java 21 LTS"},
            {"id": 25, "displayName": "Java 25 LTS"},
        ],
        "providers": [
            {
                "id": "temurin",
                "componentId": "java-temurin",
                "displayName": "Eclipse Temurin",
                "resolver": {
                    "type": "adoptium-v3",
                    "baseUrl": "https://api.example",
                    "vendor": "eclipse",
                    "jvmImpl": "hotspot",
                },
                "verification": {"implementorContains": "Eclipse Adoptium"},
            },
            {
                "id": "corretto",
                "componentId": "java-corretto",
                "displayName": "Amazon Corretto",
                "resolver": {
                    "type": "corretto-index",
                    "indexUrl": "https://index.example/index.json",
                    "resourceBaseUrl": "https://resource.example",
                },
                "verification": {"implementorContains": "Amazon.com"},
            },
        ],
        "install": {
            "directoryTemplate": "java/{provider}/{version}",
            "stripSingleRoot": True,
            "requiredFiles": ["bin/java.exe", "bin/javac.exe", "release"],
            "validation": {"type": "java-release"},
        },
        "environment": {
            "variables": {
                "JAVA_HOME": "{{component.root}}",
                "JAVA_TOOL_OPTIONS": (
                    "-Duser.home=\"{{profile.home}}\" "
                    "-Djava.io.tmpdir=\"{{profile.temp}}\""
                ),
            },
            "appendable": ["JAVA_TOOL_OPTIONS"],
            "path": ["{{component.root}}/bin"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def maven_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "maven",
        "displayName": "Apache Maven",
        "kind": "tool",
        "info": component_info(
            "maven",
            "Herramienta de construcción para Java.",
            ("Configuración Maven", "profile", "home/.m2/settings.xml"),
        ),
        "launchers": [],
        "capability": {"id": "tool.maven", "exclusive": True},
        "requires": [
            {"capability": "runtime.java", "minimumTrack": 8}
        ],
        "tracks": [{"id": 3, "displayName": "Maven 3 estable"}],
        "defaultProvider": "apache",
        "defaultTrack": 3,
        "providers": [
            {
                "id": "apache",
                "componentId": "apache-maven",
                "displayName": "Apache Software Foundation",
                "resolver": {
                    "type": "html-directory",
                    "indexUrl": "https://downloads.example/maven-3/",
                    "releasePattern": (
                        r'''href=["'](?P<version>\d+\.\d+\.\d+)/["']'''
                    ),
                    "artifactUrlTemplate": (
                        "https://downloads.example/maven-3/{version}/"
                        "binaries/apache-maven-{version}-bin.zip"
                    ),
                    "checksumUrlTemplate": "{artifactUrl}.sha512",
                    "checksumAlgorithm": "sha512",
                },
                "verification": {"checksumAlgorithm": "sha512"},
            }
        ],
        "install": {
            "directoryTemplate": "maven/{provider}/{version}",
            "stripSingleRoot": True,
            "requiredFiles": ["bin/mvn.cmd", "conf/settings.xml"],
            "validation": {
                "type": "command",
                "command": ["bin/mvn.cmd", "--version"],
                "expectContains": "Apache Maven",
            },
        },
        "data": {
            "directories": [
                {
                    "path": "{{profile.home}}/.m2",
                    "displayName": "Configuración Maven",
                    "role": "configuration",
                    "showInDashboard": True,
                },
                {
                    "path": "{{profile.home}}/.m2/repository",
                    "displayName": "Repositorio local",
                    "role": "repository",
                    "showInDashboard": False,
                },
            ],
            "files": [
                {
                    "path": "{{profile.home}}/.m2/settings.xml",
                    "displayName": "settings.xml",
                    "role": "configuration",
                    "showInDashboard": False,
                    "mode": "if-missing",
                    "content": (
                        "<settings>\n"
                        "  <localRepository>${user.home}/.m2/repository"
                        "</localRepository>\n"
                        "</settings>\n"
                    ),
                }
            ],
        },
        "environment": {
            "variables": {"MAVEN_HOME": "{{component.root}}"},
            "path": ["{{component.root}}/bin"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def tomcat_component(manifest_path: Path) -> ComponentDefinition:
    return ComponentDefinition(
        manifest_path,
        {
            "id": "tomcat",
            "displayName": "Apache Tomcat",
            "kind": "server",
            "tracks": [
                {"id": 9, "displayName": "Tomcat 9"},
                {"id": 10, "displayName": "Tomcat 10.1"},
                {"id": 11, "displayName": "Tomcat 11"},
            ],
            "providers": [
                {
                    "id": "apache",
                    "componentId": "apache-tomcat",
                    "displayName": "Apache Software Foundation",
                    "resolver": {
                        "type": "html-directory",
                        "indexUrl": (
                            "https://downloads.example/tomcat/tomcat-"
                            "{track}/"
                        ),
                        "releasePattern": (
                            r'''href=["']v(?P<version>\d+\.\d+\.\d+)/["']'''
                        ),
                        "artifactUrlTemplate": (
                            "https://downloads.example/tomcat/tomcat-"
                            "{track}/v{version}/bin/"
                            "apache-tomcat-{version}.zip"
                        ),
                        "checksumUrlTemplate": "{artifactUrl}.sha512",
                        "checksumAlgorithm": "sha512",
                    },
                }
            ],
        },
    )


def git_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "git",
        "displayName": "Git",
        "kind": "tool",
        "info": component_info(
            "git",
            "Git for Windows portable.",
            ("Configuración Git", "profile", "home/.gitconfig"),
        ),
        "launchers": [],
        "capability": {"id": "tool.git", "exclusive": True},
        "tracks": [{"id": 2, "displayName": "Git 2 estable"}],
        "defaultProvider": "git-for-windows",
        "defaultTrack": 2,
        "providers": [
            {
                "id": "git-for-windows",
                "componentId": "git-for-windows-mingit",
                "displayName": "Git for Windows · MinGit ZIP",
                "resolver": {
                    "type": "github-release-asset",
                    "apiUrl": "https://api.example/releases/latest",
                    "assetPattern": (
                        r"^MinGit-(?P<version>\d+\.\d+\.\d+\.\d+)"
                        r"-64-bit\.zip$"
                    ),
                },
                "verification": {"checksumAlgorithm": "sha256"},
            }
        ],
        "install": {
            "directoryTemplate": "git/{provider}/{version}",
            "stripSingleRoot": False,
            "requiredFiles": ["cmd/git.exe", "mingw64/bin/git.exe"],
            "validation": {
                "type": "command",
                "command": ["cmd/git.exe", "--version"],
                "expectContains": "git version",
            },
        },
        "data": {
            "directories": [
                {
                    "path": "{{profile.home}}/.ssh",
                    "displayName": "Claves SSH",
                    "role": "configuration",
                }
            ],
            "files": [
                {
                    "path": "{{profile.home}}/.gitconfig",
                    "displayName": "Configuración Git",
                    "role": "configuration",
                    "mode": "if-missing",
                    "content": "",
                }
            ],
        },
        "environment": {
            "variables": {
                "GIT_HOME": "{{component.root}}",
                "GIT_CONFIG_GLOBAL": "{{profile.home}}/.gitconfig",
            },
            "path": ["{{component.root}}/cmd"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def nodejs_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "nodejs",
        "displayName": "Node.js",
        "kind": "runtime",
        "info": component_info(
            "nodejs",
            "Runtime JavaScript y herramientas npm.",
            ("Caché npm", "profile", "home/.npm"),
            ("Configuración npm (.npmrc)", "profile", "home/.npmrc"),
        ),
        "launchers": [],
        "capability": {"id": "runtime.nodejs", "exclusive": True},
        "tracks": [
            {"id": 22, "displayName": "Node.js 22 LTS · Jod"},
            {"id": 24, "displayName": "Node.js 24 LTS · Krypton"},
            {"id": 26, "displayName": "Node.js 26 Current"},
        ],
        "defaultProvider": "nodejs",
        "defaultTrack": 24,
        "providers": [
            {
                "id": "nodejs",
                "componentId": "nodejs-official",
                "displayName": "Node.js Foundation",
                "resolver": {
                    "type": "nodejs-index",
                    "indexUrl": "https://node.example/dist/index.json",
                    "downloadBaseUrl": "https://node.example/dist",
                },
                "verification": {"checksumAlgorithm": "sha256"},
            }
        ],
        "install": {
            "directoryTemplate": "nodejs/{provider}/{version}",
            "stripSingleRoot": True,
            "requiredFiles": ["node.exe", "npm.cmd", "npx.cmd"],
            "validation": {
                "type": "command",
                "command": ["node.exe", "--version"],
                "expectContains": "v",
            },
        },
        "data": {
            "directories": [
                {
                    "path": "{{profile.home}}/.npm",
                    "displayName": "Caché npm",
                    "role": "cache",
                },
                {
                    "path": "{{profile.home}}/.npm-global",
                    "displayName": "Paquetes npm globales",
                    "role": "repository",
                },
            ],
            "files": [],
        },
        "environment": {
            "variables": {
                "NODE_HOME": "{{component.root}}",
                "NPM_CONFIG_CACHE": "{{profile.home}}/.npm",
                "NPM_CONFIG_PREFIX": "{{profile.home}}/.npm-global",
            },
            "path": ["{{component.root}}"],
            "dataPath": ["{{profile.home}}/.npm-global"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def python_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "python",
        "displayName": "Python",
        "kind": "runtime",
        "info": component_info(
            "python",
            "Runtime CPython con pip y venv.",
            ("Caché pip", "profile", "home/.cache/pip"),
        ),
        "launchers": [],
        "capability": {"id": "runtime.python", "exclusive": True},
        "tracks": [
            {"id": "3.12", "displayName": "Python 3.12"},
            {"id": "3.13", "displayName": "Python 3.13"},
            {"id": "3.14", "displayName": "Python 3.14"},
        ],
        "defaultProvider": "pythoncore",
        "defaultTrack": "3.14",
        "providers": [
            {
                "id": "pythoncore",
                "componentId": "pythoncore-official",
                "displayName": "CPython oficial · PythonCore",
                "resolver": {
                    "type": "python-install-manager-index",
                    "indexUrl": "https://python.example/index-windows.json",
                    "company": "PythonCore",
                    "architectureTag": "64",
                },
                "verification": {"checksumAlgorithm": "sha256"},
            }
        ],
        "install": {
            "directoryTemplate": "python/{provider}/{version}",
            "stripSingleRoot": False,
            "requiredFiles": [
                "python.exe",
                "Lib/venv/__init__.py",
                "Lib/site-packages/pip/__init__.py",
            ],
            "validation": {
                "type": "command",
                "command": ["python.exe", "--version"],
                "expectContains": "Python",
            },
        },
        "data": {
            "directories": [
                {
                    "path": "{{profile.home}}/.python/Scripts",
                    "displayName": "Paquetes de usuario Python",
                    "role": "repository",
                },
                {
                    "path": "{{profile.home}}/.cache/pip",
                    "displayName": "Caché pip",
                    "role": "cache",
                },
            ],
            "files": [],
        },
        "environment": {
            "variables": {
                "PYTHONUSERBASE": "{{profile.home}}/.python",
                "PIP_USER": "1",
                "PIP_CACHE_DIR": "{{profile.home}}/.cache/pip",
            },
            "unset": ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"],
            "path": [
                "{{component.root}}",
            ],
            "commands": [
                {
                    "name": "pip",
                    "executable": "{{component.root}}/python.exe",
                    "arguments": ["-m", "pip"],
                }
            ],
            "dataPath": ["{{profile.home}}/.python/Scripts"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def golang_component(manifest_path: Path) -> ComponentDefinition:
    return ComponentDefinition(
        manifest_path,
        {
            "id": "golang",
            "displayName": "Go",
            "tracks": [
                {"id": "1.26", "displayName": "Go 1.26"},
                {"id": "1.27", "displayName": "Go 1.27"},
            ],
            "providers": [
                {
                    "id": "go-dev",
                    "componentId": "golang-official",
                    "displayName": "Go oficial",
                    "resolver": {
                        "type": "json-index",
                        "indexUrl": "https://go.example/dl/?mode=json",
                        "releases": {
                            "path": "/",
                            "versionPath": "/version",
                            "versionPattern": (
                                r"^go(?P<version>\d+\.\d+\.\d+)$"
                            ),
                            "filters": {"/stable": True},
                        },
                        "artifacts": {
                            "path": "/files",
                            "filters": {
                                "/os": "windows",
                                "/arch": "amd64",
                                "/kind": "archive",
                            },
                            "fileNamePath": "/filename",
                            "urlTemplate": (
                                "https://go.example/dl/{fileName}"
                            ),
                            "sha256Path": "/sha256",
                            "sizePath": "/size",
                        },
                    },
                }
            ],
        },
    )


def php_component(manifest_path: Path) -> ComponentDefinition:
    return ComponentDefinition(
        manifest_path,
        {
            "id": "php",
            "displayName": "PHP",
            "tracks": [
                {"id": "8.4", "displayName": "PHP 8.4"},
                {"id": "8.5", "displayName": "PHP 8.5"},
            ],
            "providers": [
                {
                    "id": "php-windows",
                    "componentId": "php-windows-official",
                    "displayName": "PHP oficial · NTS x64",
                    "resolver": {
                        "type": "json-index",
                        "indexUrl": "https://php.example/releases.json",
                        "releases": {
                            "path": "/{track}",
                            "versionPath": "/version",
                        },
                        "artifacts": {
                            "path": "/nts-vs*-x64/zip",
                            "selection": "last",
                            "fileNamePath": "/path",
                            "urlTemplate": (
                                "https://php.example/releases/{fileName}"
                            ),
                            "sha256Path": "/sha256",
                        },
                    },
                }
            ],
        },
    )


def dbeaver_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "dbeaver",
        "displayName": "DBeaver Community",
        "kind": "application",
        "info": component_info(
            "dbeaver",
            "Cliente universal de bases de datos.",
        ),
        "launchers": [
            {
                "id": "dbeaver",
                "displayName": "DBeaver Community",
                "type": "application",
                "workspaceMode": "component-data",
                "executable": "{{component.root}}/dbeaver.exe",
                "arguments": [
                    "-configuration",
                    (
                        "{{data.component.uri}}/runtime/"
                        "{{component.version}}/configuration"
                    ),
                    "-data",
                    "{{workspace.selected}}",
                ],
                "environment": {
                    "EAP_COMPONENT_DATA": "{{data.component}}",
                    "EAP_WORKSPACE": "{{workspace.selected}}",
                },
                "unset": ["CLASSPATH", "JDK_JAVA_OPTIONS"],
                "dataCopies": [
                    {
                        "source": "{{component.root}}/configuration",
                        "target": (
                            "{{data.component}}/runtime/"
                            "{{component.version}}/configuration"
                        ),
                        "mode": "if-missing",
                    },
                    {
                        "source": "{{component.root}}/p2",
                        "target": (
                            "{{data.component}}/runtime/"
                            "{{component.version}}/p2"
                        ),
                        "mode": "if-missing",
                    },
                ],
                "startMode": "detached",
            }
        ],
        "capability": {"id": "app.database-client", "exclusive": False},
        "tracks": [
            {"id": 26, "displayName": "DBeaver 26.x estable"}
        ],
        "defaultProvider": "community",
        "defaultTrack": 26,
        "updatePolicy": "same-track",
        "majorUpdates": "confirm-component-name",
        "providers": [
            {
                "id": "community",
                "componentId": "dbeaver-community",
                "displayName": "DBeaver Community",
                "resolver": {
                    "type": "github-release-asset",
                    "apiUrl": "https://api.example/releases?per_page=100",
                    "assetPattern": (
                        r"^dbeaver-ce-(?P<version>\d+\.\d+\.\d+)"
                        r"-windows-x86_64\.zip$"
                    ),
                },
                "verification": {"checksumAlgorithm": "sha256"},
            }
        ],
        "install": {
            "directoryTemplate": "dbeaver/{provider}/{version}",
            "stripSingleRoot": True,
            "requiredFiles": [
                "dbeaver.exe",
                "dbeaverc.exe",
                "dbeaver.ini",
                "jre/bin/javaw.exe",
            ],
            "validation": {"type": "files-only"},
        },
        "data": {
            "directories": [
                {
                    "path": "{{data.component}}",
                    "displayName": "Datos privados",
                    "role": "data",
                },
                {
                    "path": "{{data.component}}/workspace",
                    "displayName": "Workspace auxiliar",
                    "role": "workspace",
                },
            ],
            "files": [],
        },
        "environment": {"variables": {}, "path": []},
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def vscode_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "vscode",
        "displayName": "Visual Studio Code",
        "kind": "application",
        "info": component_info(
            "vscode",
            "Editor de Microsoft ligado al workspace.",
        ),
        "launchers": [
            {
                "id": "vscode",
                "displayName": "Visual Studio Code",
                "type": "application",
                "workspaceMode": "environment",
                "executable": "{{component.root}}/Code.exe",
                "arguments": [
                    "--user-data-dir",
                    "{{data.component}}\\user-data",
                    "--extensions-dir",
                    "{{data.component}}\\extensions",
                    "--disable-updates",
                    "--new-window",
                    "{{workspace.selected}}",
                ],
                "environment": {
                    "EAP_COMPONENT_DATA": "{{data.component}}",
                    "EAP_WORKSPACE": "{{workspace.selected}}",
                },
                "dataDirectories": [
                    "{{data.component}}\\user-data",
                    "{{data.component}}\\extensions",
                ],
                "startMode": "detached",
            }
        ],
        "capability": {"id": "app.ide.vscode", "exclusive": True},
        "tracks": [{"id": 1, "displayName": "VS Code 1.x estable"}],
        "defaultProvider": "microsoft",
        "defaultTrack": 1,
        "majorUpdates": "confirm-component-name",
        "providers": [
            {
                "id": "microsoft",
                "componentId": "vscode-microsoft",
                "displayName": "Microsoft",
                "resolver": {
                    "type": "vscode-update-api",
                    "updateUrl": "https://update.example/latest",
                },
                "verification": {"checksumAlgorithm": "sha256"},
            }
        ],
        "install": {
            "directoryTemplate": "vscode/{provider}/{version}",
            "stripSingleRoot": False,
            "requiredFiles": [
                "Code.exe",
                "bin/code.cmd",
                "Code.VisualElementsManifest.xml",
            ],
            "validation": {"type": "files-only"},
        },
        "data": {
            "directories": [
                {
                    "path": "{{data.component}}/user-data",
                    "displayName": "Configuración VS Code",
                    "role": "configuration",
                },
                {
                    "path": "{{data.component}}/extensions",
                    "displayName": "Extensiones VS Code",
                    "role": "extensions",
                },
            ],
            "files": [],
        },
        "environment": {
            "variables": {"VSCODE_HOME": "{{component.root}}"},
            "path": ["{{component.root}}/bin"],
        },
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


def html_links_component(
    manifest_path: Path,
    *,
    index_url: str = "https://downloads.example/releases/",
) -> ComponentDefinition:
    component = vscode_component(manifest_path)
    component.value["updatePolicy"] = "same-track"
    provider = component.provider("microsoft")
    provider["resolver"] = {
        "type": "html-links",
        "indexUrl": index_url,
        "linkPattern": (
            r"^Microsoft\.WindowsTerminal.*?_"
            r"(?P<version>\d+(?:\.\d+){3})_x64\.zip$"
        ),
        "excludePatterns": [r".*Preview.*"],
    }
    provider["verification"] = {"type": "none"}
    manifest_path.write_text(
        json.dumps(component.value), encoding="utf-8"
    )
    return component


def intellij_component() -> ComponentDefinition:
    manifest_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "intellij-idea.json"
    )
    return ComponentDefinition(manifest_path, load_json(manifest_path))


def eclipse_component() -> ComponentDefinition:
    manifest_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "eclipse.json"
    )
    return ComponentDefinition(manifest_path, load_json(manifest_path))


def bruno_component() -> ComponentDefinition:
    manifest_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "bruno.json"
    )
    return ComponentDefinition(manifest_path, load_json(manifest_path))


def external_component(manifest_path: Path) -> ComponentDefinition:
    value = {
        "schemaVersion": 1,
        "id": "kiro",
        "displayName": "Kiro",
        "kind": "external",
        "info": component_info(
            "kiro",
            "Instalación externa ligada al workspace.",
            ("Workspace activo", "workspace", "."),
        ),
        "launchers": [
            {
                "id": "kiro",
                "displayName": "Kiro",
                "type": "application",
                "workspaceMode": "environment",
                "executable": "{{external.executable}}",
                "arguments": ["--new-window", "{{workspace.selected}}"],
                "startMode": "detached",
            }
        ],
        "tracks": [
            {"id": "local", "displayName": "Instalación local"}
        ],
        "defaultProvider": "external",
        "defaultTrack": "local",
        "updatePolicy": "manual",
        "providers": [
            {
                "id": "external",
                "componentId": "kiro-external",
                "displayName": "Instalación externa",
                "resolver": {"type": "external-executable"},
                "verification": {"type": "local-executable"},
            }
        ],
        "install": {
            "type": "external-executable",
            "executableNames": ["kiro.exe"],
            "prompt": "Ruta completa al ejecutable kiro.exe",
        },
        "data": {"directories": [], "files": []},
        "environment": {"variables": {}, "path": []},
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return ComponentDefinition(manifest_path, value)


class ComponentInfoTests(unittest.TestCase):
    def test_obsolete_track_is_mapped_only_to_a_compatible_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = dbeaver_component(
                Path(temporary) / "dbeaver.json"
            )

            self.assertEqual(
                26, component.compatible_track("26.1", "26.1.5")
            )
            with self.assertRaisesRegex(ValidationError, "no soportada"):
                component.compatible_track("25.3", "25.3.5")

    def test_major_update_policy_requires_major_number_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["tracks"] = [
                {"id": "26.1", "displayName": "DBeaver 26.1"}
            ]
            value["defaultTrack"] = "26.1"

            with self.assertRaisesRegex(
                ValidationError, "tracks enteros positivos"
            ):
                Catalog._validate_component(value, "dbeaver", path)

    def test_json_index_contract_rejects_unsafe_declarations(self) -> None:
        path = Path("external-component.json")
        resolver = golang_component(path).provider("go-dev")["resolver"]

        Catalog._validate_json_index_resolver(resolver, path)

        invalid_pattern = json.loads(json.dumps(resolver))
        invalid_pattern["releases"]["versionPattern"] = r"^go\d+$"
        with self.assertRaisesRegex(ValidationError, "grupo"):
            Catalog._validate_json_index_resolver(invalid_pattern, path)

        invalid_url = json.loads(json.dumps(resolver))
        invalid_url["artifacts"]["urlTemplate"] = (
            "http://example.test/{fileName}"
        )
        with self.assertRaisesRegex(ValidationError, "urlTemplate"):
            Catalog._validate_json_index_resolver(invalid_url, path)

        invalid_url_token = json.loads(json.dumps(resolver))
        invalid_url_token["artifacts"]["urlTemplate"] = (
            "https://example.test/{component}/{fileName}"
        )
        with self.assertRaisesRegex(ValidationError, "tokens"):
            Catalog._validate_json_index_resolver(
                invalid_url_token, path
            )

        invalid_token = json.loads(json.dumps(resolver))
        invalid_token["releases"]["path"] = "/{component}/releases"
        with self.assertRaisesRegex(ValidationError, "tokens"):
            Catalog._validate_json_index_resolver(invalid_token, path)

        malformed_token = json.loads(json.dumps(resolver))
        malformed_token["releases"]["path"] = "/{track/releases"
        with self.assertRaisesRegex(ValidationError, "tokens"):
            Catalog._validate_json_index_resolver(malformed_token, path)

    def test_html_directory_contract_rejects_unsafe_declarations(self) -> None:
        path = Path("external-component.json")
        resolver = tomcat_component(path).provider("apache")["resolver"]

        Catalog._validate_html_directory_resolver(resolver, path)

        invalid_pattern = json.loads(json.dumps(resolver))
        invalid_pattern["releasePattern"] = r"href=v\d+/"
        with self.assertRaisesRegex(ValidationError, "grupo"):
            Catalog._validate_html_directory_resolver(
                invalid_pattern, path
            )

        invalid_url = json.loads(json.dumps(resolver))
        invalid_url["artifactUrlTemplate"] = (
            "http://example.test/{version}.zip"
        )
        with self.assertRaisesRegex(ValidationError, "artifactUrlTemplate"):
            Catalog._validate_html_directory_resolver(invalid_url, path)

        invalid_token = json.loads(json.dumps(resolver))
        invalid_token["indexUrl"] = (
            "https://example.test/{component}/"
        )
        with self.assertRaisesRegex(ValidationError, "tokens"):
            Catalog._validate_html_directory_resolver(invalid_token, path)

    def test_html_links_contract_accepts_declarative_web_crawling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vscode.json"
            component = html_links_component(path)
            resolver = component.provider("microsoft")["resolver"]
            resolver["followPattern"] = r"^v\d+(?:\.\d+){3}$"
            resolver["maxDepth"] = 3

            Catalog._validate_component(component.value, "vscode", path)

            resolver["indexUrl"] = "http://intranet.example/releases/"
            Catalog._validate_component(component.value, "vscode", path)

    def test_html_links_contract_rejects_invalid_patterns_and_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vscode.json"
            component = html_links_component(path)
            original = component.provider("microsoft")["resolver"]

            invalid_link_pattern = json.loads(json.dumps(original))
            invalid_link_pattern["linkPattern"] = "["
            component.provider("microsoft")["resolver"] = invalid_link_pattern
            with self.assertRaisesRegex(ValidationError, "linkPattern"):
                Catalog._validate_component(component.value, "vscode", path)

            missing_version_group = json.loads(json.dumps(original))
            missing_version_group["linkPattern"] = r"^tool-\d+\.zip$"
            component.provider("microsoft")[
                "resolver"
            ] = missing_version_group
            with self.assertRaisesRegex(ValidationError, "version"):
                Catalog._validate_component(component.value, "vscode", path)

            invalid_exclusions = json.loads(json.dumps(original))
            invalid_exclusions["excludePatterns"] = "Preview"
            component.provider("microsoft")["resolver"] = invalid_exclusions
            with self.assertRaisesRegex(ValidationError, "excludePatterns"):
                Catalog._validate_component(component.value, "vscode", path)

            malformed_exclusion = json.loads(json.dumps(original))
            malformed_exclusion["excludePatterns"] = ["["]
            component.provider("microsoft")["resolver"] = malformed_exclusion
            with self.assertRaisesRegex(ValidationError, "excludePatterns"):
                Catalog._validate_component(component.value, "vscode", path)

            invalid_follow_pattern = json.loads(json.dumps(original))
            invalid_follow_pattern["followPattern"] = "["
            component.provider("microsoft")[
                "resolver"
            ] = invalid_follow_pattern
            with self.assertRaisesRegex(ValidationError, "followPattern"):
                Catalog._validate_component(component.value, "vscode", path)

            for invalid_depth in (0, 4, True, "2"):
                with self.subTest(maxDepth=invalid_depth):
                    invalid_max_depth = json.loads(json.dumps(original))
                    invalid_max_depth["followPattern"] = r"^v\d+$"
                    invalid_max_depth["maxDepth"] = invalid_depth
                    component.provider("microsoft")[
                        "resolver"
                    ] = invalid_max_depth
                    with self.assertRaisesRegex(ValidationError, "maxDepth"):
                        Catalog._validate_component(
                            component.value, "vscode", path
                        )

            invalid_url = json.loads(json.dumps(original))
            invalid_url["indexUrl"] = "ftp://downloads.example/releases/"
            component.provider("microsoft")["resolver"] = invalid_url
            with self.assertRaisesRegex(ValidationError, "indexUrl"):
                Catalog._validate_component(component.value, "vscode", path)

    def test_server_kind_and_appendable_environment_are_manifest_driven(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["kind"] = "server"
            value["environment"]["variables"]["SERVER_OPTIONS"] = "--safe"
            value["environment"]["appendable"] = ["SERVER_OPTIONS"]

            Catalog._validate_component(value, "dbeaver", path)

            value["environment"]["appendable"] = ["UNKNOWN_OPTIONS"]
            with self.assertRaisesRegex(
                ValidationError, "environment.appendable"
            ):
                Catalog._validate_component(value, "dbeaver", path)

    def test_component_info_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["schemaVersion"] = 2
            del value["info"]

            with self.assertRaisesRegex(
                ValidationError, "Faltan campos.*info"
            ):
                Catalog._validate_component(value, "dbeaver", path)

    def test_schema_one_component_remains_compatible_without_info(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            component = dbeaver_component(path)
            del component.value["info"]

            Catalog._validate_component(component.value, "dbeaver", path)

            self.assertEqual(
                "components/dbeaver",
                component.important_paths[0]["relativePath"],
            )

    def test_schema_three_requires_the_path_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["schemaVersion"] = 3

            with self.assertRaisesRegex(
                ValidationError, "Faltan campos.*type"
            ):
                Catalog._validate_component(value, "dbeaver", path)

            for item in value["info"]["paths"]:
                item["type"] = "directory"
            Catalog._validate_component(value, "dbeaver", path)

    def test_legacy_info_path_type_is_inferred_from_component_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = nodejs_component(Path(temporary) / "nodejs.json")
            component.value["data"]["files"].append(
                {
                    "path": "{{profile.home}}/.npmrc",
                    "displayName": "Configuración npm",
                    "role": "configuration",
                    "mode": "if-missing",
                    "content": "",
                }
            )
            paths = {
                item["relativePath"]: item["type"]
                for item in component.important_paths
            }

            self.assertEqual("directory", paths["home/.npm"])
            self.assertEqual("file", paths["home/.npmrc"])

    def test_component_info_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["info"]["paths"][0]["relativePath"] = "../secreto"

            with self.assertRaisesRegex(
                ValidationError, "rutas relativas seguras"
            ):
                Catalog._validate_component(value, "dbeaver", path)

    def test_component_info_resolves_profile_and_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create(
                "default",
                workspace_id="proyecto",
                data_profile_id="desarrollo",
            )
            component = dbeaver_component(paths.temp / "dbeaver.json")
            component.value["info"]["paths"].append(
                {
                    "displayName": "Workspace activo",
                    "base": "workspace",
                    "relativePath": ".",
                }
            )
            app = SimpleNamespace(paths=paths, environments=environments)

            sections = cli_module._component_information_sections(
                app, "default", component
            )
            rendered = "\n".join(
                row for _, rows in sections for row in rows
            )

            self.assertIn(
                str(
                    paths.data
                    / "profiles"
                    / "desarrollo"
                    / "components"
                    / "dbeaver"
                ),
                rendered,
            )
            self.assertIn(str(paths.workspaces / "proyecto"), rendered)


class VersionTests(unittest.TestCase):
    def test_java_and_corretto_versions_sort_numerically(self) -> None:
        self.assertGreater(
            java_version_key("21.0.12.1+1", "temurin"),
            java_version_key("21.0.11+9", "temurin"),
        )
        self.assertGreater(
            java_version_key("21.0.12.9.1", "corretto"),
            java_version_key("21.0.11.10.1", "corretto"),
        )
        self.assertGreater(
            java_version_key("21.0.12.1+1", "temurin"),
            java_version_key("21.0.12+8", "temurin"),
        )
        self.assertGreater(
            component_version_key("maven", "3.9.16", "apache"),
            component_version_key("maven", "3.9.15", "apache"),
        )

    def test_component_version_scheme_comes_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            java = java_component(Path(temporary) / "java.json")
            self.assertEqual(
                java_version_key("21.0.12.9.1", "corretto"),
                java.comparable_version_key("21.0.12.9.1"),
            )
            component = dbeaver_component(
                Path(temporary) / "dbeaver.json"
            )
            component.value["versioning"] = {"scheme": "unsupported"}
            with self.assertRaisesRegex(
                ValidationError, "Esquema de versiones"
            ):
                Catalog._validate_component(
                    component.value, "dbeaver", component.manifest_path
                )

    def test_python_tracks_preserve_minor_version_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = python_component(Path(temporary) / "python.json")
            self.assertEqual("3.14", component.validate_track("3.14"))
            with self.assertRaises(ValidationError):
                component.validate_track("3.15")


class EapReleaseTests(unittest.TestCase):
    _MANAGED = [
        "README.md",
        "config.properties.example",
        "eap.cmd",
        "core/app",
        "core/bootstrap.ps1",
        "core/catalog",
        "core/commands",
        "core/core_tools.json",
        "core/release.json",
        "core/version.json",
    ]

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _release_archive(
        self, root: Path, version: str = "0.20.0"
    ) -> tuple[Path, GitHubRelease]:
        archive = root / ASSET_TEMPLATE.format(version=version)
        manifest = {
            "schemaVersion": 1,
            "repository": "danielgube/eap",
            "assetName": ASSET_TEMPLATE,
            "managedPaths": self._MANAGED,
        }
        files = {
            "README.md": "EAP actualizado\n",
            "config.properties.example": "profile.default=default\n",
            "eap.cmd": "@echo off\r\n",
            "core/app/eap/__init__.py": (
                '"""EAP."""\n\n'
                f'__version__ = "{version}"\n'
            ),
            "core/app/eap/new_module.py": "VALUE = 1\n",
            "core/bootstrap.ps1": "# bootstrap actualizado\n",
            "core/catalog/catalog.json": "{}\n",
            "core/commands/eap.cmd": "@echo off\r\n",
            "core/core_tools.json": "{}\n",
            "core/release.json": json.dumps(manifest),
            "core/version.json": json.dumps(
                {"schemaVersion": 1, "name": "EAP", "version": version}
            ),
        }
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as package:
            for name, content in files.items():
                package.writestr(name, content)
        digest = sha256_file(archive)
        asset = GitHubAsset(
            id=10,
            name=archive.name,
            browser_download_url=(
                "https://github.com/danielgube/eap/releases/download/"
                f"v{version}/{archive.name}"
            ),
            digest=f"sha256:{digest}",
            size=archive.stat().st_size,
        )
        release = GitHubRelease(
            id=20,
            tag_name=f"v{version}",
            name=f"EAP v{version}",
            html_url=f"https://github.com/danielgube/eap/releases/v{version}",
            published_at="2026-08-28T12:00:00Z",
            assets=(asset,),
        )
        return archive, release

    def test_release_version_uses_first_local_then_increments_patch(
        self,
    ) -> None:
        self.assertEqual("0.19.1", next_patch("v0.19.0"))
        self.assertEqual(
            "0.19.0", EapReleasePublisher._target_version("0.19.0", None)
        )
        latest = GitHubRelease(
            id=1,
            tag_name="v0.19.0",
            name="EAP v0.19.0",
            html_url="https://example.test/release",
            published_at=None,
            assets=(
                GitHubAsset(
                    id=10,
                    name="eap-0.19.0-windows-x64.zip",
                    browser_download_url="https://example.test/eap.zip",
                    digest=f"sha256:{'a' * 64}",
                    size=1,
                ),
                GitHubAsset(
                    id=11,
                    name="eap-0.19.0-windows-x64.zip.sha256",
                    browser_download_url="https://example.test/eap.sha256",
                    digest=f"sha256:{'b' * 64}",
                    size=1,
                ),
            ),
        )
        self.assertEqual(
            "0.19.1",
            EapReleasePublisher._target_version("0.19.0", latest),
        )
        self.assertEqual(
            "0.19.1",
            EapReleasePublisher._target_version("0.19.1", latest),
        )
        incomplete = GitHubRelease(
            id=2,
            tag_name="v0.19.0",
            name="EAP v0.19.0",
            html_url="https://example.test/incomplete",
            published_at=None,
            assets=(),
        )
        self.assertEqual(
            "0.19.0",
            EapReleasePublisher._target_version("0.19.0", incomplete),
        )
        with self.assertRaises(ValidationError):
            EapReleasePublisher._target_version("0.20.0", latest)

    def test_public_update_replaces_only_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            self._write(root / "README.md", "anterior\n")
            self._write(root / "eap.cmd", "anterior\n")
            self._write(
                paths.core / "app" / "eap" / "obsolete.py", "OLD = 1\n"
            )
            self._write(
                paths.core / "app" / "eap" / "__init__.py",
                '__version__ = "0.19.0"\n',
            )
            self._write(paths.core / "bootstrap.ps1", "# anterior\n")
            self._write(paths.catalog, "{}\n")
            self._write(paths.core / "commands" / "eap.cmd", "anterior\n")
            self._write(paths.core / "core_tools.json", "{}\n")
            self._write(paths.core / "release.json", "{}\n")
            self._write(
                paths.core / "version.json",
                json.dumps(
                    {"schemaVersion": 1, "name": "EAP", "version": "0.19.0"}
                ),
            )
            self._write(paths.core / "tools" / "keep.txt", "tool\n")
            self._write(paths.data / "keep.txt", "datos\n")
            self._write(paths.components / "keep.txt", "component\n")
            source, release = self._release_archive(root)

            class Api:
                @staticmethod
                def latest_release() -> GitHubRelease:
                    return release

            class Http:
                @staticmethod
                def download(
                    url: str,
                    destination: Path,
                    progress: Any = None,
                    maximum_bytes: int | None = None,
                ) -> tuple[str, int]:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    return url, destination.stat().st_size

            updater = EapReleaseUpdater(paths, Http(), Api())
            update = updater.check("0.19.0")
            self.assertTrue(update.update_available)
            result = updater.install(update)

            self.assertEqual("0.20.0", result.version)
            self.assertEqual("EAP actualizado\n", (root / "README.md").read_text())
            self.assertFalse(
                (paths.core / "app" / "eap" / "obsolete.py").exists()
            )
            self.assertTrue(
                (paths.core / "app" / "eap" / "new_module.py").is_file()
            )
            self.assertEqual(
                "tool\n",
                (paths.core / "tools" / "keep.txt").read_text(),
            )
            self.assertEqual("datos\n", (paths.data / "keep.txt").read_text())
            self.assertEqual(
                "component\n", (paths.components / "keep.txt").read_text()
            )

    def test_update_check_does_not_require_an_asset_when_not_newer(self) -> None:
        release = GitHubRelease(
            id=1,
            tag_name="v0.19.0",
            name="EAP v0.19.0",
            html_url="https://example.test/release",
            published_at=None,
            assets=(),
        )
        api = SimpleNamespace(latest_release=lambda: release)
        updater = EapReleaseUpdater(
            SimpleNamespace(), SimpleNamespace(), api
        )

        result = updater.check("0.20.0")

        self.assertFalse(result.update_available)
        self.assertIsNone(result.asset)

    def test_update_rolls_back_every_managed_path_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            payload = root / "payload"
            transaction = root / "transaction"
            self._write(root / "README.md", "README anterior\n")
            self._write(root / "eap.cmd", "EAP anterior\n")
            self._write(payload / "README.md", "README nuevo\n")
            self._write(payload / "eap.cmd", "EAP nuevo\n")
            updater = EapReleaseUpdater(
                paths, SimpleNamespace(), SimpleNamespace()
            )
            original_replace = Path.replace

            def failing_replace(source: Path, target: Path) -> Path:
                if source == payload / "eap.cmd":
                    raise OSError("fallo forzado")
                return original_replace(source, target)

            with patch.object(Path, "replace", new=failing_replace):
                with self.assertRaises(TransactionError):
                    updater._commit(
                        payload,
                        (
                            PurePosixPath("README.md"),
                            PurePosixPath("eap.cmd"),
                        ),
                        transaction,
                    )

            self.assertEqual(
                "README anterior\n", (root / "README.md").read_text()
            )
            self.assertEqual("EAP anterior\n", (root / "eap.cmd").read_text())

    def test_public_update_is_disabled_in_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            (root / ".git").mkdir()
            _, release = self._release_archive(root)

            class Api:
                @staticmethod
                def latest_release() -> GitHubRelease:
                    return release

            updater = EapReleaseUpdater(paths, SimpleNamespace(), Api())
            with self.assertRaisesRegex(ValidationError, "checkout Git"):
                updater.install(updater.check("0.19.0"))

    def test_release_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", "no")
            with self.assertRaisesRegex(IntegrityError, "Ruta no válida"):
                EapReleaseUpdater._extract_release(
                    archive, root / "extracted"
                )

    def test_release_zip_rejects_windows_device_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad-device.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("core/CON.txt", "no")
            with self.assertRaisesRegex(
                IntegrityError, "no permitido en Windows"
            ):
                EapReleaseUpdater._extract_release(
                    archive, root / "extracted"
                )

    @unittest.skipUnless(shutil.which("git"), "Git no está disponible")
    def test_release_asset_matches_git_and_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            version = "0.20.0"
            manifest = {
                "schemaVersion": 1,
                "repository": "danielgube/eap",
                "assetName": ASSET_TEMPLATE,
                "managedPaths": self._MANAGED,
            }
            files = {
                ".gitignore": "/data/\n/temp/\n/exports/\n",
                "README.md": "EAP\n",
                "config.properties.example": "profile.default=default\n",
                "docs/images/catalogo-components.png": "PNG de prueba\n",
                "eap.cmd": "@echo off\r\n",
                "core/app/eap/__init__.py": f'__version__ = "{version}"\n',
                "core/bootstrap.ps1": "# bootstrap\n",
                "core/catalog/catalog.json": "{}\n",
                "core/commands/eap.cmd": "@echo off\r\n",
                "core/core_tools.json": "{}\n",
                "core/release.json": json.dumps(manifest),
                "core/version.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "name": "EAP",
                        "version": version,
                    }
                ),
            }
            for name, content in files.items():
                self._write(root / name, content)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", *files], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=EAP Test",
                    "-c",
                    "user.email=eap@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "test release",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "tag", f"v{version}"], cwd=root, check=True
            )
            tagged_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self._write(root / "post-release.txt", "cambio posterior\n")
            subprocess.run(
                ["git", "add", "post-release.txt"], cwd=root, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=EAP Test",
                    "-c",
                    "user.email=eap@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "cambio posterior a la release",
                ],
                cwd=root,
                check=True,
            )
            publisher = EapReleasePublisher(paths, 10, "EAP/Test")
            repository = GitRepository(root)
            current_head = repository.run("rev-parse", "HEAD")

            self.assertEqual(
                tagged_head,
                publisher._tagged_release_commit(
                    repository, f"v{version}", current_head
                ),
            )

            archive = publisher._build_archive(
                repository, f"v{version}", version
            )
            first_digest = sha256_file(archive)
            archive = publisher._build_archive(
                repository, f"v{version}", version
            )

            self.assertEqual(first_digest, sha256_file(archive))
            with zipfile.ZipFile(archive) as package:
                names = {item.filename for item in package.infolist()}
            self.assertEqual(
                set(files)
                - {
                    ".gitignore",
                    "docs/images/catalogo-components.png",
                },
                names,
            )


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell.exe"),
    "Windows PowerShell no esta disponible",
)
class BootstrapTests(unittest.TestCase):
    _ICON = (
        "{0caa0dad-35be-5f56-a8ff-afceeeaa6101}.scale-100.png"
    )

    def _fixture(self, root: Path) -> tuple[Path, Path]:
        core = root / "core"
        core.mkdir(parents=True)
        (root / "config.properties.example").write_text(
            "profile.default=default\n"
            "components.repository.test=https://example.test/components.json\n",
            encoding="utf-8",
        )
        bootstrap = core / "bootstrap.ps1"
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "bootstrap.ps1",
            bootstrap,
        )
        archive = (
            root
            / "temp"
            / "core-bootstrap"
            / "downloads"
            / "path-tool"
            / "1.0"
            / "path-tool.zip"
        )
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("package/tool.exe", b"test tool")
            package.writestr(
                f"package/ProfileIcons/{self._ICON}",
                b"test icon",
            )
        manifest = core / "core_tools.json"
        atomic_write_json(
            manifest,
            {
                "schemaVersion": 1,
                "tools": [
                    {
                        "id": "path-tool",
                        "displayName": "Herramienta de prueba",
                        "directory": "tools/path-tool",
                        "executables": ["tool.exe"],
                        "publishToEnvironmentPath": False,
                        "version": "1.0",
                        "bootstrap": {
                            "requiredFiles": ["tool.exe"],
                            "artifacts": [
                                {
                                    "fileName": "path-tool.zip",
                                    "url": (
                                        "https://example.invalid/"
                                        "path-tool.zip"
                                    ),
                                    "sha256": sha256_file(archive),
                                    "install": {
                                        "type": "zip",
                                        "destination": ".",
                                        "source": "package",
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )
        return bootstrap, manifest

    @staticmethod
    def _run(
        root: Path,
        bootstrap: Path,
        manifest: Path,
        answer: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(shutil.which("powershell.exe")),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(bootstrap),
                "-ManifestPath",
                str(manifest),
            ],
            cwd=root,
            input=answer,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def test_first_bootstrap_confirms_and_extracts_long_zip_paths(
        self,
    ) -> None:
        test_temp = Path(__file__).resolve().parents[2] / "temp"
        test_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as temporary:
            base = Path(temporary)
            root = next(
                candidate
                for length in range(20, 140)
                for candidate in [base / ("release-" + "x" * length)]
                if (
                    len(
                        str(
                            candidate
                            / "temp"
                            / "core-bootstrap"
                            / "staging"
                            / ("path-tool." + "0" * 32)
                            / "extract"
                            / "package"
                            / "ProfileIcons"
                            / self._ICON
                        )
                    )
                    > 270
                    and len(str(candidate)) < 180
                )
            )
            root.mkdir()
            bootstrap, manifest = self._fixture(root)

            completed = self._run(root, bootstrap, manifest, "s\n")

            output = completed.stdout + completed.stderr
            self.assertEqual(0, completed.returncode, output)
            self.assertIn(
                "Bienvenido a Environments Applications Portable (EAP)",
                output,
            )
            self.assertIn("Herramienta de prueba 1.0", output)
            target = root / "core" / "tools" / "path-tool"
            self.assertTrue((target / "tool.exe").is_file())
            self.assertTrue(
                (target / "ProfileIcons" / self._ICON).is_file()
            )
            config = root / "config.properties"
            self.assertEqual(
                (root / "config.properties.example").read_text(encoding="utf-8"),
                config.read_text(encoding="utf-8"),
            )

            config.write_text("profile.default=custom\n", encoding="utf-8")
            repeated = self._run(root, bootstrap, manifest, "")
            repeated_output = repeated.stdout + repeated.stderr
            self.assertEqual(0, repeated.returncode, repeated_output)
            self.assertNotIn("Bienvenido", repeated_output)
            self.assertEqual(
                "profile.default=custom\n", config.read_text(encoding="utf-8")
            )

    def test_first_bootstrap_can_be_cancelled(self) -> None:
        test_temp = Path(__file__).resolve().parents[2] / "temp"
        test_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as temporary:
            root = Path(temporary)
            bootstrap, manifest = self._fixture(root)

            completed = self._run(root, bootstrap, manifest, "n\n")

            output = completed.stdout + completed.stderr
            self.assertEqual(2, completed.returncode, output)
            self.assertIn("configuracion inicial cancelada", output)
            self.assertFalse(
                (root / "core" / "tools" / "path-tool").exists()
            )
            self.assertFalse((root / "config.properties").exists())


class ComponentRepositoryTests(unittest.TestCase):
    _REVISION = "a" * 40

    @staticmethod
    def _copy_bundled_catalog(paths: EapPaths) -> None:
        paths.catalog.parent.mkdir(parents=True)
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "catalog" / "catalog.json",
            paths.catalog,
        )

    @staticmethod
    def _bruno_manifest() -> str:
        path = (
            Path(__file__).resolve().parent / "fixtures" / "bruno.json"
        )
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _catalog(manifest: str = "components/bruno.json") -> str:
        return json.dumps(
            {
                "schemaVersion": 1,
                "catalogVersion": "1.0.0",
                "components": [
                    {"id": "bruno", "manifest": manifest}
                ],
            }
        )

    @classmethod
    def _client(cls, catalog: str | None = None) -> Any:
        catalog_text = catalog or cls._catalog()
        manifest_text = cls._bruno_manifest()

        class Client:
            @staticmethod
            def get_json(url: str, maximum_bytes: int = 0) -> Any:
                return {"commit": {"sha": cls._REVISION}}

            @staticmethod
            def get_text(url: str, maximum_bytes: int = 0) -> str:
                if url.endswith("/catalog.json"):
                    return catalog_text
                if url.endswith("/components/bruno.json"):
                    return manifest_text
                raise AssertionError(f"URL inesperada: {url}")

        return Client()

    def test_refreshes_pinned_catalog_and_reuses_it_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            self._copy_bundled_catalog(paths)
            settings = Settings(
                {
                    "components.repository.official": (
                        "https://github.com/example/eap-components"
                    )
                }
            )
            manager = ComponentRepositoryManager(
                paths, settings, self._client()
            )

            catalog = manager.refresh()
            component = catalog.component("bruno")

            self.assertEqual("official", component.source_id)
            self.assertEqual(self._REVISION, component.source.revision)
            self.assertTrue(component.manifest_path.is_file())
            self.assertEqual(
                self._REVISION,
                manager.cached_sources()[0]["revision"],
            )

            class OfflineClient:
                @staticmethod
                def get_json(*args: Any, **kwargs: Any) -> Any:
                    raise AssertionError("No debe consultar la red")

                @staticmethod
                def get_text(*args: Any, **kwargs: Any) -> str:
                    raise AssertionError("No debe consultar la red")

            offline = ComponentRepositoryManager(
                paths, settings, OfflineClient()
            ).load()
            self.assertEqual("official", offline.component("bruno").source_id)

            changed_settings = Settings(
                {
                    "components.repository.official": (
                        "https://github.com/example/other-components"
                    )
                }
            )
            changed = ComponentRepositoryManager(
                paths, changed_settings, OfflineClient()
            )
            with self.assertRaisesRegex(ValidationError, "Componente desconocido"):
                changed.load().component("bruno")
            self.assertIsNone(changed.cached_sources()[0]["revision"])

    def test_external_repository_accepts_json_index_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            self._copy_bundled_catalog(paths)
            settings = Settings(
                {
                    "components.repository.community": (
                        "https://github.com/example/community-components"
                    )
                }
            )
            catalog_text = json.dumps(
                {
                    "schemaVersion": 1,
                    "catalogVersion": "1.0.0",
                    "components": [
                        {
                            "id": "bruno",
                            "manifest": "components/bruno.json",
                        }
                    ],
                }
            )
            manifest = json.loads(self._bruno_manifest())
            manifest["providers"][0]["resolver"] = golang_component(
                Path("golang.json")
            ).provider("go-dev")["resolver"]
            manifest_text = json.dumps(manifest)

            class Client:
                @staticmethod
                def get_json(url: str, maximum_bytes: int = 0) -> Any:
                    return {
                        "commit": {
                            "sha": ComponentRepositoryTests._REVISION
                        }
                    }

                @staticmethod
                def get_text(url: str, maximum_bytes: int = 0) -> str:
                    if url.endswith("/catalog.json"):
                        return catalog_text
                    if url.endswith("/components/bruno.json"):
                        return manifest_text
                    raise AssertionError(f"URL inesperada: {url}")

            component = ComponentRepositoryManager(
                paths, settings, Client()
            ).refresh().component("bruno")

            self.assertEqual("community", component.source_id)
            self.assertEqual(
                "json-index",
                component.provider("community")["resolver"]["type"],
            )

    def test_rejects_manifest_traversal_and_cross_repository_collisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            self._copy_bundled_catalog(paths)
            invalid_settings = Settings(
                {
                    "components.repository.invalid": (
                        "https://github.com/example/invalid"
                    )
                }
            )
            invalid = ComponentRepositoryManager(
                paths,
                invalid_settings,
                self._client(self._catalog("../bruno.json")),
            )
            with self.assertRaisesRegex(
                ValidationError, "Ruta de manifiesto no válida"
            ):
                invalid.refresh()

            duplicate_settings = Settings(
                {
                    "components.repository.first": (
                        "https://github.com/example/first"
                    ),
                    "components.repository.second": (
                        "https://github.com/example/second"
                    ),
                }
            )
            duplicate = ComponentRepositoryManager(
                paths, duplicate_settings, self._client()
            )
            with self.assertRaisesRegex(
                ValidationError, "publicado por dos repositorios"
            ):
                duplicate.refresh()
            self.assertFalse(
                (
                    paths.data
                    / "component-catalogs"
                    / "first"
                    / "active.json"
                ).exists()
            )
            self.assertFalse(
                (
                    paths.data
                    / "component-catalogs"
                    / "second"
                    / "active.json"
                ).exists()
            )

    def test_lock_records_repository_manifest_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            self._copy_bundled_catalog(paths)
            settings = Settings(
                {
                    "components.repository.official": (
                        "https://github.com/example/eap-components"
                    )
                }
            )
            component = ComponentRepositoryManager(
                paths, settings, self._client()
            ).refresh().component("bruno")
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = paths.components / "bruno" / "community" / "4.0.0"
            install_path.mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="bruno",
                component_id="bruno-community",
                provider="community",
                provider_name="Bruno Community",
                track=4,
                version="4.0.0",
                url="https://example.test/bruno.zip",
                file_name="bruno.zip",
                sha256="b" * 64,
                size=1,
                metadata_url="https://example.test/release.json",
            )

            store.publish_component(
                "default",
                artifact,
                install_path,
                sha256_file(component.manifest_path),
                manifest_source=component.manifest_source(),
            )

            locked = store.read_lock("default")["components"][0]
            self.assertEqual("official", locked["manifestSource"]["id"])
            self.assertEqual(
                self._REVISION,
                locked["manifestSource"]["revision"],
            )
            self.assertEqual(
                "components/bruno.json",
                locked["manifestSource"]["manifest"],
            )

    def test_repository_property_can_be_added_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.properties"
            path.write_text("profile.default=default\n", encoding="utf-8")

            update_component_repository_property(
                path,
                "official",
                "https://github.com/example/eap-components",
            )
            self.assertIn(
                "components.repository.official=https://github.com/"
                "example/eap-components",
                path.read_text(encoding="utf-8"),
            )

            update_component_repository_property(path, "official", None)
            self.assertNotIn(
                "components.repository.official",
                path.read_text(encoding="utf-8"),
            )


class SettingsTests(unittest.TestCase):
    def test_repository_sources_are_declared_only_in_config(self) -> None:
        self.assertFalse(
            any(".repository." in key for key in DEFAULTS)
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.properties"
            path.write_text(
                "components.repository.one=https://example.test/components.json\n"
                "components.repository.two=https://example.test/more.json\n"
                "pocketools.repository.one=https://example.test/pocketools.json\n",
                encoding="utf-8",
            )
            settings = Settings.load(path)
            self.assertEqual(
                "https://example.test/more.json",
                settings.get("components.repository.two"),
            )
            self.assertEqual(
                "https://example.test/pocketools.json",
                settings.get("pocketools.repository.one"),
            )

    def test_profile_default_and_environment_alias_are_bidirectional(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.properties"
            path.write_text(
                "environment.default=legacy\n", encoding="utf-8"
            )
            old_settings = Settings.load(path)
            self.assertEqual("legacy", old_settings.get("profile.default"))

            path.write_text("profile.default=modern\n", encoding="utf-8")
            new_settings = Settings.load(path)
            self.assertEqual(
                "modern", new_settings.get("environment.default")
            )

    def test_text_viewer_defaults_to_notepad(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.load(
                Path(temporary) / "config.properties"
            )
        self.assertEqual(
            "notepad.exe",
            settings.get("textViewer.executable"),
        )


class ProxyTests(unittest.TestCase):
    def test_applies_standard_proxy_variables_and_redacts_credentials(
        self,
    ) -> None:
        properties = {
            "http_proxy": "http://domain%5Cuser:secret@proxy.example:8080",
            "https_proxy": "http://proxy.example:8080",
            "no_proxy": "localhost,127.0.0.1,.internal.example",
        }
        environment = {"HTTP_PROXY": "http://old.example:3128"}

        configured = apply_proxy_environment(environment, properties)
        configuration = ProxyConfiguration.from_properties(properties)
        status = configuration.status()

        self.assertEqual(properties["http_proxy"], configured["http_proxy"])
        self.assertEqual(environment["http_proxy"], environment["HTTP_PROXY"])
        self.assertEqual(
            properties["https_proxy"], environment["HTTPS_PROXY"]
        )
        self.assertEqual(properties["no_proxy"], environment["NO_PROXY"])
        self.assertNotIn("secret", str(status))
        self.assertIn("***:***@proxy.example:8080", str(status))

    def test_empty_proxy_property_removes_inherited_value(self) -> None:
        environment = {
            "http_proxy": "http://old.example:3128",
            "HTTP_PROXY": "http://other.example:3128",
        }

        apply_proxy_environment(environment, {"http_proxy": ""})

        self.assertNotIn("http_proxy", environment)
        self.assertNotIn("HTTP_PROXY", environment)

    def test_authentication_requires_proxy_and_portal_url(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "requiere configurar http_proxy"
        ):
            ProxyConfiguration.from_properties(
                {"enable_proxy_authentication": "true"}
            )
        with self.assertRaisesRegex(
            ValidationError, "requiere proxy_authentication_url"
        ):
            ProxyConfiguration.from_properties(
                {
                    "http_proxy": "http://proxy.example:8080",
                    "enable_proxy_authentication": "true",
                }
            )

    def test_post_authentication_replays_hidden_form_fields_securely(
        self,
    ) -> None:
        configuration = ProxyConfiguration.from_properties(
            {
                "http_proxy": "http://proxy.example:8080",
                "https_proxy": "http://proxy.example:8080",
                "enable_proxy_authentication": "true",
                "proxy_authentication_type": "post",
                "proxy_authentication_url": "https://portal.example/start",
                "proxy_authentication_check_url": (
                    "https://check.example/connect"
                ),
                "proxy_authentication_form_field.4Tmthd": "0",
            }
        )
        login_form = """
            <html><form action="https://portal.example/login" method="post">
              <input type="hidden" name="magic" value="token-123">
              <input type="hidden" name="4Tredir" value="target-value">
              <input type="text" name="username">
              <input type="password" name="password">
            </form></html>
        """
        calls: list[tuple[str, str, bytes | None]] = []
        check_count = 0

        def transport(
            method: str,
            url: str,
            data: bytes | None,
            headers: dict[str, str],
        ) -> SimpleNamespace:
            nonlocal check_count
            calls.append((method, url, data))
            if url == "https://check.example/connect":
                check_count += 1
                body = login_form if check_count == 1 else "connected"
                return SimpleNamespace(status=200, url=url, body=body)
            if method == "GET":
                return SimpleNamespace(status=200, url=url, body=login_form)
            return SimpleNamespace(status=204, url=url, body="")

        messages: list[str] = []
        with patch.dict(os.environ, {}, clear=True):
            authenticator = ProxyAuthenticator(
                configuration,
                user_agent="EAP/test",
                status=messages.append,
                username_reader=lambda prompt: "domain\\alice",
                password_reader=lambda prompt: "secret & value",
                transport=transport,
            )
            result = authenticator.ensure_authenticated()
            marker = os.environ.get("EAP_PROXY_AUTHENTICATED")

        post_call = next(call for call in calls if call[0] == "POST")
        fields = urllib.parse.parse_qs(
            (post_call[2] or b"").decode("utf-8")
        )
        self.assertTrue(result.authenticated)
        self.assertEqual("authenticated", result.state)
        self.assertEqual(configuration.session_token, marker)
        self.assertEqual(["token-123"], fields["magic"])
        self.assertEqual(["target-value"], fields["4Tredir"])
        self.assertEqual(["0"], fields["4Tmthd"])
        self.assertEqual(["domain\\alice"], fields["username"])
        self.assertEqual(["secret & value"], fields["password"])
        self.assertNotIn("secret & value", " ".join(messages))

    def test_browser_authentication_opens_portal_and_checks_connection(
        self,
    ) -> None:
        configuration = ProxyConfiguration.from_properties(
            {
                "http_proxy": "http://proxy.example:8080",
                "enable_proxy_authentication": "true",
                "proxy_authentication_type": "browser",
                "proxy_authentication_url": "https://portal.example/login",
                "proxy_authentication_check_url": "https://check.example/",
            }
        )
        responses = iter(
            (
                SimpleNamespace(
                    status=200,
                    url="https://portal.example/login",
                    body='<form><input type="password" name="password"></form>',
                ),
                SimpleNamespace(
                    status=204,
                    url="https://check.example/",
                    body="",
                ),
            )
        )
        opened: list[str] = []
        with patch.dict(os.environ, {}, clear=True):
            result = ProxyAuthenticator(
                configuration,
                user_agent="EAP/test",
                browser_open=lambda url: opened.append(url) or True,
                pause_reader=lambda prompt: "",
                transport=lambda method, url, data, headers: next(responses),
            ).ensure_authenticated()

        self.assertTrue(result.authenticated)
        self.assertEqual(["https://portal.example/login"], opened)


class ResolverTests(unittest.TestCase):
    def test_html_links_follows_github_fragment_and_excludes_preview(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_url = "https://github.example/microsoft/terminal/releases/"
            fragment_url = (
                "https://github.example/microsoft/terminal/releases/"
                "expanded_assets/v1.24.11911.0"
            )
            component = html_links_component(
                Path(temporary) / "vscode.json", index_url=index_url
            )
            selected_name = (
                "Microsoft.WindowsTerminal_1.24.11911.0_x64.zip"
            )
            selected_href = (
                "/microsoft/terminal/releases/download/v1.24.11911.0/"
                f"{selected_name}"
            )
            client = FakeWebHttpClient(
                {
                    index_url: (
                        '<include-fragment src="expanded_assets/'
                        'v1.24.11911.0"></include-fragment>'
                    ),
                    fragment_url: (
                        '<a href="/microsoft/terminal/releases/download/'
                        'v1.25.1912.0/'
                        'Microsoft.WindowsTerminalPreview_1.25.1912.0_x64.zip">'
                        "Microsoft.WindowsTerminalPreview_1.25.1912.0_x64.zip"
                        "</a>"
                        '<a href="/microsoft/terminal/releases/download/'
                        'v1.23.10353.0/'
                        'Microsoft.WindowsTerminal_1.23.10353.0_x64.zip">'
                        "old</a>"
                        f'<a href="{selected_href}">\n'
                        f'  <span class="text-bold">{selected_name}</span>\n'
                        "</a>"
                        f'<a href="{selected_href}">{selected_name}</a>'
                        '<a href="/microsoft/terminal/releases/download/'
                        'v1.24.11911.0/'
                        'Microsoft.WindowsTerminal_1.24.11911.0_arm64.zip">'
                        "arm64</a>"
                    ),
                }
            )

            artifact = resolve_component(
                component, "microsoft", 1, client
            )

            self.assertEqual("1.24.11911.0", artifact.version)
            self.assertEqual(selected_name, artifact.file_name)
            self.assertEqual(
                "https://github.example" + selected_href,
                artifact.url,
            )
            self.assertEqual(index_url, artifact.metadata_url)
            self.assertIsNone(artifact.sha256)
            self.assertIsNone(artifact.sha512)
            self.assertIsNone(artifact.size)
            self.assertEqual("unavailable", artifact.checksum_origin)
            self.assertTrue(artifact.allow_http)
            serialized = artifact.as_json()
            self.assertEqual("unavailable", serialized["checksumOrigin"])
            json.dumps(serialized)
            self.assertEqual(
                [(index_url, True), (fragment_url, True)],
                client.text_calls,
            )

    def test_html_links_matches_normalized_text_and_absolute_url_basename(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = html_links_component(
                Path(temporary) / "vscode.json"
            )
            resolver = component.provider("microsoft")["resolver"]
            resolver["linkPattern"] = (
                r"^Tool_(?P<version>\d+\.\d+\.\d+)_x64\.zip$"
            )
            resolver.pop("excludePatterns")
            index_url = str(resolver["indexUrl"])

            cases = (
                (
                    (
                        '<a href="artifacts/download.zip">\n'
                        "  <span>Tool_1.2.3_x64.zip</span>\n"
                        "</a>"
                    ),
                    "1.2.3",
                    "https://downloads.example/releases/artifacts/download.zip",
                    "download.zip",
                ),
                (
                    '<a href="https://cdn.example/files/'
                    'Tool_1.2.4_x64.zip">Download</a>',
                    "1.2.4",
                    "https://cdn.example/files/Tool_1.2.4_x64.zip",
                    "Tool_1.2.4_x64.zip",
                ),
            )
            for html, version, expected_url, file_name in cases:
                with self.subTest(version=version):
                    artifact = resolve_component(
                        component,
                        "microsoft",
                        1,
                        FakeWebHttpClient({index_url: html}),
                    )
                    self.assertEqual(version, artifact.version)
                    self.assertEqual(expected_url, artifact.url)
                    self.assertEqual(file_name, artifact.file_name)

    def test_html_links_ignores_failed_optional_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_url = "https://forge.example/releases/"
            failed_fragment = "https://forge.example/releases/metadata"
            asset_fragment = "https://forge.example/releases/assets"
            component = html_links_component(
                Path(temporary) / "vscode.json", index_url=index_url
            )
            client = FakeWebHttpClient(
                {
                    index_url: (
                        '<include-fragment src="metadata"></include-fragment>'
                        '<include-fragment src="assets"></include-fragment>'
                    ),
                    failed_fragment: NetworkError("optional fragment failed"),
                    asset_fragment: (
                        '<a href="Microsoft.WindowsTerminal_1.24.11911.0_x64.zip">'
                        "download</a>"
                    ),
                }
            )

            artifact = resolve_component(
                component, "microsoft", 1, client
            )

            self.assertEqual("1.24.11911.0", artifact.version)
            self.assertEqual(
                [
                    (index_url, True),
                    (failed_fragment, True),
                    (asset_fragment, True),
                ],
                client.text_calls,
            )

    def test_html_links_selects_highest_semantic_version_within_track(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = html_links_component(
                Path(temporary) / "vscode.json"
            )
            component.value["tracks"] = [
                {"id": "1.24", "displayName": "Terminal 1.24"}
            ]
            component.value["defaultTrack"] = "1.24"
            resolver = component.provider("microsoft")["resolver"]
            resolver["linkPattern"] = (
                r"^terminal-(?P<version>\d+\.\d+\.\d+)\.zip$"
            )
            resolver.pop("excludePatterns")
            index_url = str(resolver["indexUrl"])
            client = FakeWebHttpClient(
                {
                    index_url: (
                        '<a href="terminal-1.25.1.zip">other track</a>'
                        '<a href="terminal-1.24.11911.zip">latest</a>'
                        '<a href="terminal-1.24.9999.zip">older</a>'
                        '<a href="terminal-1.23.99999.zip">old track</a>'
                    )
                }
            )

            artifact = resolve_component(
                component, "microsoft", "1.24", client
            )

            self.assertEqual("1.24.11911", artifact.version)
            self.assertEqual(
                "https://downloads.example/releases/"
                "terminal-1.24.11911.zip",
                artifact.url,
            )

    def test_html_links_follows_detail_page_and_nested_fragment_at_default_depth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_url = "https://forge.example/tool/releases/"
            detail_url = "https://forge.example/tool/releases/v1.8.2"
            fragment_url = (
                "https://forge.example/tool/releases/assets/v1.8.2"
            )
            component = html_links_component(
                Path(temporary) / "vscode.json", index_url=index_url
            )
            resolver = component.provider("microsoft")["resolver"]
            resolver["linkPattern"] = (
                r"^tool-(?P<version>\d+\.\d+\.\d+)-x64\.zip$"
            )
            resolver["followPattern"] = r"/v\d+\.\d+\.\d+$"
            resolver.pop("excludePatterns")
            client = FakeWebHttpClient(
                {
                    index_url: (
                        '<a href="v1.8.2">v1.8.2</a>'
                        '<a href="ignored">not a release</a>'
                    ),
                    detail_url: (
                        '<include-fragment src="assets/v1.8.2">'
                        "</include-fragment>"
                    ),
                    fragment_url: (
                        '<a href="downloads/tool-1.8.2-x64.zip">'
                        "download</a>"
                    ),
                }
            )

            artifact = resolve_component(
                component, "microsoft", 1, client
            )

            self.assertEqual("1.8.2", artifact.version)
            self.assertEqual(
                "https://forge.example/tool/releases/assets/"
                "downloads/tool-1.8.2-x64.zip",
                artifact.url,
            )
            self.assertEqual(
                [
                    (index_url, True),
                    (detail_url, True),
                    (fragment_url, True),
                ],
                client.text_calls,
            )

    def test_html_links_honors_configured_follow_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_url = "https://forge.example/releases/"
            first_url = "https://forge.example/releases/details-1"
            second_url = "https://forge.example/releases/details-2"
            component = html_links_component(
                Path(temporary) / "vscode.json", index_url=index_url
            )
            resolver = component.provider("microsoft")["resolver"]
            resolver["linkPattern"] = (
                r"^tool-(?P<version>\d+\.\d+\.\d+)\.zip$"
            )
            resolver["followPattern"] = r"/details-\d+$"
            resolver["maxDepth"] = 2
            resolver.pop("excludePatterns")
            responses = {
                index_url: '<a href="details-1">details-1</a>',
                first_url: '<a href="details-2">details-2</a>',
                second_url: '<a href="tool-1.7.3.zip">download</a>',
            }

            artifact = resolve_component(
                component,
                "microsoft",
                1,
                FakeWebHttpClient(responses),
            )
            self.assertEqual("1.7.3", artifact.version)

            resolver["maxDepth"] = 1
            with self.assertRaisesRegex(NetworkError, "ZIP|enlace|versión"):
                resolve_component(
                    component,
                    "microsoft",
                    1,
                    FakeWebHttpClient(responses),
                )

    def test_html_links_allows_http_and_keeps_first_latest_mirror(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_url = "http://intranet.example/releases/"
            component = html_links_component(
                Path(temporary) / "vscode.json", index_url=index_url
            )
            resolver = component.provider("microsoft")["resolver"]
            resolver["linkPattern"] = (
                r"^tool-(?P<version>\d+\.\d+\.\d+)\.zip$"
            )
            resolver.pop("excludePatterns")
            first_url = "http://mirror-a.example/tool-1.6.0.zip"
            second_url = "http://mirror-b.example/tool-1.6.0.zip"
            client = FakeWebHttpClient(
                {
                    index_url: (
                        f'<a href="{first_url}">first</a>'
                        f'<a href="{first_url}">duplicate</a>'
                        f'<a href="{second_url}">second</a>'
                        '<a href="tool-1.5.9.zip">older</a>'
                    )
                }
            )

            artifact = resolve_component(
                component, "microsoft", 1, client
            )

            self.assertEqual("1.6.0", artifact.version)
            self.assertEqual(first_url, artifact.url)
            self.assertTrue(artifact.allow_http)
            self.assertEqual([(index_url, True)], client.text_calls)

    def test_resolves_bruno_portable_zip_from_stable_github_release(
        self,
    ) -> None:
        component = bruno_component()
        response = [
            {
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "bruno_5.0.0_x64_win.zip",
                        "browser_download_url": (
                            "https://example.test/bruno-5.0.0.zip"
                        ),
                        "digest": "sha256:" + ("a" * 64),
                        "size": 300,
                    },
                    {
                        "name": "bruno_4.1.0_x64_win.zip",
                        "browser_download_url": (
                            "https://example.test/bruno-4.1.0.zip"
                        ),
                        "digest": "sha256:" + ("b" * 64),
                        "size": 184_280_060,
                    },
                ],
            }
        ]

        artifact = resolve_component(
            component, "community", 4, FakeHttpClient(response)
        )

        self.assertEqual("4.1.0", artifact.version)
        self.assertEqual("bruno_4.1.0_x64_win.zip", artifact.file_name)
        self.assertEqual("b" * 64, artifact.sha256)
        self.assertEqual(184_280_060, artifact.size)
        self.assertEqual("bruno-community", artifact.component_id)

    def test_resolves_eclipse_enterprise_zip_with_official_sha512(self) -> None:
        component = eclipse_component()
        base_url = (
            "https://download.eclipse.org/technology/epp/downloads/"
            "release/2026-06/R/"
        )
        file_name = "eclipse-jee-2026-06-R-win32-x86_64.zip"
        checksum_url = f"{base_url}{file_name}.sha512"
        client = FakeTextHttpClient(
            {checksum_url: f"{'b' * 128} *{file_name}\n"}
        )

        artifact = resolve_component(
            component, "enterprise-java", "2026-06", client
        )

        self.assertEqual("2026-06", artifact.version)
        self.assertEqual(file_name, artifact.file_name)
        self.assertEqual("sha512", artifact.checksum_algorithm)
        self.assertEqual("b" * 128, artifact.sha512)
        self.assertEqual(
            "eclipse-enterprise-java", artifact.component_id
        )
        self.assertEqual(base_url, artifact.metadata_url)

    def test_resolves_intellij_zip_from_official_jetbrains_api(self) -> None:
        component = intellij_component()
        checksum_url = (
            "https://download.jetbrains.test/idea/"
            "idea-2026.2.1.win.zip.sha256"
        )
        response = {
            "IIU": [
                {
                    "type": "release",
                    "version": "2026.2.1",
                    "downloads": {
                        "windowsZip": {
                            "link": (
                                "https://download.jetbrains.test/idea/"
                                "idea-2026.2.1.win.zip"
                            ),
                            "size": 1_614_981_679,
                            "checksumLink": checksum_url,
                        }
                    },
                }
            ]
        }
        artifact = resolve_component(
            component,
            "jetbrains",
            "2026.2",
            FakeNodeHttpClient(response, {checksum_url: ("a" * 64) + "\n"}),
        )
        self.assertEqual("2026.2.1", artifact.version)
        self.assertEqual("idea-2026.2.1.win.zip", artifact.file_name)
        self.assertEqual("a" * 64, artifact.sha256)
        self.assertEqual(1_614_981_679, artifact.size)
        self.assertEqual("intellij-idea-jetbrains", artifact.component_id)
        self.assertIn("code=IIU", artifact.metadata_url)
        self.assertIn("majorVersion=2026.2", artifact.metadata_url)

    def test_resolves_vscode_zip_from_official_update_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = vscode_component(
                Path(temporary) / "vscode.json"
            )
            response = {
                "url": (
                    "https://download.example/stable/commit/"
                    "VSCode-win32-x64-1.134.0.zip"
                ),
                "name": "1.134.0",
                "productVersion": "1.134.0",
                "sha256hash": "f" * 64,
            }
            artifact = resolve_component(
                component, "microsoft", 1, FakeHttpClient(response)
            )
            self.assertEqual("1.134.0", artifact.version)
            self.assertEqual(
                "VSCode-win32-x64-1.134.0.zip", artifact.file_name
            )
            self.assertEqual("f" * 64, artifact.sha256)
            self.assertEqual("vscode-microsoft", artifact.component_id)

    def test_resolves_temurin_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = java_component(Path(temporary) / "java.json")
            response = [
                {
                    "release_name": "jdk-21.0.12+8",
                    "binary": {
                        "package": {
                            "name": "temurin.zip",
                            "link": "https://example.test/temurin.zip",
                            "checksum": "a" * 64,
                            "size": 123,
                        }
                    },
                }
            ]
            artifact = resolve_component(
                component, "temurin", 21, FakeHttpClient(response)
            )
            self.assertEqual("21.0.12+8", artifact.version)
            self.assertEqual("a" * 64, artifact.sha256)
            self.assertEqual("java-temurin", artifact.component_id)

    def test_resolves_corretto_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = java_component(Path(temporary) / "java.json")
            response = {
                "windows": {
                    "x64": {
                        "jdk": {
                            "21": {
                                "zip": {
                                    "resource": (
                                        "/downloads/resources/21.0.12.9.1/"
                                        "amazon-corretto-21.0.12.9.1-windows-x64-jdk.zip"
                                    ),
                                    "checksum_sha256": "b" * 64,
                                }
                            }
                        }
                    }
                }
            }
            artifact = resolve_component(
                component, "corretto", 21, FakeHttpClient(response)
            )
            self.assertEqual("21.0.12.9.1", artifact.version)
            self.assertEqual("b" * 64, artifact.sha256)
            self.assertEqual("java-corretto", artifact.component_id)

    def test_resolves_latest_stable_apache_maven_with_sha512(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = maven_component(Path(temporary) / "maven.json")
            index_url = "https://downloads.example/maven-3/"
            archive_url = (
                "https://downloads.example/maven-3/3.9.16/binaries/"
                "apache-maven-3.9.16-bin.zip"
            )
            client = FakeTextHttpClient(
                {
                    index_url: (
                        '<a href="3.9.15/">3.9.15/</a>'
                        '<a href="3.9.16/">3.9.16/</a>'
                        '<a href="3.10.0-rc-1/">preview</a>'
                    ),
                    f"{archive_url}.sha512": "d" * 128,
                }
            )
            artifact = resolve_component(
                component, "apache", 3, client
            )
            self.assertEqual("3.9.16", artifact.version)
            self.assertEqual("sha512", artifact.checksum_algorithm)
            self.assertEqual("d" * 128, artifact.checksum)
            self.assertIsNone(artifact.sha256)

            component.provider("apache")["resolver"] = {
                "type": "apache-directory",
                "indexUrl": index_url,
                "downloadBaseUrl": "https://downloads.example/maven-3",
            }
            legacy_artifact = resolve_component(
                component, "apache", 3, client
            )
            self.assertEqual(artifact, legacy_artifact)

    def test_resolves_latest_tomcat_base_zip_with_sha512(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = tomcat_component(
                Path(temporary) / "tomcat.json"
            )
            index_url = (
                "https://downloads.example/tomcat/tomcat-11/"
            )
            archive_url = (
                "https://downloads.example/tomcat/tomcat-11/"
                "v11.0.25/bin/apache-tomcat-11.0.25.zip"
            )
            client = FakeTextHttpClient(
                {
                    index_url: (
                        '<a href="v11.0.24/">v11.0.24/</a>'
                        '<a href="v11.0.25/">v11.0.25/</a>'
                        '<a href="v12.0.0-M1/">preview</a>'
                    ),
                    f"{archive_url}.sha512": (
                        ("e" * 128)
                        + "  apache-tomcat-11.0.25.zip\n"
                    ),
                }
            )

            artifact = resolve_component(
                component, "apache", 11, client
            )

            self.assertEqual("11.0.25", artifact.version)
            self.assertEqual(
                "apache-tomcat-11.0.25.zip", artifact.file_name
            )
            self.assertEqual("sha512", artifact.checksum_algorithm)
            self.assertEqual("e" * 128, artifact.sha512)
            self.assertEqual(archive_url, artifact.url)

    def test_resolves_git_for_windows_mingit_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = git_component(Path(temporary) / "git.json")
            response = {
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "PortableGit-2.55.0.5-64-bit.7z.exe",
                        "browser_download_url": "https://example.test/full.exe",
                        "digest": "sha256:" + ("a" * 64),
                        "size": 100,
                    },
                    {
                        "name": "MinGit-2.55.0.5-64-bit.zip",
                        "browser_download_url": (
                            "https://example.test/MinGit.zip"
                        ),
                        "digest": "sha256:" + ("e" * 64),
                        "size": 200,
                    },
                ],
            }
            artifact = resolve_component(
                component,
                "git-for-windows",
                2,
                FakeHttpClient(response),
            )
            self.assertEqual("2.55.0.5", artifact.version)
            self.assertEqual("MinGit-2.55.0.5-64-bit.zip", artifact.file_name)
            self.assertEqual("e" * 64, artifact.sha256)
            self.assertEqual(200, artifact.size)

    def test_resolves_latest_nodejs_zip_with_official_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = nodejs_component(Path(temporary) / "nodejs.json")
            checksum_url = (
                "https://node.example/dist/v24.19.0/SHASUMS256.txt"
            )
            client = FakeNodeHttpClient(
                [
                    {
                        "version": "v26.7.0",
                        "files": ["win-x64-zip"],
                        "lts": False,
                    },
                    {
                        "version": "v24.18.1",
                        "files": ["win-x64-zip"],
                        "lts": "Krypton",
                    },
                    {
                        "version": "v24.19.0",
                        "files": ["win-x64-zip"],
                        "lts": "Krypton",
                    },
                ],
                {
                    checksum_url: (
                        ("a" * 64)
                        + "  node-v24.19.0-win-x64.7z\n"
                        + ("b" * 64)
                        + "  node-v24.19.0-win-x64.zip\n"
                    )
                },
            )
            artifact = resolve_component(component, "nodejs", 24, client)
            self.assertEqual("24.19.0", artifact.version)
            self.assertEqual("node-v24.19.0-win-x64.zip", artifact.file_name)
            self.assertEqual("b" * 64, artifact.sha256)
            self.assertEqual(24, artifact.track)

    def test_resolves_latest_stable_golang_zip_for_selected_minor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = golang_component(Path(temporary) / "golang.json")
            response = [
                {
                    "version": "go1.28rc1",
                    "stable": False,
                    "files": [],
                },
                {
                    "version": "go1.27.1",
                    "stable": True,
                    "files": [
                        {
                            "filename": "go1.27.1.windows-amd64.msi",
                            "os": "windows",
                            "arch": "amd64",
                            "kind": "installer",
                            "sha256": "a" * 64,
                            "size": 100,
                        },
                        {
                            "filename": "go1.27.1.windows-amd64.zip",
                            "os": "windows",
                            "arch": "amd64",
                            "kind": "archive",
                            "sha256": "b" * 64,
                            "size": 78_931_360,
                        },
                    ],
                },
                {
                    "version": "go1.26.8",
                    "stable": True,
                    "files": [],
                },
            ]

            artifact = resolve_component(
                component, "go-dev", "1.27", FakeHttpClient(response)
            )

            self.assertEqual("1.27", artifact.track)
            self.assertEqual("1.27.1", artifact.version)
            self.assertEqual(
                "go1.27.1.windows-amd64.zip", artifact.file_name
            )
            self.assertEqual("b" * 64, artifact.sha256)
            self.assertEqual(78_931_360, artifact.size)
            self.assertEqual(
                "https://go.example/dl/go1.27.1.windows-amd64.zip",
                artifact.url,
            )

            component.provider("go-dev")["resolver"] = {
                "type": "golang-downloads-index",
                "indexUrl": "https://go.example/dl/?mode=json",
                "downloadBaseUrl": "https://go.example/dl",
            }
            legacy_artifact = resolve_component(
                component, "go-dev", "1.27", FakeHttpClient(response)
            )
            self.assertEqual(artifact, legacy_artifact)

    def test_resolves_php_windows_nts_zip_for_selected_minor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = php_component(Path(temporary) / "php.json")
            response = {
                "8.4": {"version": "8.4.25"},
                "8.5": {
                    "version": "8.5.10",
                    "ts-vs17-x64": {
                        "zip": {
                            "path": "php-8.5.10-Win32-vs17-x64.zip",
                            "sha256": "a" * 64,
                        }
                    },
                    "nts-vs17-x64": {
                        "zip": {
                            "path": "php-8.5.10-nts-Win32-vs17-x64.zip",
                            "sha256": "b" * 64,
                        }
                    },
                },
            }

            artifact = resolve_component(
                component,
                "php-windows",
                "8.5",
                FakeHttpClient(response),
            )

            self.assertEqual("8.5", artifact.track)
            self.assertEqual("8.5.10", artifact.version)
            self.assertEqual(
                "php-8.5.10-nts-Win32-vs17-x64.zip",
                artifact.file_name,
            )
            self.assertEqual("b" * 64, artifact.sha256)
            self.assertEqual(
                "https://php.example/releases/"
                "php-8.5.10-nts-Win32-vs17-x64.zip",
                artifact.url,
            )

            component.provider("php-windows")["resolver"] = {
                "type": "php-windows-releases",
                "indexUrl": "https://php.example/releases.json",
                "downloadBaseUrl": "https://php.example/releases",
                "threadSafety": "nts",
                "architecture": "x64",
            }
            legacy_artifact = resolve_component(
                component,
                "php-windows",
                "8.5",
                FakeHttpClient(response),
            )
            self.assertEqual(artifact, legacy_artifact)

    def test_json_index_rejects_ambiguous_or_unsafe_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = golang_component(Path(temporary) / "golang.json")
            release = {
                "version": "go1.27.1",
                "stable": True,
                "files": [
                    {
                        "filename": "go1.27.1.windows-amd64.zip",
                        "os": "windows",
                        "arch": "amd64",
                        "kind": "archive",
                        "sha256": "a" * 64,
                        "size": 100,
                    },
                    {
                        "filename": "go1.27.1.windows-amd64-alt.zip",
                        "os": "windows",
                        "arch": "amd64",
                        "kind": "archive",
                        "sha256": "b" * 64,
                        "size": 100,
                    },
                ],
            }
            with self.assertRaisesRegex(NetworkError, "varios ZIP"):
                resolve_component(
                    component, "go-dev", "1.27", FakeHttpClient([release])
                )

            component.provider("go-dev")["resolver"]["artifacts"][
                "selection"
            ] = "first"
            release["files"][0]["filename"] = "../payload.zip"
            with self.assertRaisesRegex(NetworkError, "no seguro"):
                resolve_component(
                    component, "go-dev", "1.27", FakeHttpClient([release])
                )

    def test_resolves_latest_pythoncore_zip_for_selected_minor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = python_component(Path(temporary) / "python.json")
            response = {
                "versions": [
                    {
                        "company": "PythonCore",
                        "tag": "3.13-64",
                        "sort-version": "3.13.15",
                        "url": "https://python.example/python-3.13.15-amd64.zip",
                        "hash": {"sha256": "a" * 64},
                    },
                    {
                        "company": "PythonEmbed",
                        "tag": "3.14-64",
                        "sort-version": "3.14.7",
                        "url": "https://python.example/python-embed.zip",
                        "hash": {"sha256": "b" * 64},
                    },
                    {
                        "company": "PythonCore",
                        "tag": "3.14-64",
                        "sort-version": "3.14.6",
                        "url": "https://python.example/python-3.14.6-amd64.zip",
                        "hash": {"sha256": "c" * 64},
                    },
                    {
                        "company": "PythonCore",
                        "tag": "3.14-64",
                        "sort-version": "3.14.7",
                        "url": "https://python.example/python-3.14.7-amd64.zip",
                        "hash": {"sha256": "d" * 64},
                    },
                ]
            }
            artifact = resolve_component(
                component, "pythoncore", "3.14", FakeHttpClient(response)
            )
            self.assertEqual("3.14", artifact.track)
            self.assertEqual("3.14.7", artifact.version)
            self.assertEqual("python-3.14.7-amd64.zip", artifact.file_name)
            self.assertEqual("d" * 64, artifact.sha256)

    def test_resolves_latest_dbeaver_release_in_selected_major(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = dbeaver_component(
                Path(temporary) / "dbeaver.json"
            )
            response = [
                {
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": (
                                "dbeaver-ce-26.2.0-windows-x86_64.zip"
                            ),
                            "browser_download_url": (
                                "https://example.test/dbeaver-26.2.0.zip"
                            ),
                            "digest": "sha256:" + ("a" * 64),
                            "size": 300,
                        }
                    ],
                },
                {
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": (
                                "dbeaver-ce-26.1.4-windows-x86_64.zip"
                            ),
                            "browser_download_url": (
                                "https://example.test/dbeaver-26.1.4.zip"
                            ),
                            "digest": "sha256:" + ("b" * 64),
                            "size": 200,
                        },
                        {
                            "name": (
                                "dbeaver-ce-26.1.5-windows-x86_64.zip"
                            ),
                            "browser_download_url": (
                                "https://example.test/dbeaver-26.1.5.zip"
                            ),
                            "digest": "sha256:" + ("c" * 64),
                            "size": 250,
                        },
                    ],
                },
            ]
            artifact = resolve_component(
                component,
                "community",
                26,
                FakeHttpClient(response),
            )
            self.assertEqual("26.2.0", artifact.version)
            self.assertEqual("a" * 64, artifact.sha256)
            self.assertEqual(300, artifact.size)

    def test_resolves_dbeaver_from_official_download_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = dbeaver_component(
                Path(temporary) / "dbeaver.json"
            )
            component.value["providers"][0]["resolver"] = {
                "type": "dbeaver-download-page",
                "downloadPageUrl": "https://dbeaver.example/download/",
                "filesBaseUrl": "https://dbeaver.example/files",
                "assetTemplate": (
                    "dbeaver-ce-{version}-windows-x86_64.zip"
                ),
            }
            file_name = "dbeaver-ce-26.2.0-windows-x86_64.zip"
            client = FakeTextHttpClient(
                {
                    "https://dbeaver.example/download/": (
                        "<h1>Download DBeaver Community 26.2.0</h1>"
                    ),
                    (
                        "https://dbeaver.example/files/26.2.0/checksum/"
                        f"{file_name}.sha256"
                    ): "d" * 64 + "\n",
                }
            )

            artifact = resolve_component(
                component, "community", 26, client
            )

            self.assertEqual("26.2.0", artifact.version)
            self.assertEqual(26, artifact.track)
            self.assertEqual(file_name, artifact.file_name)
            self.assertEqual("d" * 64, artifact.sha256)
            self.assertEqual(
                f"https://dbeaver.example/files/26.2.0/{file_name}",
                artifact.url,
            )


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = EapPaths.from_root(self.root)
        self.paths.ensure_layout()
        self.installer = ComponentInstaller(
            self.paths,
            Settings(dict(DEFAULTS)),
            HttpClient(1),
        )
        seven_zip_root = self.paths.core / "tools" / "7zip"
        seven_zip_root.mkdir(parents=True)
        seven_zip_source = Path(__file__).resolve().parents[1] / "tools" / "7zip"
        for name in ("7z.exe", "7z.dll"):
            shutil.copy2(seven_zip_source / name, seven_zip_root / name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_installer_records_local_sha256_when_source_has_no_checksum(
        self,
    ) -> None:
        component = html_links_component(self.root / "vscode.json")
        source_archive = self.root / "vscode-source.zip"
        with zipfile.ZipFile(
            source_archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as output:
            output.writestr("Code.exe", "portable executable")
            output.writestr("bin/code.cmd", "@echo off\n")
            output.writestr(
                "Code.VisualElementsManifest.xml", "<Application/>"
            )
        expected_sha256 = sha256_file(source_archive)
        client = FakeDownloadHttpClient(source_archive.read_bytes())
        self.installer.client = client
        artifact = ResolvedArtifact(
            family="vscode",
            component_id="vscode-microsoft",
            provider="microsoft",
            provider_name="Microsoft",
            track=1,
            version="1.24.11911.0",
            url=(
                "http://intranet.example/"
                "Microsoft.WindowsTerminal_1.24.11911.0_x64.zip"
            ),
            file_name=(
                "Microsoft.WindowsTerminal_1.24.11911.0_x64.zip"
            ),
            sha256=None,
            sha512=None,
            size=len(client.payload),
            metadata_url="http://intranet.example/releases/",
            checksum_origin="unavailable",
            allow_http=True,
        )

        with patch.object(self.installer, "_check_disk_space"):
            install_path, installed_artifact = self.installer.install(
                component, artifact, process_environment={}
            )

        self.assertEqual(expected_sha256, installed_artifact.sha256)
        self.assertEqual("downloaded", installed_artifact.checksum_origin)
        self.assertTrue(installed_artifact.allow_http)
        self.assertEqual(
            [(artifact.url, True)], client.download_calls
        )
        marker = load_json(install_path / ".eap-install.json")
        self.assertEqual("sha256", marker["checksumAlgorithm"])
        self.assertEqual(expected_sha256, marker["artifactChecksum"])
        self.assertEqual(expected_sha256, marker["artifactSha256"])
        self.assertEqual("downloaded", marker["checksumOrigin"])
        self.assertEqual("downloaded", marker["source"]["checksumOrigin"])
        self.assertTrue(marker["source"]["allowHttp"])

        with patch.object(self.installer, "_check_disk_space"):
            reused_path, reused_artifact = self.installer.install(
                component, artifact, process_environment={}
            )
        self.assertEqual(install_path, reused_path)
        self.assertEqual(expected_sha256, reused_artifact.sha256)
        self.assertEqual("downloaded", reused_artifact.checksum_origin)
        self.assertEqual([(artifact.url, True)], client.download_calls)

    def test_bruno_validates_portable_archive_root(self) -> None:
        component = bruno_component()
        candidate = self.root / "bruno"
        (candidate / "resources").mkdir(parents=True)
        (candidate / "Bruno.exe").touch()
        (candidate / "resources" / "app.asar").touch()
        (candidate / "resources" / "portable.json").touch()
        artifact = ResolvedArtifact(
            family="bruno",
            component_id="bruno-community",
            provider="community",
            provider_name="Bruno Community",
            track=4,
            version="4.1.0",
            url="https://example.test/bruno.zip",
            file_name="bruno_4.1.0_x64_win.zip",
            sha256="a" * 64,
            size=184_280_060,
            metadata_url="https://api.github.test/releases",
        )

        selected = self.installer._select_candidate_root(component, candidate)
        self.installer._validate_payload(
            component, artifact, selected, process_environment=None
        )

        self.assertEqual(candidate, selected)

    def test_rejects_zip_path_traversal(self) -> None:
        archive = self.root / "malicious.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../outside.txt", "blocked")
        destination = self.root / "extract"
        destination.mkdir()
        with self.assertRaises(IntegrityError):
            self.installer._safe_extract_zip(archive, destination)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_extracts_normal_zip(self) -> None:
        archive = self.root / "normal.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("jdk/bin/java.exe", "fixture")
        destination = self.root / "extract"
        destination.mkdir()
        self.installer._safe_extract_zip(archive, destination)
        self.assertEqual(
            "fixture",
            (destination / "jdk" / "bin" / "java.exe").read_text(),
        )

    def test_7zip_failure_does_not_terminate_eap(self) -> None:
        archive = self.root / "blocked.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("blocked.jar", "fixture")
        destination = self.root / "blocked"
        destination.mkdir()

        completed = subprocess.CompletedProcess([], -1, "", "")
        with patch(
            "eap.installer.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(
                IntegrityError, "7-Zip no pudo descomprimir"
            ):
                self.installer._safe_extract_zip(archive, destination)

    def test_failed_extraction_releases_lock_without_walking_staging(self) -> None:
        component = html_links_component(self.root / "fixture.json")
        archive = self.paths.temp / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Code.exe", "fixture")
        artifact = ResolvedArtifact(
            family="vscode",
            component_id="vscode-microsoft",
            provider="microsoft",
            provider_name="Microsoft",
            track=1,
            version="1.0.0",
            url="https://example.test/fixture.zip",
            file_name="fixture.zip",
            sha256="a" * 64,
            size=archive.stat().st_size,
            metadata_url="https://example.test/releases/",
        )

        with (
            patch.object(self.installer, "_check_disk_space"),
            patch.object(
                self.installer,
                "_obtain_archive",
                return_value=(archive, artifact),
            ),
            patch.object(
                self.installer,
                "_safe_extract_zip",
                side_effect=IntegrityError("extractor interrumpido"),
            ),
        ):
            with self.assertRaisesRegex(
                IntegrityError, "extractor interrumpido"
            ):
                self.installer.install(
                    component, artifact, process_environment={}
                )

        lock = self.paths.temp / "locks" / "install-vscode-microsoft-1.lock"
        self.assertFalse(lock.exists())
        [journal_path] = list((self.paths.temp / "transactions").glob("*.json"))
        journal = load_json(journal_path)
        self.assertEqual("failed", journal["state"])
        staging = self.paths.temp / "staging" / journal["transactionId"]
        self.assertTrue(staging.is_dir())

    def test_rejects_file_removed_after_extraction_worker_finishes(self) -> None:
        archive = self.root / "removed.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("removed.jar", "fixture")
        destination = self.root / "removed"
        destination.mkdir()

        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch(
            "eap.installer.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(
                IntegrityError, "Falta un archivo tras la extracción"
            ):
                self.installer._safe_extract_zip(archive, destination)

    def test_component_can_use_a_narrow_explicit_extraction_cap(self) -> None:
        archive = self.root / "bounded.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("payload.bin", "12345")
        destination = self.root / "bounded"
        destination.mkdir()

        with self.assertRaisesRegex(
            IntegrityError, "install.maxExtractBytes"
        ):
            self.installer._safe_extract_zip(
                archive, destination, maximum_bytes=4
            )

    def test_eclipse_validation_requires_its_bundled_jre(self) -> None:
        component = eclipse_component()
        candidate = self.root / "eclipse"
        for relative in component.value["install"]["requiredFiles"]:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        vm_directory = candidate / "plugins" / "bundled-jre" / "jre" / "bin"
        vm_directory.mkdir(parents=True)
        javaw = vm_directory / "javaw.exe"
        javaw.touch()
        (candidate / "eclipse.ini").write_text(
            "-startup\nplugins/launcher.jar\n-vm\n"
            "plugins/bundled-jre/jre/bin\n-vmargs\n-Xmx2g\n",
            encoding="utf-8",
        )
        artifact = ResolvedArtifact(
            family="eclipse",
            component_id="eclipse-java",
            provider="java",
            provider_name="Eclipse IDE for Java Developers",
            track="2026-06",
            version="2026-06",
            url="https://example.test/eclipse.zip",
            file_name="eclipse.zip",
            sha256=None,
            sha512="a" * 128,
            size=None,
            metadata_url="https://example.test/",
        )

        self.installer._validate_payload(
            component, artifact, candidate, process_environment={}
        )
        javaw.unlink()
        with self.assertRaisesRegex(IntegrityError, "JRE incluido"):
            self.installer._validate_payload(
                component, artifact, candidate, process_environment={}
            )

    def test_files_only_validation_does_not_start_gui_application(self) -> None:
        component = dbeaver_component(self.root / "dbeaver.json")
        candidate = self.root / "dbeaver"
        for relative in component.value["install"]["requiredFiles"]:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        artifact = ResolvedArtifact(
            family="dbeaver",
            component_id="dbeaver-community",
            provider="community",
            provider_name="DBeaver Community",
            track="26.1",
            version="26.1.5",
            url="https://example.test/dbeaver.zip",
            file_name="dbeaver.zip",
            sha256="a" * 64,
            size=250,
            metadata_url="https://api.example/releases",
        )
        with patch("eap.installer.subprocess.run") as run:
            self.installer._validate_payload(
                component, artifact, candidate, process_environment={}
            )
        run.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    def test_managed_properties_are_merged_without_losing_user_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            store.create("default")
            component = dbeaver_component(paths.temp / "dbeaver.json")
            component.value["data"]["files"] = [
                {
                    "path": "{{data.component}}/workspace/core.prefs",
                    "displayName": "Preferencias administradas",
                    "role": "configuration",
                    "showInDashboard": False,
                    "mode": "merge-properties",
                    "content": "ui.auto.update.check=false\n",
                }
            ]
            target = (
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "dbeaver"
                / "workspace"
                / "core.prefs"
            )
            target.parent.mkdir(parents=True)
            target.write_text(
                "# user preference\n"
                "ui.auto.update.check=true\n"
                "sample.database.canceled=true\n",
                encoding="utf-8",
            )
            locked = {
                "id": "dbeaver",
                "provider": "community",
                "track": "26.1",
                "version": "26.1.5",
                "installPath": "components/dbeaver/community/26.1.5",
            }

            store.ensure_component_data("default", component, locked)

            content = target.read_text(encoding="utf-8")
            self.assertIn("# user preference", content)
            self.assertIn("sample.database.canceled=true", content)
            self.assertEqual(1, content.count("ui.auto.update.check="))
            self.assertIn("ui.auto.update.check=false", content)

    def test_custom_commands_are_profile_scoped_and_published_in_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            store.create("default")
            commands_root = store.custom_commands_path("default")
            self.assertTrue(commands_root.is_dir())
            for name in (
                "legacy.com",
                "mvndepens.cmd",
                "notes.txt",
                "script.ps1",
                "testqa.bat",
                "testqa.exe",
                "utility.exe",
            ):
                (commands_root / name).write_text(
                    f"contenido de {name}", encoding="utf-8"
                )
            inherited_commands = (
                paths.data
                / "profiles"
                / "previous"
                / "custom-commands"
            )
            inherited_commands.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {
                    "PATH": os.pathsep.join(
                        [
                            str(inherited_commands),
                            r"C:\Windows\System32",
                        ]
                    )
                },
                clear=True,
            ):
                environment = store.build_process_environment(
                    "default", Catalog(paths, {}, {})
                )

            self.assertEqual(
                ["legacy", "mvndepens", "script", "testqa", "utility"],
                store.custom_commands("default"),
            )
            path_entries = environment["PATH"].split(os.pathsep)
            self.assertEqual(str(commands_root.resolve()), path_entries[0])
            self.assertNotIn(str(inherited_commands), path_entries)
            self.assertEqual(
                "contenido de mvndepens.cmd",
                (commands_root / "mvndepens.cmd").read_text(
                    encoding="utf-8"
                ),
            )

    def test_environments_can_share_and_reassign_data_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)

            store.create(
                "java11",
                workspace_id="legacy",
                data_profile_id="developer",
            )
            store.create(
                "java21",
                workspace_id="modern",
                data_profile_id="developer",
            )

            self.assertEqual(
                "developer", store.read_desired("java11")["dataProfile"]
            )
            self.assertEqual(
                "developer", store.read_desired("java21")["dataProfile"]
            )
            shared = paths.data / "profiles" / "developer"
            self.assertEqual(shared, store.ensure_profile("java11"))
            self.assertEqual(shared, store.ensure_profile("java21"))
            self.assertEqual(["developer"], store.list_data_profiles())

            isolated = store.set_data_profile("java21", "java21-private")
            self.assertEqual(
                paths.data / "profiles" / "java21-private", isolated
            )
            self.assertEqual(
                "java21-private",
                store.read_desired("java21")["dataProfile"],
            )
            self.assertEqual(
                ["developer", "java21-private"],
                store.list_data_profiles(),
            )

    def test_profile_duplicate_creates_workspace_and_shares_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            source_files = store.create(
                "java21",
                workspace_id="project",
                data_profile_id="developer",
            )
            desired = store.read_desired("java21")
            desired["components"] = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "updatePolicy": "same-track",
                }
            ]
            lock = store.read_lock("java21")
            lock["components"] = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "version": "21.0.12+8",
                    "installPath": "components/java/temurin/21.0.12+8",
                }
            ]
            atomic_write_json(source_files.desired, desired)
            atomic_write_json(source_files.lock, lock)
            source_files.config.write_text(
                "env.PROJECT_TOKEN=private\n", encoding="utf-8"
            )

            duplicated = store.duplicate("java21", "java11")

            copied_desired = store.read_desired("java11")
            copied_lock = store.read_lock("java11")
            self.assertEqual("java11", copied_desired["id"])
            self.assertEqual("java11", copied_lock["environmentId"])
            self.assertEqual("java11", copied_desired["workspace"])
            self.assertEqual("developer", copied_desired["dataProfile"])
            self.assertTrue((paths.workspaces / "java11").is_dir())
            self.assertEqual(desired["components"], copied_desired["components"])
            self.assertEqual(lock["components"], copied_lock["components"])
            self.assertEqual(
                "env.PROJECT_TOKEN=private\n",
                duplicated.config.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "java11", store.selected(configured_default="java21")
            )

    def test_deleting_profile_preserves_workspace_data_and_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            store.create(
                "keep",
                workspace_id="project",
                data_profile_id="developer",
            )
            store.create(
                "remove",
                workspace_id="project",
                data_profile_id="developer",
            )
            payload = paths.components / "java" / "temurin" / "21"
            payload.mkdir(parents=True)
            workspace_file = paths.workspaces / "project" / "pom.xml"
            workspace_file.write_text("project", encoding="utf-8")
            data_file = (
                paths.data / "profiles" / "developer" / "home" / "user.txt"
            )
            data_file.write_text("user", encoding="utf-8")

            selected = store.delete("remove")

            self.assertEqual("keep", selected)
            self.assertEqual(["keep"], store.list())
            self.assertFalse((paths.envs / "remove").exists())
            self.assertTrue(workspace_file.is_file())
            self.assertTrue(data_file.is_file())
            self.assertTrue(payload.is_dir())

    def test_maven_creates_private_settings_once_without_component_data_dir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = maven_component(
                paths.components / "maven_eap_component.json"
            )
            catalog = Catalog(paths, {}, {"maven": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "maven" / "apache" / "3.9.16"
            )
            install_path.mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="maven",
                component_id="apache-maven",
                provider="apache",
                provider_name="Apache Software Foundation",
                track=3,
                version="3.9.16",
                url="https://example.test/maven.zip",
                file_name="maven.zip",
                sha256=None,
                sha512="a" * 128,
                size=123,
                metadata_url="https://example.test/maven/",
            )
            store.publish_component(
                "default", artifact, install_path, manifest_sha256="b" * 64
            )

            store.build_process_environment("default", catalog)
            profile = paths.data / "profiles" / "default"
            settings = profile / "home" / ".m2" / "settings.xml"
            self.assertTrue(settings.is_file())
            self.assertIn(
                "${user.home}/.m2/repository",
                settings.read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (profile / "home" / ".m2" / "repository").is_dir()
            )
            self.assertFalse((profile / "components" / "maven").exists())

            settings.write_text(
                "<settings><!-- personalizado --></settings>\n",
                encoding="utf-8",
            )
            store.build_process_environment("default", catalog)
            self.assertIn(
                "personalizado", settings.read_text(encoding="utf-8")
            )

    def test_publishes_declared_core_tools_without_exposing_python_embed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            tool_root = paths.core / "tools" / "7zip"
            tool_root.mkdir(parents=True)
            (tool_root / "7z.exe").touch()
            openssl_root = paths.core / "tools" / "openssl"
            openssl_bin = openssl_root / "bin"
            openssl_bin.mkdir(parents=True)
            (openssl_bin / "openssl.exe").touch()
            atomic_write_json(
                paths.core / "core_tools.json",
                {
                    "schemaVersion": 1,
                    "tools": [
                        {
                            "id": "7zip",
                            "displayName": "7-Zip",
                            "directory": "tools/7zip",
                            "executables": ["7z.exe"],
                            "publishToEnvironmentPath": True,
                        },
                        {
                            "id": "openssl",
                            "displayName": "OpenSSL",
                            "directory": "tools/openssl",
                            "executables": ["bin\\openssl.exe"],
                            "publishToEnvironmentPath": True,
                        }
                    ],
                },
            )
            store = EnvironmentStore(paths)
            store.create("default")
            environment = store.build_process_environment(
                "default", Catalog(paths, {}, {})
            )
            path_entries = environment["PATH"].split(os.pathsep)
            self.assertIn(str(tool_root.resolve()), path_entries)
            self.assertIn(str(openssl_bin.resolve()), path_entries)
            self.assertEqual(
                os.pathsep.join(
                    [str(tool_root.resolve()), str(openssl_bin.resolve())]
                ),
                environment["EAP_CORE_TOOLS"],
            )
            self.assertEqual(
                (openssl_bin / "openssl.exe").resolve(),
                CoreTools.load(paths)
                .tool("openssl")
                .executable("bin\\openssl.exe"),
            )
            self.assertNotIn(
                str(paths.core / "tools" / "python-embed").casefold(),
                environment["PATH"].casefold(),
            )

    def test_rejects_core_tool_executable_outside_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            tool_root = paths.core / "tools" / "safe"
            tool_root.mkdir(parents=True)
            escaped = tool_root.parent / "escaped.exe"
            escaped.touch()
            atomic_write_json(
                paths.core / "core_tools.json",
                {
                    "schemaVersion": 1,
                    "tools": [
                        {
                            "id": "safe",
                            "displayName": "Safe",
                            "directory": "tools/safe",
                            "executables": ["..\\escaped.exe"],
                            "publishToEnvironmentPath": True,
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(
                ValidationError, "sale de su directorio"
            ):
                CoreTools.load(paths)

    def test_inactive_components_do_not_hide_host_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(
                paths.components / "java_eap_component.json"
            )
            git = git_component(
                paths.components / "git_eap_component.json"
            )
            nodejs = nodejs_component(
                paths.components / "nodejs_eap_component.json"
            )
            python = python_component(
                paths.components / "python_eap_component.json"
            )
            catalog = Catalog(
                paths,
                {},
                {
                    "java": component,
                    "git": git,
                    "nodejs": nodejs,
                    "python": python,
                },
            )
            store = EnvironmentStore(paths)
            store.create("empty")
            with patch.dict(
                "os.environ",
                {
                    "PATH": r"C:\Windows\System32",
                    "JAVA_HOME": r"C:\Host\Java",
                    "JAVA_TOOL_OPTIONS": "-Duser.home=C:\\Host",
                    "GIT_HOME": r"C:\Host\Git",
                    "GIT_CONFIG_GLOBAL": r"C:\Users\host\.gitconfig",
                    "NODE_HOME": r"C:\Host\Node",
                    "NPM_CONFIG_CACHE": r"C:\Users\host\.npm",
                    "PYTHONHOME": r"C:\Host\Python",
                    "PYTHONPATH": r"C:\Host\Python\site-packages",
                    "PYTHONUSERBASE": r"C:\Users\host\Python",
                    "VIRTUAL_ENV": r"C:\Host\venv",
                },
                clear=True,
            ):
                environment = store.build_process_environment("empty", catalog)
            self.assertEqual(r"C:\Host\Java", environment["JAVA_HOME"])
            self.assertEqual(
                "-Duser.home=C:\\Host",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertEqual(r"C:\Host\Git", environment["GIT_HOME"])
            self.assertEqual(
                r"C:\Users\host\.gitconfig",
                environment["GIT_CONFIG_GLOBAL"],
            )
            self.assertEqual(r"C:\Host\Node", environment["NODE_HOME"])
            self.assertEqual(
                r"C:\Users\host\.npm", environment["NPM_CONFIG_CACHE"]
            )
            self.assertEqual(r"C:\Host\Python", environment["PYTHONHOME"])
            self.assertEqual(
                r"C:\Host\Python\site-packages",
                environment["PYTHONPATH"],
            )
            self.assertEqual(
                r"C:\Users\host\Python", environment["PYTHONUSERBASE"]
            )
            self.assertEqual(r"C:\Host\venv", environment["VIRTUAL_ENV"])

    def test_environment_config_overrides_global_env_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            paths.config.write_text(
                "env.API_URL=https://global.example\n"
                "env.SHARED_TOKEN=global\n",
                encoding="utf-8",
            )
            store = EnvironmentStore(paths)
            files = store.create("default")
            files.config.write_text(
                "env.SHARED_TOKEN=local\n"
                "env.PROJECT_TOKEN=private\n",
                encoding="utf-8",
            )
            environment = store.build_process_environment(
                "default", Catalog(paths, {}, {})
            )
            self.assertEqual("https://global.example", environment["API_URL"])
            self.assertEqual("local", environment["SHARED_TOKEN"])
            self.assertEqual("private", environment["PROJECT_TOKEN"])

    def test_inactive_component_does_not_reserve_configured_variables(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            paths.config.write_text(
                "env.JAVA_HOME=C:\\Configured\\Java\n",
                encoding="utf-8",
            )
            store = EnvironmentStore(paths)
            store.create("default")
            catalog = Catalog(
                paths,
                {},
                {
                    "java": java_component(
                        paths.components / "java.json"
                    )
                },
            )

            environment = store.build_process_environment(
                "default", catalog
            )

            self.assertEqual(
                "C:\\Configured\\Java", environment["JAVA_HOME"]
            )

    def test_windows_trust_is_profile_scoped_and_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            paths.config.write_text(
                "http_proxy=http://proxy.internal:8080\n"
                "https_proxy=http://proxy.internal:8443\n"
                "no_proxy=localhost,.internal\n",
                encoding="utf-8",
            )
            store = EnvironmentStore(paths)
            files = store.create("default")
            files.config.write_text(
                "# Privado\n"
                "env.PROJECT_TOKEN=private\n"
                "env.NODE_TLS_REJECT_UNAUTHORIZED=0\n"
                "env.GIT_SSL_NO_VERIFY=true\n",
                encoding="utf-8",
            )
            store.set_windows_trust("default", True)
            self.assertTrue(store.windows_trust_enabled("default"))
            self.assertIn(
                "env.PROJECT_TOKEN=private",
                files.config.read_text(encoding="utf-8"),
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": r"C:\Windows\System32",
                        "JAVA_TOOL_OPTIONS": (
                            "-Dfile.encoding=UTF-8 "
                            "-Djavax.net.ssl.trustStore=C:\\old"
                        ),
                    },
                    clear=True,
                ),
                patch.object(
                    EnvironmentStore,
                    "_windows_root_ca_pem",
                    return_value=(
                        "-----BEGIN CERTIFICATE-----\n"
                        "VEVTVA==\n"
                        "-----END CERTIFICATE-----\n"
                    ),
                ),
            ):
                environment = store.build_process_environment(
                    "default", Catalog(paths, {}, {})
                )
            bundle = (
                paths.data
                / "profiles"
                / "default"
                / "trust"
                / "windows-root-ca.pem"
            )
            self.assertTrue(bundle.is_file())
            self.assertEqual("1", environment["NODE_USE_SYSTEM_CA"])
            self.assertEqual("1", environment["NODE_USE_ENV_PROXY"])
            self.assertEqual(str(bundle), environment["NODE_EXTRA_CA_CERTS"])
            self.assertEqual("true", environment["NPM_CONFIG_STRICT_SSL"])
            self.assertEqual(
                "1", environment["NODE_TLS_REJECT_UNAUTHORIZED"]
            )
            self.assertEqual("false", environment["GIT_SSL_NO_VERIFY"])
            self.assertEqual(str(bundle), environment["SSL_CERT_FILE"])
            self.assertEqual(str(bundle), environment["REQUESTS_CA_BUNDLE"])
            self.assertIn(
                "-Dfile.encoding=UTF-8", environment["JAVA_TOOL_OPTIONS"]
            )
            self.assertIn(
                "-Djavax.net.ssl.trustStore=NONE",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertIn(
                "-Djavax.net.ssl.trustStoreType=Windows-ROOT",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertGreater(
                environment["JAVA_TOOL_OPTIONS"].rfind(
                    "-Djavax.net.ssl.trustStore=NONE"
                ),
                environment["JAVA_TOOL_OPTIONS"].rfind(
                    "-Djavax.net.ssl.trustStore=C:\\old"
                ),
            )
            self.assertIn(
                "-Dhttps.proxyHost=proxy.internal",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertIn(
                "-Dhttps.proxyPort=8443",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertIn(
                "-Dhttp.nonProxyHosts=localhost|*.internal",
                environment["JAVA_TOOL_OPTIONS"],
            )

            store.set_windows_trust("default", False)
            self.assertFalse(store.windows_trust_enabled("default"))
            self.assertEqual(
                1,
                files.config.read_text(encoding="utf-8").count(
                    "trust.windows="
                ),
            )

    def test_windows_trust_rejects_invalid_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            files = store.create("default")
            files.config.write_text(
                "trust.windows=quizá\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValidationError, "Booleano inválido para trust.windows"
            ):
                store.windows_trust_enabled("default")

    def test_global_proxy_properties_are_published_on_profile_activation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            paths.config.write_text(
                "http_proxy=http://proxy.internal:8080\n"
                "https_proxy=http://proxy.internal:8080\n"
                "no_proxy=localhost,127.0.0.1,.internal\n",
                encoding="utf-8",
            )
            store = EnvironmentStore(paths)
            store.create("default")
            with patch.dict(
                os.environ,
                {
                    "PATH": r"C:\Windows\System32",
                    "HTTP_PROXY": "http://host-proxy:3128",
                },
                clear=True,
            ):
                environment = store.build_process_environment(
                    "default", Catalog(paths, {}, {})
                )

            self.assertEqual(
                "http://proxy.internal:8080", environment["http_proxy"]
            )
            self.assertEqual(
                environment["http_proxy"], environment["HTTP_PROXY"]
            )
            self.assertEqual(
                environment["https_proxy"], environment["HTTPS_PROXY"]
            )
            self.assertEqual(
                "localhost,127.0.0.1,.internal", environment["NO_PROXY"]
            )

    def test_environment_config_cannot_replace_portability_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            store = EnvironmentStore(paths)
            files = store.create("default")
            files.config.write_text(
                "env.USERPROFILE=C:\\Users\\host\n", encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                store.build_process_environment(
                    "default", Catalog(paths, {}, {})
                )

    def test_publishes_git_with_portable_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            manifest = paths.components / "git_eap_component.json"
            component = git_component(manifest)
            catalog = Catalog(paths, {}, {"git": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "git" / "git-for-windows" / "2.55.0.5"
            )
            (install_path / "cmd").mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="git",
                component_id="git-for-windows-mingit",
                provider="git-for-windows",
                provider_name="Git for Windows · MinGit ZIP",
                track=2,
                version="2.55.0.5",
                url="https://example.test/mingit.zip",
                file_name="MinGit-2.55.0.5-64-bit.zip",
                sha256="e" * 64,
                size=123,
                metadata_url="https://example.test/releases/latest",
            )
            store.publish_component(
                "default", artifact, install_path, manifest_sha256="f" * 64
            )
            with patch.dict(
                "os.environ",
                {
                    "PATH": r"C:\Windows\System32",
                    "USERPROFILE": r"C:\Users\host",
                    "GIT_HOME": r"C:\Host\Git",
                    "GIT_CONFIG_GLOBAL": r"C:\Users\host\.gitconfig",
                },
                clear=True,
            ):
                environment = store.build_process_environment(
                    "default", catalog
                )
            home = paths.data / "profiles" / "default" / "home"
            self.assertEqual(str(install_path), environment["GIT_HOME"])
            self.assertEqual(
                f"{home}/.gitconfig", environment["GIT_CONFIG_GLOBAL"]
            )
            self.assertEqual(
                str(install_path / "cmd"),
                environment["PATH"].split(os.pathsep)[0],
            )
            self.assertTrue((home / ".gitconfig").is_file())
            self.assertTrue((home / ".ssh").is_dir())

    def test_publishes_nodejs_with_portable_npm_data_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            manifest = paths.components / "nodejs_eap_component.json"
            component = nodejs_component(manifest)
            catalog = Catalog(paths, {}, {"nodejs": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = paths.components / "nodejs" / "nodejs" / "24.19.0"
            install_path.mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="nodejs",
                component_id="nodejs-official",
                provider="nodejs",
                provider_name="Node.js Foundation",
                track=24,
                version="24.19.0",
                url="https://example.test/node.zip",
                file_name="node-v24.19.0-win-x64.zip",
                sha256="a" * 64,
                size=123,
                metadata_url="https://example.test/index.json",
            )
            store.publish_component(
                "default", artifact, install_path, manifest_sha256="b" * 64
            )
            with patch.dict(
                "os.environ",
                {
                    "PATH": r"C:\Windows\System32",
                    "USERPROFILE": r"C:\Users\host",
                    "NODE_HOME": r"C:\Host\Node",
                    "NPM_CONFIG_CACHE": r"C:\Users\host\.npm",
                    "NPM_CONFIG_PREFIX": r"C:\Users\host\.npm-global",
                },
                clear=True,
            ):
                environment = store.build_process_environment(
                    "default", catalog
                )
            home = paths.data / "profiles" / "default" / "home"
            path_entries = environment["PATH"].split(os.pathsep)
            self.assertEqual(str(install_path), environment["NODE_HOME"])
            self.assertEqual(f"{home}/.npm", environment["NPM_CONFIG_CACHE"])
            self.assertEqual(
                f"{home}/.npm-global", environment["NPM_CONFIG_PREFIX"]
            )
            self.assertEqual(str(install_path), path_entries[0])
            self.assertEqual(str(home / ".npm-global"), path_entries[1])
            self.assertTrue((home / ".npm").is_dir())
            self.assertTrue((home / ".npm-global").is_dir())

    def test_publishes_python_with_portable_user_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            manifest = paths.components / "python_eap_component.json"
            component = python_component(manifest)
            catalog = Catalog(paths, {}, {"python": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "python" / "pythoncore" / "3.14.7"
            )
            install_path.mkdir(parents=True)
            (install_path / "python.exe").touch()
            artifact = ResolvedArtifact(
                family="python",
                component_id="pythoncore-official",
                provider="pythoncore",
                provider_name="CPython oficial · PythonCore",
                track="3.14",
                version="3.14.7",
                url="https://example.test/python.zip",
                file_name="python-3.14.7-amd64.zip",
                sha256="a" * 64,
                size=123,
                metadata_url="https://example.test/index-windows.json",
            )
            store.publish_component(
                "default", artifact, install_path, manifest_sha256="b" * 64
            )
            with patch.dict(
                "os.environ",
                {
                    "PATH": r"C:\Windows\System32",
                    "USERPROFILE": r"C:\Users\host",
                    "PYTHONHOME": r"C:\Host\Python",
                    "PYTHONPATH": r"C:\Host\Python\site-packages",
                    "PYTHONUSERBASE": r"C:\Users\host\Python",
                    "VIRTUAL_ENV": r"C:\Host\venv",
                },
                clear=True,
            ):
                environment = store.build_process_environment(
                    "default", catalog
                )
            home = paths.data / "profiles" / "default" / "home"
            path_entries = environment["PATH"].split(os.pathsep)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("VIRTUAL_ENV", environment)
            self.assertEqual(
                f"{home}/.python", environment["PYTHONUSERBASE"]
            )
            self.assertEqual("1", environment["PIP_USER"])
            self.assertEqual(str(install_path), path_entries[0])
            self.assertEqual(
                str(
                    paths.data
                    / "profiles"
                    / "default"
                    / "components"
                    / "python"
                    / "bin"
                ),
                path_entries[1],
            )
            self.assertEqual(
                str(home / ".python" / "Scripts"), path_entries[2]
            )
            self.assertTrue((Path(path_entries[1]) / "pip.cmd").is_file())
            self.assertTrue((home / ".cache" / "pip").is_dir())

    def test_publishes_java_with_portable_profile_and_without_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = EapPaths.from_root(root)
            paths.ensure_layout()
            paths.config.write_text(
                "env.JAVA_TOOL_OPTIONS=-Duser.language=en "
                "-Duser.country=US\n",
                encoding="utf-8",
            )
            manifest = paths.components / "java_eap_component.json"
            component = java_component(manifest)
            component.value["environment"].pop("appendable")
            catalog = Catalog(paths, {}, {"java": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = paths.components / "java" / "temurin" / "21.0.12+8"
            (install_path / "bin").mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                url="https://example.test/temurin.zip",
                file_name="temurin.zip",
                sha256="c" * 64,
                size=123,
                metadata_url="https://example.test/metadata",
            )
            store.publish_component(
                "default", artifact, install_path, manifest_sha256="d" * 64
            )
            host_path = (
                f"{paths.core / 'tools' / 'python-embed'}"
                f"{os.pathsep}{paths.components / 'java' / 'corretto' / 'old'}"
                f"{os.pathsep}C:\\Windows\\System32"
            )
            host_environment = {
                "PATH": host_path,
                "USERPROFILE": r"C:\Users\host",
                "APPDATA": r"C:\Users\host\AppData\Roaming",
                "LOCALAPPDATA": r"C:\Users\host\AppData\Local",
                "TEMP": r"C:\Users\host\AppData\Local\Temp",
                "TMP": r"C:\Users\host\AppData\Local\Temp",
                "ProgramFiles": r"C:\Program Files",
                "JAVA_HOME": r"C:\Host\Java",
                "JAVA_TOOL_OPTIONS": "-Duser.home=C:\\Host",
                "EAP_CORE_LEAK": str(
                    paths.core / "tools" / "python-embed"
                ),
                "EAP_BOOTSTRAP_HOST_USERPROFILE": r"C:\Users\host",
                "EAP_BOOTSTRAP_HOST_APPDATA": (
                    r"C:\Users\host\AppData\Roaming"
                ),
                "EAP_BOOTSTRAP_HOST_LOCALAPPDATA": (
                    r"C:\Users\host\AppData\Local"
                ),
            }
            with patch.dict("os.environ", host_environment, clear=True):
                environment = store.build_process_environment("default", catalog)
            profile = paths.data / "profiles" / "default"
            home = profile / "home"
            self.assertEqual(str(install_path), environment["JAVA_HOME"])
            self.assertIn(
                f'-Duser.home="{home}"',
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertIn(
                "-Duser.language=en -Duser.country=US",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertNotIn(
                "-Duser.home=C:\\Host",
                environment["JAVA_TOOL_OPTIONS"],
            )
            self.assertLess(
                environment["JAVA_TOOL_OPTIONS"].index(
                    "-Duser.language=en"
                ),
                environment["JAVA_TOOL_OPTIONS"].index("-Duser.home="),
            )
            self.assertEqual(
                str(install_path / "bin"),
                environment["PATH"].split(os.pathsep)[0],
            )
            self.assertNotIn(
                str(paths.core).lower(),
                environment["PATH"].lower(),
            )
            self.assertNotIn(
                str(paths.components / "java" / "corretto" / "old").lower(),
                environment["PATH"].lower(),
            )
            self.assertNotIn("EAP_CORE_LEAK", environment)
            self.assertNotIn("EAP_BOOTSTRAP_HOST_USERPROFILE", environment)
            self.assertNotIn("EAP_BOOTSTRAP_HOST_APPDATA", environment)
            self.assertNotIn("EAP_BOOTSTRAP_HOST_LOCALAPPDATA", environment)
            self.assertEqual(str(home), environment["USERPROFILE"])
            self.assertEqual(str(home), environment["HOME"])
            self.assertEqual(
                str(home / "AppData" / "Roaming"),
                environment["APPDATA"],
            )
            self.assertEqual(
                str(home / "AppData" / "Local"),
                environment["LOCALAPPDATA"],
            )
            self.assertEqual(
                str(home / "AppData" / "Local" / "Temp"),
                environment["TEMP"],
            )
            self.assertEqual(environment["TEMP"], environment["TMP"])
            self.assertEqual(str(profile), environment["EAP_DATA_PROFILE"])
            self.assertEqual("default", environment["EAP_PROFILE"])
            self.assertEqual("default", environment["EAP_ENV"])
            program_files = next(
                value
                for key, value in environment.items()
                if key.lower() == "programfiles"
            )
            self.assertEqual(r"C:\Program Files", program_files)
            self.assertTrue(Path(environment["APPDATA"]).is_dir())
            self.assertTrue(Path(environment["TEMP"]).is_dir())
            self.assertFalse((home / ".gitconfig").exists())


class ManagedTerminalTests(unittest.TestCase):
    def _manager(self, root: Path) -> ManagedTerminal:
        paths = EapPaths.from_root(root)
        paths.ensure_layout()
        python_root = paths.core / "tools" / "python-embed"
        python_root.mkdir(parents=True)
        (python_root / "python.exe").touch()
        terminal_root = paths.core / "tools" / "windows-terminal"
        terminal_root.mkdir(parents=True)
        (terminal_root / "WindowsTerminal.exe").touch()
        (terminal_root / "wt.exe").touch()
        atomic_write_json(
            paths.core / "core_tools.json",
            {
                "schemaVersion": 1,
                "tools": [
                    {
                        "id": "windows-terminal",
                        "displayName": "Windows Terminal Portable",
                        "directory": "tools/windows-terminal",
                        "executables": ["WindowsTerminal.exe", "wt.exe"],
                        "publishToEnvironmentPath": False,
                    }
                ],
            },
        )
        environments = EnvironmentStore(paths)
        environments.create("default")
        return ManagedTerminal(
            paths,
            environments,
            Catalog(paths, {}, {}),
            CoreTools.load(paths),
        )

    def test_generates_isolated_dynamic_terminal_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            configuration = manager.prepare("default")
            settings = load_json(configuration.settings_path)
            profiles = settings["profiles"]["list"]
            self.assertEqual(
                configuration.settings_path,
                manager.paths.data
                / "profiles"
                / "default"
                / "home"
                / "AppData"
                / "Local"
                / "Microsoft"
                / "Windows Terminal"
                / "settings.json",
            )
            self.assertEqual(5, len(profiles))
            self.assertEqual(profiles[1]["guid"], settings["defaultProfile"])
            self.assertEqual("maximized", settings["launchMode"])
            self.assertIn(
                "--shell-on-exit", profiles[0]["commandline"]
            )
            self.assertIn("shell --type cmd", profiles[1]["commandline"])
            self.assertIn(
                "shell --type powershell", profiles[2]["commandline"]
            )
            self.assertNotIn("--env", profiles[1]["commandline"])
            self.assertTrue(profiles[3]["hidden"])
            self.assertTrue(profiles[4]["hidden"])
            self.assertEqual(
                str(manager.paths.workspaces / "default"),
                profiles[1]["startingDirectory"],
            )
            self.assertEqual(
                "1",
                configuration.process_environment[
                    "EAP_MANAGED_TERMINAL"
                ],
            )
            self.assertEqual(
                str(configuration.settings_path),
                configuration.process_environment[
                    "EAP_TERMINAL_SETTINGS"
                ],
            )

    def test_starts_portable_terminal_with_manager_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            with patch(
                "eap.terminal.subprocess.Popen",
                return_value=SimpleNamespace(pid=4321),
            ) as popen:
                result = manager.start("default")
            self.assertEqual(4321, result.process_id)
            command = popen.call_args.args[0]
            self.assertEqual(
                str(
                    manager.paths.core
                    / "tools"
                    / "windows-terminal"
                    / "WindowsTerminal.exe"
                ),
                command[0],
            )
            self.assertIn("EAP · Gestor", command)
            self.assertIn("--maximized", command)
            self.assertEqual(
                "1",
                popen.call_args.kwargs["env"]["EAP_MANAGED_TERMINAL"],
            )

    def test_prepares_manager_when_locked_component_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = self._manager(Path(temporary))
            component = java_component(
                manager.paths.components / "java_eap_component.json"
            )
            manager = ManagedTerminal(
                manager.paths,
                manager.environments,
                Catalog(manager.paths, {}, {"java": component}),
                manager.core_tools,
            )
            missing_install = (
                manager.paths.components
                / "java"
                / "temurin"
                / "21.0.12.1+1"
            )
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12.1+1",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=100,
                metadata_url="https://example.test/releases",
            )
            manager.environments.publish_component(
                "default", artifact, missing_install, "b" * 64
            )

            with self.assertRaises(ValidationError):
                manager.environments.build_process_environment(
                    "default", manager.catalog
                )

            configuration = manager.prepare("default")
            self.assertEqual(
                "1",
                configuration.process_environment["EAP_MANAGED_TERMINAL"],
            )
            self.assertNotIn("JAVA_HOME", configuration.process_environment)
            self.assertTrue(configuration.settings_path.is_file())


class ComponentLifecycleTests(unittest.TestCase):
    def test_update_check_continues_and_migrates_obsolete_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            dbeaver = dbeaver_component(paths.temp / "dbeaver.json")
            maven = maven_component(paths.temp / "maven.json")
            catalog = Catalog(
                paths, {}, {"dbeaver": dbeaver, "maven": maven}
            )
            store = EnvironmentStore(paths)
            store.create("default")
            lock = store.read_lock("default")
            lock["components"] = [
                {
                    "id": "maven",
                    "provider": "apache",
                    "track": 3,
                    "version": "3.9.11",
                },
                {
                    "id": "dbeaver",
                    "provider": "community",
                    "track": "26.1",
                    "version": "26.1.5",
                },
            ]
            atomic_write_json(store.files("default").lock, lock)
            latest = ResolvedArtifact(
                family="dbeaver",
                component_id="dbeaver-community",
                provider="community",
                provider_name="DBeaver Community",
                track=26,
                version="26.2.0",
                url="https://dbeaver.example/dbeaver.zip",
                file_name="dbeaver.zip",
                sha256="a" * 64,
                size=None,
                metadata_url="https://dbeaver.example/download/",
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            def resolve(
                family: str, provider: str, track: int | str
            ) -> ResolvedArtifact:
                if family == "maven":
                    raise NetworkError("GitHub rate limit exceeded")
                self.assertEqual(26, track)
                return latest

            errors: dict[str, str] = {}
            with patch.object(app, "resolve", side_effect=resolve):
                updates = app.check_updates(
                    "default", persist=False, errors=errors
                )

            self.assertEqual(["dbeaver"], [item.family for item in updates])
            self.assertEqual(26, updates[0].track)
            self.assertFalse(updates[0].major_update)
            self.assertEqual(
                {"maven": "GitHub rate limit exceeded"}, errors
            )

    def test_opted_in_component_offers_newer_declared_major(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = dbeaver_component(paths.temp / "dbeaver.json")
            component.value["tracks"].append(
                {"id": 27, "displayName": "DBeaver 27.x estable"}
            )
            catalog = Catalog(paths, {}, {"dbeaver": component})
            store = EnvironmentStore(paths)
            store.create("default")
            lock = store.read_lock("default")
            lock["components"] = [
                {
                    "id": "dbeaver",
                    "provider": "community",
                    "track": 26,
                    "version": "26.2.0",
                }
            ]
            atomic_write_json(store.files("default").lock, lock)
            releases = {
                26: ResolvedArtifact(
                    family="dbeaver",
                    component_id="dbeaver-community",
                    provider="community",
                    provider_name="DBeaver Community",
                    track=26,
                    version="26.2.1",
                    url="https://example.test/dbeaver-26.zip",
                    file_name="dbeaver-26.zip",
                    sha256="a" * 64,
                    size=None,
                    metadata_url="https://example.test/releases",
                ),
                27: ResolvedArtifact(
                    family="dbeaver",
                    component_id="dbeaver-community",
                    provider="community",
                    provider_name="DBeaver Community",
                    track=27,
                    version="27.0.0",
                    url="https://example.test/dbeaver-27.zip",
                    file_name="dbeaver-27.zip",
                    sha256="b" * 64,
                    size=None,
                    metadata_url="https://example.test/releases",
                ),
            }
            app = EapApplication.__new__(EapApplication)
            app.catalog = catalog
            app.environments = store

            with patch.object(
                app,
                "resolve",
                side_effect=lambda family, provider, track: releases[track],
            ) as resolve:
                update = app.resolve_update("default", "dbeaver")

            self.assertIsNotNone(update)
            assert update is not None
            self.assertEqual(27, update.track)
            self.assertEqual("27.0.0", update.latest.version)
            self.assertTrue(update.major_update)
            self.assertTrue(update.as_json()["majorUpdate"])
            self.assertEqual(
                [
                    call("dbeaver", "community", 26),
                    call("dbeaver", "community", 27),
                ],
                resolve.call_args_list,
            )

            with (
                patch.object(app, "resolve_update", return_value=update),
                patch.object(
                    app,
                    "install",
                    return_value=(update.latest, Path("dbeaver")),
                ) as install,
            ):
                with self.assertRaisesRegex(
                    ValidationError, "confirmar el nombre"
                ):
                    app.update("default", "dbeaver")
                app.update(
                    "default",
                    "dbeaver",
                    major_confirmation="dbeaver",
                )
            install.assert_called_once()

    def test_runtime_does_not_offer_another_major_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(paths.temp / "java.json")
            app = EapApplication.__new__(EapApplication)
            app.catalog = Catalog(paths, {}, {"java": component})
            latest = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.13+9",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=None,
                metadata_url="https://example.test/releases",
            )

            with patch.object(
                app, "resolve", return_value=latest
            ) as resolve:
                update = app._resolve_locked_update(
                    {
                        "id": "java",
                        "provider": "temurin",
                        "track": 21,
                        "version": "21.0.12+8",
                    }
                )

            self.assertIsNotNone(update)
            assert update is not None
            self.assertEqual(21, update.track)
            self.assertFalse(update.major_update)
            resolve.assert_called_once_with("java", "temurin", 21)

    def test_disabled_payload_can_be_reactivated_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(paths.components / "java.json")
            catalog = Catalog(paths, {}, {"java": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "java" / "temurin" / "21.0.12+8"
            )
            for relative in component.value["install"]["requiredFiles"]:
                required = install_path / relative
                required.parent.mkdir(parents=True, exist_ok=True)
                required.touch()
            atomic_write_json(
                install_path / ".eap-install.json",
                {
                    "schemaVersion": 1,
                    "component": "java",
                    "provider": "temurin",
                    "track": 21,
                    "version": "21.0.12+8",
                    "checksumAlgorithm": "sha256",
                    "artifactChecksum": "a" * 64,
                    "status": "ready",
                },
            )
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=100,
                metadata_url="https://example.test/releases",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            app.disable_component("default", "java")

            self.assertEqual([], app.inventory("default"))
            marker = load_json(install_path / ".eap-install.json")
            self.assertEqual(
                "https://example.test/java.zip",
                marker["source"]["url"],
            )
            [payload] = app.available_component_payloads("default")
            self.assertTrue(payload.restorable)

            with patch.object(app, "resolve") as resolve:
                activated = app.activate_component_payload(
                    "default", payload
                )

            resolve.assert_not_called()
            self.assertEqual("java", activated.component_id)
            self.assertEqual(
                ["java"],
                [item["id"] for item in app.inventory("default")],
            )
            self.assertEqual(
                "https://example.test/java.zip",
                app.inventory("default")[0]["artifact"]["url"],
            )

    def test_legacy_payload_with_obsolete_track_can_still_be_activated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = dbeaver_component(
                paths.components / "dbeaver.json"
            )
            catalog = Catalog(paths, {}, {"dbeaver": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components
                / "dbeaver"
                / "community"
                / "26.1.5"
            )
            for relative in component.value["install"]["requiredFiles"]:
                required = install_path / relative
                required.parent.mkdir(parents=True, exist_ok=True)
                required.touch()
            atomic_write_json(
                install_path / ".eap-install.json",
                {
                    "schemaVersion": 1,
                    "component": "dbeaver",
                    "provider": "community",
                    "track": "26.1",
                    "version": "26.1.5",
                    "checksumAlgorithm": "sha256",
                    "artifactChecksum": "a" * 64,
                    "status": "ready",
                },
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            [payload] = app.available_component_payloads("default")
            self.assertFalse(payload.restorable)
            self.assertEqual(26, payload.track)
            app.activate_component_payload("default", payload)

            locked = app.inventory("default")[0]
            self.assertTrue(locked["artifact"]["localOnly"])
            self.assertEqual(26, locked["track"])

    def test_declared_dependencies_are_informational(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            maven = maven_component(paths.components / "maven.json")
            self.assertEqual(
                [
                    {
                        "capability": "runtime.java",
                        "minimumTrack": 8,
                    }
                ],
                maven.value["requires"],
            )
            self.assertFalse(
                hasattr(EapApplication, "_validate_dependencies")
            )

    def test_maven_can_be_installed_with_java_from_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            maven = maven_component(paths.components / "maven.json")
            catalog = Catalog(paths, {}, {"maven": maven})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "maven" / "apache" / "3.9.16"
            )
            install_path.mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="maven",
                component_id="apache-maven",
                provider="apache",
                provider_name="Apache Software Foundation",
                track=3,
                version="3.9.16",
                url="https://example.test/maven.zip",
                file_name="maven.zip",
                sha256=None,
                sha512="a" * 128,
                size=123,
                metadata_url="https://example.test/maven/",
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.settings = Settings(dict(DEFAULTS))
            app.client = HttpClient(1)
            app.catalog = catalog
            app.environments = store
            app.status = lambda message: None
            app.update_cache_path = paths.data / "update-checks.json"

            with (
                patch.dict(
                    "os.environ",
                    {
                        "PATH": r"C:\Host\Java\bin",
                        "JAVA_HOME": r"C:\Host\Java",
                    },
                    clear=True,
                ),
                patch("eap.application.ComponentInstaller") as installer,
            ):
                installer.return_value.install.return_value = (
                    install_path,
                    artifact,
                )
                app.install(
                    "default",
                    "maven",
                    "apache",
                    3,
                    artifact=artifact,
                )

            process_environment = installer.return_value.install.call_args.kwargs[
                "process_environment"
            ]
            self.assertEqual(
                r"C:\Host\Java", process_environment["JAVA_HOME"]
            )
            self.assertIn(
                r"C:\Host\Java\bin",
                process_environment["PATH"].split(os.pathsep),
            )
            self.assertEqual(
                ["maven"],
                [item["id"] for item in app.inventory("default")],
            )

    def test_disabling_component_ignores_dependencies_and_preserves_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            java = java_component(paths.components / "java.json")
            maven = maven_component(paths.components / "maven.json")
            catalog = Catalog(paths, {}, {"java": java, "maven": maven})
            store = EnvironmentStore(paths)
            store.create("default")
            desired = store.read_desired("default")
            desired["components"] = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "updatePolicy": "same-track",
                },
                {
                    "id": "maven",
                    "provider": "apache",
                    "track": 3,
                    "updatePolicy": "same-track",
                },
            ]
            lock = store.read_lock("default")
            lock["components"] = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "version": "21.0.12+8",
                },
                {
                    "id": "maven",
                    "provider": "apache",
                    "track": 3,
                    "version": "3.9.16",
                },
            ]
            atomic_write_json(store.files("default").desired, desired)
            atomic_write_json(store.files("default").lock, lock)
            payload = paths.components / "java" / "payload.txt"
            payload.parent.mkdir(parents=True)
            payload.write_text("shared", encoding="utf-8")
            profile_data = (
                store.ensure_profile("default") / "home" / ".m2" / "data.txt"
            )
            profile_data.parent.mkdir(parents=True)
            profile_data.write_text("private", encoding="utf-8")
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            app.disable_component("default", "java")
            self.assertEqual(
                ["maven"],
                [item["id"] for item in app.inventory("default")],
            )
            self.assertEqual(
                ["maven"],
                [
                    item["id"]
                    for item in store.read_desired("default")["components"]
                ],
            )
            self.assertTrue(payload.is_file())
            self.assertTrue(profile_data.is_file())

    def test_uninstall_removes_unused_payload_but_keeps_profile_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(paths.components / "java.json")
            catalog = Catalog(paths, {}, {"java": component})
            store = EnvironmentStore(paths)
            store.create("default")
            install_path = (
                paths.components / "java" / "temurin" / "21.0.12+8"
            )
            install_path.mkdir(parents=True)
            (install_path / "payload.txt").write_text(
                "shared", encoding="utf-8"
            )
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=100,
                metadata_url="https://example.test/releases",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            profile_data = (
                store.ensure_profile("default") / "home" / "personal.txt"
            )
            profile_data.parent.mkdir(parents=True, exist_ok=True)
            profile_data.write_text("private", encoding="utf-8")
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            result = app.uninstall_component("default", "java")

            self.assertTrue(result.payload_removed)
            self.assertFalse(install_path.exists())
            self.assertEqual([], app.inventory("default"))
            self.assertTrue(profile_data.is_file())

    def test_uninstall_keeps_payload_referenced_by_another_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(paths.components / "java.json")
            catalog = Catalog(paths, {}, {"java": component})
            store = EnvironmentStore(paths)
            store.create("java11")
            store.create("java21")
            install_path = (
                paths.components / "java" / "temurin" / "21.0.12+8"
            )
            install_path.mkdir(parents=True)
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=100,
                metadata_url="https://example.test/releases",
            )
            for profile_id in ("java11", "java21"):
                store.publish_component(
                    profile_id, artifact, install_path, "b" * 64
                )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            result = app.uninstall_component("java11", "java")

            self.assertEqual(("java21",), result.shared_profiles)
            self.assertFalse(result.payload_removed)
            self.assertTrue(install_path.is_dir())
            self.assertEqual([], app.inventory("java11"))
            self.assertEqual(["java"], [
                item["id"] for item in app.inventory("java21")
            ])


class TemporaryStorageTests(unittest.TestCase):
    def test_console_output_is_written_immediately_to_session_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with capture_console_output(logs) as log_path:
                    self.assertIsNotNone(log_path)
                    assert log_path is not None
                    print("\x1b[31mmensaje visible\x1b[0m")
                    print("mensaje de error", file=sys.stderr)
                    current = log_path.read_text(encoding="utf-8")
                    self.assertIn("mensaje visible", current)
                    self.assertIn("mensaje de error", current)

            self.assertIn("\x1b[31mmensaje visible\x1b[0m", stdout.getvalue())
            self.assertIn("mensaje de error", stderr.getvalue())
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("\x1b[", content)
            self.assertIn("EAP iniciado", content)
            self.assertIn("EAP finalizado", content)

    def test_usage_and_cleanup_remove_temp_contents_and_recreate_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            download = paths.temp / "downloads" / "artifact.zip"
            log = paths.temp / "logs" / "eap.log"
            session_log = paths.logs / "eap-session.log"
            download.write_bytes(b"a" * 1024)
            log.write_bytes(b"b" * 512)
            session_log.write_bytes(b"c" * 256)
            app = EapApplication.__new__(EapApplication)
            app.paths = paths

            usage = app.temporary_storage_usage()
            self.assertEqual(1792, usage.bytes)
            self.assertEqual(3, usage.files)

            result = app.clean_temporary_storage()
            self.assertEqual(1792, result.bytes_removed)
            self.assertEqual(3, result.files_removed)
            self.assertFalse(download.exists())
            self.assertFalse(log.exists())
            self.assertFalse(session_log.exists())
            self.assertTrue(paths.logs.is_dir())
            self.assertTrue((paths.temp / "downloads").is_dir())
            self.assertTrue((paths.temp / "transactions").is_dir())

    def test_cleanup_resets_active_log_and_removes_older_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            old_log = paths.logs / "old.log"
            old_log.write_text("registro anterior", encoding="utf-8")
            app = EapApplication.__new__(EapApplication)
            app.paths = paths

            with redirect_stdout(StringIO()):
                with capture_console_output(paths.logs) as active_log:
                    assert active_log is not None
                    print("mensaje anterior a la limpieza")
                    result = app.clean_temporary_storage()
                    print("mensaje posterior a la limpieza")

            self.assertFalse(old_log.exists())
            self.assertTrue(active_log.is_file())
            content = active_log.read_text(encoding="utf-8")
            self.assertNotIn("mensaje anterior", content)
            self.assertIn("mensaje posterior", content)
            self.assertGreater(result.bytes_removed, 0)

    def test_cleanup_removes_readonly_git_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            diagnostics = (
                paths.temp
                / "diagnostics"
                / "catalog"
                / ".git"
                / "objects"
                / "pack"
            )
            diagnostics.mkdir(parents=True)
            readonly = diagnostics / "catalog.pack"
            readonly.write_bytes(b"pack")
            readonly.chmod(readonly.stat().st_mode & ~stat.S_IWRITE)
            app = EapApplication.__new__(EapApplication)
            app.paths = paths

            result = app.clean_temporary_storage()

            self.assertFalse((paths.temp / "diagnostics").exists())
            self.assertEqual(4, result.bytes_removed)
            self.assertEqual(1, result.files_removed)

    def test_cleanup_refuses_to_run_while_an_operation_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            active_lock = paths.temp / "locks" / "component-java.lock"
            active_lock.write_text("active", encoding="utf-8")
            payload = paths.temp / "downloads" / "artifact.zip"
            payload.write_bytes(b"data")
            app = EapApplication.__new__(EapApplication)
            app.paths = paths

            with self.assertRaisesRegex(
                ValidationError, "operaciones activas"
            ):
                app.clean_temporary_storage()
            self.assertTrue(payload.is_file())


class WorkspaceTests(unittest.TestCase):
    def test_workspace_is_confined_and_separate_from_component_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            catalog = Catalog(paths, {}, {})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")

            workspace = store.workspace_path("default")
            self.assertEqual(paths.workspaces / "hbx", workspace)
            self.assertTrue(workspace.is_dir())

            process_environment = store.build_process_environment(
                "default", catalog
            )
            self.assertEqual(
                str(workspace), process_environment["EAP_WORKSPACE"]
            )

            ide_directory = store.launcher_working_directory(
                "default", "vscode", "environment"
            )
            support_directory = store.launcher_working_directory(
                "default", "dbeaver", "component-data"
            )
            self.assertEqual(workspace, ide_directory)
            self.assertEqual(
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "dbeaver"
                / "workspace",
                support_directory,
            )
            self.assertNotEqual(ide_directory, support_directory)
            with self.assertRaises(ValidationError):
                store.set_workspace("default", "../outside")

    def test_shell_opens_in_associated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            catalog = Catalog(paths, {}, {})
            store = EnvironmentStore(paths)
            store.create("default")
            workspace = store.set_workspace("default", "backend")
            app = SimpleNamespace(environments=store, catalog=catalog)
            with (
                patch(
                    "eap.application.subprocess.call", return_value=0
                ) as call,
                patch("eap.application.console_title") as title,
            ):
                status = EapApplication.open_shell(app, "default", "cmd")
            self.assertEqual(0, status)
            title.assert_called_once_with("EAP (default) · CMD")
            self.assertEqual(workspace, call.call_args.kwargs["cwd"])
            self.assertEqual(
                str(workspace),
                call.call_args.kwargs["env"]["EAP_WORKSPACE"],
            )

    def test_shell_opens_in_degraded_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(
                paths.components / "java_eap_component.json"
            )
            catalog = Catalog(paths, {}, {"java": component})
            store = EnvironmentStore(paths)
            store.create("default")
            artifact = ResolvedArtifact(
                family="java",
                component_id="java-temurin",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12.1+1",
                url="https://example.test/java.zip",
                file_name="java.zip",
                sha256="a" * 64,
                size=100,
                metadata_url="https://example.test/releases",
            )
            store.publish_component(
                "default",
                artifact,
                paths.components / "java" / "temurin" / "21.0.12.1+1",
                "b" * 64,
            )
            app = SimpleNamespace(environments=store, catalog=catalog)

            with (
                patch(
                    "eap.application.subprocess.call", return_value=0
                ) as call,
                patch("eap.application.console_title"),
            ):
                status = EapApplication.open_shell(app, "default", "cmd")

            self.assertEqual(0, status)
            process_environment = call.call_args.kwargs["env"]
            self.assertEqual("default", process_environment["EAP_ENV"])
            self.assertNotIn("JAVA_HOME", process_environment)


class LauncherTests(unittest.TestCase):
    def test_launcher_cannot_override_profile_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dbeaver.json"
            value = dbeaver_component(path).value
            value["launchers"][0]["environment"]["HOME"] = (
                "{{data.component}}/home"
            )

            with self.assertRaisesRegex(
                ValidationError,
                "redefine variables reservadas del profile.*HOME",
            ):
                Catalog._validate_component(value, "dbeaver", path)

            del value["launchers"][0]["environment"]["HOME"]
            value["launchers"][0]["unset"].append("HOME")
            with self.assertRaisesRegex(
                ValidationError,
                "elimina variables reservadas del profile.*HOME",
            ):
                Catalog._validate_component(value, "dbeaver", path)

    def test_intellij_opens_workspace_with_portable_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = intellij_component()
            catalog = Catalog(paths, {}, {"intellij-idea": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            install_path = (
                paths.components
                / "intellij-idea"
                / "jetbrains"
                / "2026.2.1"
            )
            executable = install_path / "bin" / "idea64.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            artifact = ResolvedArtifact(
                family="intellij-idea",
                component_id="intellij-idea-jetbrains",
                provider="jetbrains",
                provider_name="JetBrains · Free / Ultimate",
                track="2026.2",
                version="2026.2.1",
                url="https://example.test/idea.zip",
                file_name="idea.zip",
                sha256="a" * 64,
                size=250,
                metadata_url="https://data.example/releases",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store

            [launcher] = app.available_launchers("default")
            workspace = paths.workspaces / "hbx"
            component_data = (
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "intellij-idea"
            )
            properties = component_data / "idea.properties"
            self.assertEqual(executable.resolve(), launcher.executable)
            self.assertEqual((str(workspace),), launcher.arguments)
            self.assertEqual(workspace, launcher.working_directory)
            self.assertEqual(
                properties,
                Path(launcher.environment["IDEA_PROPERTIES"]),
            )
            self.assertEqual(
                str(install_path),
                launcher.environment["INTELLIJ_IDEA_HOME"],
            )
            self.assertEqual(
                "\n".join(
                    [
                        f"idea.config.path={component_data.as_posix()}/config",
                        f"idea.system.path={component_data.as_posix()}/system",
                        f"idea.plugins.path={component_data.as_posix()}/plugins",
                        f"idea.log.path={component_data.as_posix()}/log",
                        "",
                    ]
                ),
                properties.read_text(encoding="utf-8"),
            )
            for directory in ("config", "system", "plugins", "log"):
                self.assertTrue((component_data / directory).is_dir())

    def test_eclipse_uses_workspace_and_private_runtime_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = eclipse_component()
            catalog = Catalog(paths, {}, {"eclipse": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            install_path = (
                paths.components / "eclipse" / "java" / "2026-06"
            )
            install_path.mkdir(parents=True)
            executable = install_path / "eclipse.exe"
            executable.touch()
            configuration = install_path / "configuration"
            configuration.mkdir()
            (configuration / "config.ini").write_text(
                "eclipse.product=fixture", encoding="utf-8"
            )
            p2 = install_path / "p2"
            p2.mkdir()
            (p2 / "state.txt").write_text("fixture", encoding="utf-8")
            artifact = ResolvedArtifact(
                family="eclipse",
                component_id="eclipse-java",
                provider="java",
                provider_name="Eclipse IDE for Java Developers",
                track="2026-06",
                version="2026-06",
                url="https://example.test/eclipse.zip",
                file_name="eclipse.zip",
                sha256=None,
                sha512="a" * 128,
                size=None,
                metadata_url="https://example.test/",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store

            [launcher] = app.available_launchers("default")
            workspace = paths.workspaces / "hbx"
            component_data = (
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "eclipse"
            )
            runtime = component_data / "runtime" / "java" / "2026-06"
            self.assertEqual(executable.resolve(), launcher.executable)
            self.assertEqual(workspace, launcher.working_directory)
            self.assertEqual(
                (
                    "-configuration",
                    (runtime / "configuration").as_uri(),
                    "-data",
                    str(workspace),
                ),
                launcher.arguments,
            )
            self.assertEqual(
                str(install_path), launcher.environment["ECLIPSE_HOME"]
            )
            self.assertEqual(
                "eclipse.product=fixture",
                (runtime / "configuration" / "config.ini").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "fixture",
                (runtime / "p2" / "state.txt").read_text(encoding="utf-8"),
            )

    def test_external_application_inherits_environment_and_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = external_component(
                paths.core / "catalog" / "components" / "kiro.json"
            )
            catalog = Catalog(paths, {}, {"kiro": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            executable = paths.root / "local-apps" / "Kiro" / "kiro.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store
            app.update_cache_path = paths.data / "update-checks.json"

            linked = app.link_external_component(
                "default", "kiro", executable
            )

            self.assertEqual(executable.resolve(), linked)
            [locked] = store.read_lock("default")["components"]
            self.assertNotIn("installPath", locked)
            self.assertEqual(
                "external-executable", locked["installation"]["type"]
            )
            self.assertEqual(
                str(executable.resolve()),
                locked["installation"]["executable"],
            )
            [launcher] = app.available_launchers("default")
            workspace = paths.workspaces / "hbx"
            profile_home = paths.data / "profiles" / "default" / "home"
            self.assertEqual(executable.resolve(), launcher.executable)
            self.assertEqual(workspace, launcher.working_directory)
            self.assertEqual(
                ("--new-window", str(workspace)), launcher.arguments
            )
            self.assertEqual("default", launcher.environment["EAP_ENV"])
            self.assertEqual(
                str(workspace), launcher.environment["EAP_WORKSPACE"]
            )
            self.assertEqual(
                str(profile_home), launcher.environment["USERPROFILE"]
            )

            process = SimpleNamespace(pid=4321)
            with patch(
                "eap.application.subprocess.Popen", return_value=process
            ) as popen:
                self.assertEqual(4321, app.launch("default", "kiro"))
            self.assertEqual(
                [str(executable.resolve()), "--new-window", str(workspace)],
                popen.call_args.args[0],
            )
            self.assertEqual(workspace, popen.call_args.kwargs["cwd"])
            self.assertEqual(
                "default", popen.call_args.kwargs["env"]["EAP_ENV"]
            )

            executable.unlink()
            [missing] = app.missing_components("default")
            self.assertEqual("kiro", missing["id"])
            self.assertFalse(missing["restorable"])
            self.assertEqual([], app.available_launchers("default"))

    def test_vscode_opens_environment_workspace_with_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = vscode_component(
                paths.components / "vscode_eap_component.json"
            )
            catalog = Catalog(paths, {}, {"vscode": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            install_path = (
                paths.components / "vscode" / "microsoft" / "1.134.0"
            )
            (install_path / "bin").mkdir(parents=True)
            (install_path / "Code.exe").touch()
            (install_path / "bin" / "code.cmd").touch()
            artifact = ResolvedArtifact(
                family="vscode",
                component_id="vscode-microsoft",
                provider="microsoft",
                provider_name="Microsoft",
                track=1,
                version="1.134.0",
                url="https://example.test/vscode.zip",
                file_name="vscode.zip",
                sha256="a" * 64,
                size=250,
                metadata_url="https://update.example/latest",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store

            [launcher] = app.available_launchers("default")
            workspace = paths.workspaces / "hbx"
            component_data = (
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "vscode"
            )
            self.assertEqual(workspace, launcher.working_directory)
            self.assertEqual("--user-data-dir", launcher.arguments[0])
            self.assertEqual(
                component_data / "user-data", Path(launcher.arguments[1])
            )
            self.assertEqual("--extensions-dir", launcher.arguments[2])
            self.assertEqual(
                component_data / "extensions", Path(launcher.arguments[3])
            )
            self.assertEqual(
                ("--disable-updates", "--new-window", str(workspace)),
                launcher.arguments[4:],
            )
            self.assertTrue((component_data / "user-data").is_dir())
            self.assertTrue((component_data / "extensions").is_dir())
            self.assertEqual(
                str(workspace), launcher.environment["EAP_WORKSPACE"]
            )
            self.assertEqual(
                str(install_path), launcher.environment["VSCODE_HOME"]
            )
            self.assertIn(
                str(install_path / "bin"), launcher.environment["PATH"]
            )

    def test_bruno_uses_private_user_data_and_profile_collections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = bruno_component()
            catalog = Catalog(paths, {}, {"bruno": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            install_path = (
                paths.components / "bruno" / "community" / "4.1.0"
            )
            install_path.mkdir(parents=True)
            (install_path / "Bruno.exe").touch()
            artifact = ResolvedArtifact(
                family="bruno",
                component_id="bruno-community",
                provider="community",
                provider_name="Bruno Community",
                track=4,
                version="4.1.0",
                url="https://example.test/bruno.zip",
                file_name="bruno_4.1.0_x64_win.zip",
                sha256="a" * 64,
                size=184_280_060,
                metadata_url="https://api.github.test/releases",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store

            [launcher] = app.available_launchers("default")
            workspace = paths.workspaces / "hbx"
            profile_home = paths.data / "profiles" / "default" / "home"
            component_data = (
                paths.data
                / "profiles"
                / "default"
                / "components"
                / "bruno"
            )
            self.assertEqual(workspace, launcher.working_directory)
            self.assertEqual(
                (f"--user-data-dir={component_data / 'user-data'}",),
                launcher.arguments,
            )
            self.assertTrue((component_data / "user-data").is_dir())
            self.assertTrue((profile_home / "Documents" / "bruno").is_dir())
            self.assertEqual(
                str(component_data),
                launcher.environment["EAP_COMPONENT_DATA"],
            )
            self.assertEqual(
                str(workspace), launcher.environment["EAP_WORKSPACE"]
            )
            self.assertEqual(
                str(profile_home), launcher.environment["USERPROFILE"]
            )

    def test_dbeaver_uses_profile_home_private_workspace_and_detached_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = dbeaver_component(
                paths.components / "dbeaver_eap_component.json"
            )
            catalog = Catalog(paths, {}, {"dbeaver": component})
            store = EnvironmentStore(paths)
            store.create("default", workspace_id="hbx")
            install_path = (
                paths.components / "dbeaver" / "community" / "26.1.5"
            )
            install_path.mkdir(parents=True)
            (install_path / "dbeaver.exe").touch()
            configuration = install_path / "configuration"
            configuration.mkdir()
            (configuration / "config.ini").write_text(
                "eclipse.product=test", encoding="utf-8"
            )
            p2 = install_path / "p2"
            p2.mkdir()
            (p2 / "profile.txt").write_text("fixture", encoding="utf-8")
            artifact = ResolvedArtifact(
                family="dbeaver",
                component_id="dbeaver-community",
                provider="community",
                provider_name="DBeaver Community",
                track="26.1",
                version="26.1.5",
                url="https://example.test/dbeaver.zip",
                file_name="dbeaver.zip",
                sha256="a" * 64,
                size=250,
                metadata_url="https://api.example/releases",
            )
            store.publish_component(
                "default", artifact, install_path, "b" * 64
            )
            app = EapApplication.__new__(EapApplication)
            app.paths = paths
            app.catalog = catalog
            app.environments = store

            launchers = app.available_launchers("default")
            self.assertEqual(1, len(launchers))
            launcher = launchers[0]
            component_data = (
                paths.data / "profiles" / "default" / "components" / "dbeaver"
            )
            private_workspace = component_data / "workspace"
            self.assertEqual(private_workspace, launcher.working_directory)
            self.assertNotEqual(paths.workspaces / "hbx", private_workspace)
            self.assertEqual(
                str(private_workspace), launcher.environment["EAP_WORKSPACE"]
            )
            profile_home = paths.data / "profiles" / "default" / "home"
            self.assertEqual(
                profile_home / "AppData" / "Roaming",
                Path(launcher.environment["APPDATA"]),
            )
            self.assertEqual(
                profile_home,
                Path(launcher.environment["USERPROFILE"]),
            )
            self.assertFalse((component_data / "home").exists())
            private_runtime = component_data / "runtime" / "26.1.5"
            self.assertEqual(
                "eclipse.product=test",
                (private_runtime / "configuration" / "config.ini").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "fixture",
                (private_runtime / "p2" / "profile.txt").read_text(
                    encoding="utf-8"
                ),
            )

            process = SimpleNamespace(pid=4321)
            with patch(
                "eap.application.subprocess.Popen", return_value=process
            ) as popen:
                self.assertEqual(4321, app.launch("default", "dbeaver"))
            self.assertEqual(
                [
                    str(install_path / "dbeaver.exe"),
                    "-configuration",
                    (private_runtime / "configuration").as_uri(),
                    "-data",
                    str(private_workspace),
                ],
                popen.call_args.args[0],
            )
            self.assertEqual(
                private_workspace, popen.call_args.kwargs["cwd"]
            )
            self.assertTrue(popen.call_args.kwargs["close_fds"])


class ShortcutTests(unittest.TestCase):
    def test_shortcut_entry_keeps_a_log_only_when_launch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            with (
                patch.object(
                    shortcut_entry_module.EapPaths,
                    "discover",
                    return_value=paths,
                ),
                patch.object(
                    shortcut_entry_module,
                    "cli_main",
                    side_effect=lambda arguments: (
                        print("ERROR: launcher no disponible") or 1
                    ),
                ),
            ):
                self.assertEqual(
                    1,
                    shortcut_entry_module.run(["vscode", "default"]),
                )
            log = paths.temp / "logs" / "shortcuts" / "default-vscode.log"
            self.assertTrue(log.is_file())
            self.assertIn(
                "launcher no disponible", log.read_text(encoding="utf-8")
            )

            with (
                patch.object(
                    shortcut_entry_module.EapPaths,
                    "discover",
                    return_value=paths,
                ),
                patch.object(
                    shortcut_entry_module, "cli_main", return_value=0
                ),
            ):
                self.assertEqual(
                    0,
                    shortcut_entry_module.run(["vscode", "default"]),
                )
            self.assertFalse(log.exists())

    def test_creates_desktop_link_through_stable_pythonw_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            pythonw = (
                paths.core / "tools" / "python-embed" / "pythonw.exe"
            )
            pythonw.parent.mkdir(parents=True)
            pythonw.touch()
            icon = paths.components / "dbeaver" / "dbeaver.ico"
            icon.parent.mkdir(parents=True)
            Image.new("RGBA", (32, 32), (20, 100, 180, 255)).save(
                icon,
                format="ICO",
                sizes=[(16, 16), (32, 32)],
            )
            desktop = paths.root / "fake-desktop"
            desktop.mkdir()
            shortcut = desktop / "DBeaver Community (default).lnk"
            shortcut.touch()
            completed = SimpleNamespace(
                returncode=0,
                stdout=str(shortcut) + "\n",
                stderr="",
            )
            with patch(
                "eap.shortcuts.subprocess.run", return_value=completed
            ) as run:
                result = WindowsShortcutManager(
                    paths
                ).create_desktop_shortcut(
                    environment_id="default",
                    launcher_id="dbeaver",
                    display_name="DBeaver Community",
                    icon=icon,
                )
            self.assertEqual(shortcut.resolve(), result.path)
            self.assertNotEqual(icon.resolve(), result.icon)
            self.assertEqual(
                paths.data / "shortcut-icons" / "default",
                result.icon.parent,
            )
            self.assertTrue(result.icon.is_file())
            with Image.open(result.icon) as generated:
                self.assertEqual(
                    {(16, 16), (32, 32)}, generated.info["sizes"]
                )
                generated.size = (16, 16)
                generated.load()
                rgba = generated.convert("RGBA")
                colors = {
                    rgba.getpixel((x, y))
                    for x in range(rgba.width)
                    for y in range(rgba.height)
                }
            self.assertIn((232, 78, 31, 255), colors)
            self.assertIn((255, 255, 255, 255), colors)
            process_environment = run.call_args.kwargs["env"]
            self.assertEqual(
                str(pythonw), process_environment["EAP_SHORTCUT_TARGET"]
            )
            self.assertEqual(
                "-B -I -X utf8 -m eap.shortcut_entry dbeaver default",
                process_environment["EAP_SHORTCUT_ARGUMENTS"],
            )
            self.assertEqual(
                f"{result.icon},0",
                process_environment["EAP_SHORTCUT_ICON"],
            )
            self.assertIn("-NoProfile", run.call_args.args[0])
            self.assertIn("-NonInteractive", run.call_args.args[0])

class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = EapPaths.from_root(Path(self.temporary.name))
        self.paths.ensure_layout()
        source_core = Path(__file__).resolve().parents[1]
        tool_root = self.paths.core / "tools" / "7zip"
        tool_root.mkdir(parents=True)
        shutil.copy2(
            source_core / "tools" / "7zip" / "7z.exe",
            tool_root / "7z.exe",
        )
        shutil.copy2(
            source_core / "tools" / "7zip" / "7z.dll",
            tool_root / "7z.dll",
        )
        atomic_write_json(
            self.paths.core / "core_tools.json",
            {
                "schemaVersion": 1,
                "tools": [
                    {
                        "id": "7zip",
                        "displayName": "7-Zip",
                        "directory": "tools/7zip",
                        "executables": ["7z.exe"],
                        "publishToEnvironmentPath": True,
                    }
                ],
            },
        )
        self.paths.catalog.parent.mkdir(parents=True)
        atomic_write_json(
            self.paths.catalog,
            {
                "schemaVersion": 1,
                "catalogVersion": "test",
                "components": [],
            },
        )
        (self.paths.root / "eap.cmd").write_text(
            "@echo off\n", encoding="utf-8"
        )
        (self.paths.root / "config.properties").write_text(
            "environment.default=default\nprivate.token=DO_NOT_EXPORT\n",
            encoding="utf-8",
        )
        catalog_components = self.paths.temp / "test-manifests"
        catalog_components.mkdir(parents=True)
        component = dbeaver_component(
            catalog_components / "dbeaver.json"
        )
        self.catalog = Catalog(self.paths, {}, {"dbeaver": component})
        self.store = EnvironmentStore(self.paths)
        self.store.create("default")
        (self.paths.workspaces / "default" / "project.txt").write_text(
            "portable", encoding="utf-8"
        )
        self.install_path = (
            self.paths.components / "dbeaver" / "community" / "26.1.5"
        )
        self.artifact = ResolvedArtifact(
            family="dbeaver",
            component_id="dbeaver-community",
            provider="community",
            provider_name="DBeaver Community",
            track="26.1",
            version="26.1.5",
            url="https://example.test/dbeaver.zip",
            file_name="dbeaver.zip",
            sha256="a" * 64,
            size=250,
            metadata_url="https://api.example/releases",
        )
        self._create_fake_installation()
        self.store.publish_component(
            "default", self.artifact, self.install_path, "b" * 64
        )
        self.settings = Settings(
            {
                **DEFAULTS,
                "components.repository.danielgube": (
                    "https://github.com/danielgube/eap-components"
                ),
                "components.repository.empresa": (
                    "https://github.com/empresa/eap-components"
                ),
                "pocketools.repository.danielgube": (
                    "https://github.com/danielgube/eap-pocketools"
                ),
            }
        )
        self.core_tools = CoreTools.load(self.paths)
        self.transfer = EnvironmentTransfer(
            self.paths,
            self.settings,
            self.catalog,
            self.store,
            self.core_tools,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_fake_installation(self) -> None:
        component = self.catalog.component("dbeaver")
        for relative in component.value["install"]["requiredFiles"]:
            target = self.install_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        atomic_write_json(
            self.install_path / ".eap-install.json",
            {
                "schemaVersion": 1,
                "component": "dbeaver",
                "provider": "community",
                "track": "26.1",
                "version": "26.1.5",
                "artifactChecksum": "a" * 64,
                "status": "ready",
            },
        )

    def test_live_7zip_output_is_attached_only_to_a_terminal(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        terminal = TtyStringIO()
        with (
            redirect_stdout(terminal),
            patch.object(
                transfers_module.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            output = self.transfer._run_7zip(
                ["a", "-bsp1", "archive.7z", "."],
                cwd=self.paths.root,
                operation="probar el progreso",
                live_output=True,
            )

        self.assertEqual("", output)
        self.assertFalse(run.call_args.kwargs["capture_output"])

    def test_export_without_payload_and_import_detects_missing_component(
        self,
    ) -> None:
        exported = self.transfer.export_environment(
            "default", "dani", include_components=False
        )
        self.assertTrue(exported.archive.is_file())
        self.assertFalse(exported.custom_commands_included)
        listing = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "l",
                "-slt",
                "-ba",
                str(exported.archive),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = [
            line.removeprefix("Path = ").replace("\\", "/")
            for line in listing.splitlines()
            if line.startswith("Path = ")
        ]
        self.assertIn("envs/dani/environment.json", paths)
        self.assertFalse(any(path.startswith("workspaces/") for path in paths))
        self.assertIn("eap-env-package.json", paths)
        self.assertNotIn("eap.cmd", paths)
        self.assertFalse(any(path.startswith("core/") for path in paths))
        self.assertNotIn("components/dbeaver_eap_component.json", paths)
        self.assertFalse(any(path.startswith("data/") for path in paths))
        self.assertFalse(any(path.startswith("temp/") for path in paths))
        self.assertFalse(
            any(path.startswith("components/dbeaver/community/") for path in paths)
        )
        safe_config = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                "envs/dani/config.properties",
            ],
            capture_output=True,
            encoding="utf-8",
            check=True,
        ).stdout
        self.assertIn("Configuración privada no incluida", safe_config)
        self.assertNotIn("DO_NOT_EXPORT", safe_config)

        shutil.rmtree(self.install_path)
        imported = self.transfer.import_environment(exported.archive)
        self.assertEqual("dani", imported.environment_id)
        self.assertEqual(1, imported.components_missing)
        self.assertFalse(imported.configuration_included)
        self.assertFalse(imported.custom_commands_included)
        desired = self.store.read_desired("dani")
        self.assertEqual("dani", desired["dataProfile"])
        self.assertEqual("dani", desired["workspace"])
        self.assertTrue((self.paths.workspaces / "dani").is_dir())
        self.assertFalse(
            (self.paths.workspaces / "dani" / "project.txt").exists()
        )

    def test_import_preserves_existing_local_workspace_contents(self) -> None:
        exported = self.transfer.export_environment(
            "default", "local-workspace", include_components=False
        )
        local_workspace = self.paths.workspaces / "local-workspace"
        local_workspace.mkdir()
        local_file = local_workspace / "local.txt"
        local_file.write_text("do not replace", encoding="utf-8")

        imported = self.transfer.import_environment(exported.archive)

        self.assertEqual("local-workspace", imported.workspace_id)
        self.assertEqual(
            "do not replace", local_file.read_text(encoding="utf-8")
        )

    def test_import_ignores_packaged_workspace_contents(self) -> None:
        exported = self.transfer.export_environment(
            "default", "legacy-workspace", include_components=False
        )
        injected_root = self.paths.temp / "legacy-workspace-content"
        injected_workspace = (
            injected_root / "workspaces" / "legacy-workspace"
        )
        injected_workspace.mkdir(parents=True)
        (injected_workspace / "foreign.txt").write_text(
            "must not import", encoding="utf-8"
        )
        subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "a",
                "-bd",
                "-y",
                str(exported.archive),
                ".",
            ],
            cwd=injected_root,
            capture_output=True,
            check=True,
        )

        imported = self.transfer.import_environment(exported.archive)

        self.assertEqual("legacy-workspace", imported.workspace_id)
        self.assertFalse(
            (
                self.paths.workspaces
                / "legacy-workspace"
                / "foreign.txt"
            ).exists()
        )

    def test_custom_commands_require_opt_in_and_are_restored(self) -> None:
        commands = self.store.custom_commands_path("default")
        (commands / "deploy.cmd").write_text(
            "@echo deploy\n", encoding="utf-8"
        )
        helpers = commands / "helpers"
        helpers.mkdir()
        (helpers / "settings.json").write_text(
            '{"portable": true}\n', encoding="utf-8"
        )

        exported = self.transfer.export_environment(
            "default",
            "with-commands",
            include_components=False,
            include_custom_commands=True,
        )
        self.assertTrue(exported.custom_commands_included)
        manifest_text = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                "eap-env-package.json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertTrue(
            json.loads(manifest_text)["customCommandsIncluded"]
        )

        imported = self.transfer.import_environment(exported.archive)
        imported_commands = self.store.custom_commands_path(
            "with-commands"
        )
        self.assertTrue(imported.custom_commands_included)
        self.assertEqual(
            "@echo deploy\n",
            (imported_commands / "deploy.cmd").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            '{"portable": true}\n',
            (imported_commands / "helpers" / "settings.json").read_text(
                encoding="utf-8"
            ),
        )

    def test_custom_commands_import_does_not_overwrite_local_files(
        self,
    ) -> None:
        commands = self.store.custom_commands_path("default")
        (commands / "exported.cmd").touch()
        exported = self.transfer.export_environment(
            "default",
            "collision",
            include_components=False,
            include_custom_commands=True,
        )
        local_commands = (
            self.paths.data
            / "profiles"
            / "collision"
            / "custom-commands"
        )
        local_commands.mkdir(parents=True)
        local = local_commands / "local.cmd"
        local.write_text("local\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "custom-commands de destino no está vacía",
        ):
            self.transfer.import_environment(exported.archive)

        self.assertEqual("local\n", local.read_text(encoding="utf-8"))
        self.assertFalse((self.paths.envs / "collision").exists())

    def test_private_environment_config_requires_explicit_opt_in(self) -> None:
        config = self.store.files("default").config
        config.write_text(
            "env.PROJECT_TOKEN=very-secret\n", encoding="utf-8"
        )
        exported = self.transfer.export_environment(
            "default",
            "private",
            include_components=False,
            include_configuration=True,
        )
        content = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                "envs/private/config.properties",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("env.PROJECT_TOKEN=very-secret", content)
        self.assertTrue(exported.configuration_included)

    def test_tool_export_contains_eap_but_no_personal_state(self) -> None:
        (self.paths.root / "nested-export.7z").write_bytes(b"do not nest")
        exported = self.transfer.export_tool(
            "eap-test", include_components=False
        )
        listing = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "l",
                "-slt",
                "-ba",
                str(exported.archive),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = [
            line.removeprefix("Path = ").replace("\\", "/")
            for line in listing.splitlines()
            if line.startswith("Path = ")
        ]
        self.assertIn("eap.cmd", paths)
        self.assertIn("eap-tool-package.json", paths)
        self.assertNotIn("nested-export.7z", paths)
        self.assertIn("core/catalog/catalog.json", paths)
        self.assertFalse(
            any(path.startswith("core/catalog/components/") for path in paths)
        )
        self.assertFalse(
            any(
                path.startswith("components/")
                for path in paths
            )
        )
        self.assertTrue(any(path.startswith("core/") for path in paths))
        self.assertFalse(any(path.startswith("envs/") for path in paths))
        self.assertFalse(any(path.startswith("data/") for path in paths))
        self.assertFalse(any(path.startswith("workspaces/") for path in paths))
        self.assertFalse(
            any(path.startswith("components/dbeaver/community/") for path in paths)
        )
        safe_config = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                "config.properties",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("environment.default=default", safe_config)
        self.assertIn("profile.default=default", safe_config)
        self.assertIn(
            "components.repository.empresa=https://github.com/empresa/eap-components",
            safe_config,
        )
        self.assertIn(
            "pocketools.repository.danielgube=https://github.com/"
            "danielgube/eap-pocketools",
            safe_config,
        )
        self.assertNotIn("DO_NOT_EXPORT", safe_config)

    def test_tool_export_enriches_payloads_for_local_activation(self) -> None:
        exported = self.transfer.export_tool(
            "eap-with-components", include_components=True
        )
        marker_text = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                (
                    "components/dbeaver/community/26.1.5/"
                    ".eap-install.json"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        marker = json.loads(marker_text)
        self.assertEqual(
            "https://example.test/dbeaver.zip",
            marker["source"]["url"],
        )
        self.assertEqual("dbeaver.zip", marker["source"]["fileName"])

    def test_export_with_payload_imports_exact_component(self) -> None:
        exported = self.transfer.export_environment(
            "default", "portable", include_components=True
        )
        shutil.rmtree(self.install_path)
        imported = self.transfer.import_environment(exported.archive)
        self.assertEqual(1, imported.components_copied)
        self.assertEqual(0, imported.components_missing)
        self.assertTrue((self.install_path / "dbeaver.exe").is_file())
        self.assertEqual(
            "a" * 64,
            load_json(
                self.install_path / ".eap-install.json"
            )["artifactChecksum"],
        )

    def test_external_binding_is_sanitized_and_must_be_relinked(self) -> None:
        component = external_component(
            self.paths.core / "catalog" / "components" / "kiro.json"
        )
        self.catalog.definitions["kiro"] = component
        executable = self.paths.root / "local-apps" / "Kiro" / "kiro.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.store.publish_external_component(
            "default", component, executable, "c" * 64
        )

        exported = self.transfer.export_environment(
            "default", "shared", include_components=True
        )
        lock_content = subprocess.run(
            [
                str(self.core_tools.tool("7zip").executable("7z.exe")),
                "e",
                "-so",
                str(exported.archive),
                "envs/shared/environment.lock.json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        exported_lock = json.loads(lock_content)
        kiro = next(
            item
            for item in exported_lock["components"]
            if item["id"] == "kiro"
        )
        self.assertIsNone(kiro["installation"]["executable"])
        self.assertNotIn(str(executable.resolve()), lock_content)

        imported = self.transfer.import_environment(exported.archive)
        self.assertEqual("shared", imported.environment_id)
        self.assertEqual(1, imported.components_missing)
        app = SimpleNamespace(
            paths=self.paths,
            inventory=lambda environment_id: self.store.read_lock(
                environment_id
            )["components"],
            catalog=self.catalog,
        )
        [missing] = [
            item
            for item in EapApplication.missing_components(app, "shared")
            if item["id"] == "kiro"
        ]
        self.assertFalse(missing["restorable"])

    def test_import_rejects_unsafe_external_binding(self) -> None:
        component = external_component(
            self.paths.core / "catalog" / "components" / "kiro.json"
        )
        self.catalog.definitions["kiro"] = component
        base = {
            "id": "kiro",
            "provider": "external",
            "track": "local",
            "installation": {
                "type": "external-executable",
                "executable": None,
            },
        }
        for unsafe in ("kiro.exe", r"C:\Apps\not-kiro.exe"):
            with self.subTest(executable=unsafe):
                locked = json.loads(json.dumps(base))
                locked["installation"]["executable"] = unsafe
                with self.assertRaises(IntegrityError):
                    self.transfer._validate_imported_lock(
                        {"components": [locked]}
                    )

    def test_restore_uses_exact_artifact_from_lock(self) -> None:
        shutil.rmtree(self.install_path)
        app = EapApplication.__new__(EapApplication)
        app.paths = self.paths
        app.catalog = self.catalog
        app.environments = self.store
        with patch.object(
            app,
            "install",
            return_value=(self.artifact, self.install_path),
        ) as install:
            restored = app.restore_missing_components("default")
        self.assertEqual(1, len(restored))
        resolved = install.call_args.kwargs["artifact"]
        self.assertEqual("26.1.5", resolved.version)
        self.assertEqual("https://example.test/dbeaver.zip", resolved.url)
        self.assertEqual("a" * 64, resolved.sha256)
        self.assertTrue(install.call_args.kwargs["allow_missing"])

    def test_import_rejects_paths_outside_staging(self) -> None:
        for unsafe in ("../outside", r"C:\outside", "/absolute"):
            with self.subTest(path=unsafe):
                with self.assertRaises(IntegrityError):
                    self.transfer._validate_archive_path(unsafe)

    def test_staging_cleanup_retries_transient_windows_lock(self) -> None:
        staging = self.paths.temp / "exports" / "retry-test"
        staging.mkdir(parents=True)
        with (
            patch(
                "eap.transfers.shutil.rmtree",
                side_effect=[PermissionError("locked"), None],
            ) as remove,
            patch("eap.transfers.time.sleep") as sleep,
        ):
            self.transfer._remove_staging(staging)
        self.assertEqual(2, remove.call_count)
        sleep.assert_called_once_with(0.1)

    def test_staging_cleanup_removes_readonly_files(self) -> None:
        staging = self.paths.temp / "exports" / "readonly-test"
        staging.mkdir(parents=True)
        readonly = staging / "command.cmd"
        readonly.write_text("@echo off\n", encoding="utf-8")
        readonly.chmod(readonly.stat().st_mode & ~stat.S_IWRITE)

        self.transfer._remove_staging(staging)

        self.assertFalse(staging.exists())


class HostIntegrationTests(unittest.TestCase):
    def test_existing_directory_requires_confirmation_then_is_deleted(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as host_temporary,
        ):
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            manifest = paths.core / "catalog" / "host-integrations.json"
            manifest.parent.mkdir(parents=True)
            shutil.copyfile(
                Path(__file__).resolve().parents[1]
                / "catalog"
                / "host-integrations.json",
                manifest,
            )
            store = EnvironmentStore(paths)
            store.create("default")

            host_home = Path(host_temporary) / "home"
            host_roaming = host_home / "AppData" / "Roaming"
            host_local = host_home / "AppData" / "Local"
            firefox = host_roaming / "Mozilla" / "Firefox"
            firefox.mkdir(parents=True)
            host_local.mkdir(parents=True)
            (firefox / "profiles.ini").write_text(
                "[Profile0]\nName=default\n", encoding="utf-8"
            )
            destination = (
                paths.data
                / "profiles"
                / "default"
                / "home"
                / "AppData"
                / "Roaming"
                / "Mozilla"
                / "Firefox"
            )
            destination.mkdir(parents=True)
            portable = destination / "portable.txt"
            portable.write_text("datos portables", encoding="utf-8")

            bootstrap = {
                "EAP_BOOTSTRAP_HOST_USERPROFILE": str(host_home),
                "EAP_BOOTSTRAP_HOST_APPDATA": str(host_roaming),
                "EAP_BOOTSTRAP_HOST_LOCALAPPDATA": str(host_local),
                "COMPUTERNAME": "EAP-TEST",
                "USERDOMAIN": "EAP",
                "USERNAME": "tester",
            }
            with patch.dict(os.environ, bootstrap, clear=False):
                manager = HostIntegrationManager(paths, store)
                with patch.object(
                    manager, "_running_processes", return_value=()
                ):
                    status = manager.status("default", "firefox")
                    self.assertEqual("inactive-with-data", status.state)
                    self.assertEqual(
                        [], manager.configured_statuses("default")
                    )
                    with self.assertRaisesRegex(
                        ValidationError, "requiere confirmar"
                    ):
                        manager.enable("default", "firefox")

                    change = manager.enable(
                        "default", "firefox", delete_existing=True
                    )
                    self.assertTrue(change.status.ok)
                    self.assertTrue(destination.is_junction())
                    self.assertFalse(portable.exists())
                    self.assertEqual((destination,), change.deleted_directories)
                    self.assertEqual(1, change.deleted_files)
                    self.assertEqual(len("datos portables"), change.deleted_bytes)
                    self.assertTrue((destination / "profiles.ini").is_file())
                    [configured] = manager.configured_statuses("default")
                    self.assertTrue(configured.ok)

                    os.rmdir(destination)
                    [broken] = manager.configured_statuses("default")
                    self.assertFalse(broken.ok)
                    self.assertEqual("inactive", broken.state)
                    manager._create_junction(destination, firefox)

                    disabled = manager.disable("default", "firefox")
                    self.assertFalse(disabled.ok)
                    self.assertFalse(destination.exists())
                    self.assertEqual(
                        [], manager.configured_statuses("default")
                    )
                    self.assertTrue((firefox / "profiles.ini").is_file())

    def test_interface_confirms_permanent_delete_before_enabling(self) -> None:
        destination = Path(r"C:\eap\portable\Mozilla\Firefox")
        status = SimpleNamespace(
            id="firefox",
            display_name="Firefox",
            description="Compartir Firefox con el host",
            data_profile="default",
            state="inactive-with-data",
            ok=False,
            detail="hay datos portables locales; integración no activa",
            links=(
                SimpleNamespace(
                    source=Path(r"C:\Users\host\Mozilla\Firefox"),
                    destination=destination,
                ),
            ),
        )
        calls: list[tuple[str, str, bool]] = []

        def enable(
            environment_id: str,
            integration_id: str,
            *,
            delete_existing: bool,
        ) -> Any:
            calls.append(
                (environment_id, integration_id, delete_existing)
            )
            return SimpleNamespace(
                status=SimpleNamespace(display_name="Firefox"),
                deleted_directories=(destination,),
                deleted_bytes=2048,
                deleted_files=3,
            )

        app = SimpleNamespace(enable_host_integration=enable)
        output = StringIO()
        with (
            patch.object(cli_module, "_read_input", side_effect=["1", "s"]),
            redirect_stdout(output),
        ):
            cli_module._interactive_host_integration(
                app, "default", status
            )

        self.assertEqual([("default", "firefox", True)], calls)
        rendered = output.getvalue()
        self.assertIn("El directorio ya existe en", rendered)
        self.assertIn("borrará permanentemente", rendered)
        self.assertIn("no es recuperable desde EAP", rendered)


class InterfaceTests(unittest.TestCase):
    def test_pocketool_info_option_opens_pinned_readme_in_browser(self) -> None:
        readme_url = (
            "https://github.com/danielgube/eap-pocketools/blob/"
            + "a" * 40
            + "/pocketools/sessionkeep/README.md"
        )
        definition = SimpleNamespace(
            id="sessionkeep",
            name="Session Keep",
            version="1.0.0",
            selector="danielgube/sessionkeep",
            source=SimpleNamespace(
                id="danielgube",
                repository_url=(
                    "https://github.com/danielgube/eap-pocketools"
                ),
            ),
            readme_url=readme_url,
        )
        app = SimpleNamespace(
            available_pocketools=lambda **kwargs: [definition],
            pocketools=SimpleNamespace(installed=lambda: []),
        )
        output = StringIO()
        with (
            patch.object(
                cli_module, "_read_input", side_effect=["1i", "\x1b"]
            ),
            patch.object(
                cli_module.webbrowser, "open", return_value=True
            ) as browser_open,
            redirect_stdout(output),
        ):
            cli_module._interactive_install_pocketool(app, "default")

        browser_open.assert_called_once_with(readme_url, new=2)
        rendered = output.getvalue()
        self.assertIn("[1i] README.md", rendered)
        self.assertIn("README.md abierto en el navegador", rendered)

    def test_cli_trust_enable_routes_to_selected_profile(self) -> None:
        calls: list[tuple[str, bool]] = []
        app = SimpleNamespace(
            environments=SimpleNamespace(read_desired=lambda profile: {}),
            set_windows_trust=lambda profile, enabled: (
                calls.append((profile, enabled))
                or {
                    "schemaVersion": 1,
                    "profile": profile,
                    "enabled": enabled,
                }
            ),
        )
        arguments = cli_module.build_parser().parse_args(
            ["trust", "enable", "--profile", "desarrollo", "--json"]
        )
        output = StringIO()
        with redirect_stdout(output):
            code = cli_module.dispatch(app, arguments)
        self.assertEqual(0, code)
        self.assertEqual([("desarrollo", True)], calls)
        self.assertEqual("desarrollo", json.loads(output.getvalue())["profile"])

    def test_pythonw_launch_does_not_require_console_streams(self) -> None:
        launched: list[tuple[str, str]] = []
        launcher = SimpleNamespace(
            id="vscode",
            display_name="Visual Studio Code",
            start_mode="detached",
        )
        app = SimpleNamespace(
            environments=SimpleNamespace(
                read_desired=lambda profile_id: {"id": profile_id}
            ),
            available_launchers=lambda profile_id: [launcher],
            launch=lambda profile_id, launcher_id: (
                launched.append((profile_id, launcher_id)) or 4321
            ),
        )
        with (
            patch.object(cli_module, "EapApplication", return_value=app),
            patch.object(cli_module.sys, "stdout", None),
            patch.object(cli_module.sys, "stderr", None),
        ):
            result = cli_module.main(
                ["launch", "vscode", "--env", "default"]
            )
        self.assertEqual(0, result)
        self.assertEqual([("default", "vscode")], launched)

    def test_interactive_activation_uses_a_local_payload(self) -> None:
        payload = SimpleNamespace(
            component_id="java",
            display_name="Java JDK",
            provider="temurin",
            provider_name="Eclipse Temurin",
            track=21,
            version="21.0.12+8",
            install_path=Path(
                r"C:\eap\components\java\temurin\21.0.12+8"
            ),
            restorable=True,
        )
        activated: list[tuple[str, Any]] = []
        app = SimpleNamespace(
            available_component_payloads=lambda environment_id: [payload],
            activate_component_payload=lambda environment_id, selected: (
                activated.append((environment_id, selected)) or selected
            ),
        )
        output = StringIO()
        with (
            patch.object(cli_module, "_read_input", return_value="1"),
            redirect_stdout(output),
        ):
            result = cli_module._interactive_activate_component(
                app, "default"
            )

        self.assertTrue(result)
        self.assertEqual([("default", payload)], activated)
        rendered = output.getvalue()
        self.assertIn("No se descargará ningún archivo", rendered)
        self.assertIn("activado en default desde el payload local", rendered)

    def test_component_info_paths_open_explorer_and_notepad(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            component = SimpleNamespace(
                display_name="Node.js",
                information_description="Datos importantes de Node.js.",
                important_paths=[
                    {
                        "displayName": "Caché npm",
                        "base": "profile",
                        "relativePath": "home/.npm",
                        "type": "directory",
                    },
                    {
                        "displayName": "Configuración npm (.npmrc)",
                        "base": "profile",
                        "relativePath": "home/.npmrc",
                        "type": "file",
                    },
                ],
            )
            windows = paths.temp / "Windows"
            explorer = windows / "explorer.exe"
            notepad = windows / "System32" / "notepad.exe"
            explorer.parent.mkdir(parents=True, exist_ok=True)
            notepad.parent.mkdir(parents=True, exist_ok=True)
            explorer.touch()
            notepad.touch()
            app = SimpleNamespace(paths=paths, environments=environments)
            output = StringIO()
            with (
                patch.dict(os.environ, {"SystemRoot": str(windows)}),
                patch.object(
                    cli_module, "_read_input", side_effect=["1", "2", "\x1b"]
                ),
                patch.object(cli_module.subprocess, "Popen") as popen,
                redirect_stdout(output),
            ):
                cli_module._interactive_component_information(
                    app, "default", component
                )

            profile = paths.data / "profiles" / "default"
            self.assertEqual(
                [
                    [str(explorer), str(profile / "home" / ".npm")],
                    [str(notepad), str(profile / "home" / ".npmrc")],
                ],
                [call.args[0] for call in popen.call_args_list],
            )
            self.assertTrue((profile / "home" / ".npm").is_dir())
            self.assertTrue((profile / "home").is_dir())
            rendered = output.getvalue()
            self.assertIn("[1] Caché npm · Carpeta:", rendered)
            self.assertIn(
                "[2] Configuración npm (.npmrc) · Archivo:", rendered
            )
            self.assertIn("Carpeta abierta:", rendered)
            self.assertIn("Archivo abierto:", rendered)

    def test_configuration_actions_use_configured_text_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            environments.create("java11")
            viewer = paths.root / "tools" / "viewer.exe"
            viewer.parent.mkdir()
            viewer.touch()
            paths.config.write_text(
                f"textViewer.executable={viewer}\n",
                encoding="utf-8",
            )
            app = SimpleNamespace(
                paths=paths,
                environments=environments,
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="2"),
                patch.object(cli_module.subprocess, "Popen") as popen,
                redirect_stdout(output),
            ):
                cli_module._interactive_open_general_configuration(app)
                cli_module._interactive_open_profile_configuration(
                    app, "default"
                )

            self.assertEqual(
                [
                    [str(viewer), str(paths.config)],
                    [
                        str(viewer),
                        str(
                            paths.envs
                            / "java11"
                            / "config.properties"
                        ),
                    ],
                ],
                [call.args[0] for call in popen.call_args_list],
            )
            rendered = output.getvalue()
            self.assertIn("[1] default (actual)", rendered)
            self.assertIn("[2] java11", rendered)

    def test_missing_configured_text_viewer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            paths.config.write_text(
                "textViewer.executable=missing-viewer.exe\n",
                encoding="utf-8",
            )
            app = SimpleNamespace(paths=paths)
            with self.assertRaisesRegex(
                ValidationError,
                "textViewer.executable",
            ):
                cli_module._text_viewer_executable(app)

    def test_component_table_includes_repository_and_inactive_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            bundled = java_component(paths.temp / "java.json")
            component = ComponentDefinition(
                bundled.manifest_path,
                bundled.value,
                ComponentCatalogSource(
                    "danielgube",
                    "https://github.com/danielgube/eap-components",
                    "https://example.test/catalog.json",
                    "a" * 40,
                    "github",
                ),
                "components/java.json",
            )
            payload = SimpleNamespace(
                component_id="java",
                display_name="Java JDK",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                install_path=(
                    paths.components / "java" / "temurin" / "21.0.12+8"
                ),
                restorable=True,
            )
            app = SimpleNamespace(
                catalog=SimpleNamespace(
                    component=lambda component_id: component
                ),
                inventory=lambda environment_id: [],
                available_component_payloads=lambda environment_id: [payload],
            )

            entries = cli_module._profile_component_entries(app, "default")
            with patch.object(
                cli_module.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((180, 24)),
            ):
                [(_, rows)] = cli_module._inventory_sections(
                    app,
                    "default",
                    entries,
                    {"updates": []},
                    numbered=True,
                )
            rendered = "\n".join(rows)

            self.assertIn("Repositorio", rendered)
            self.assertIn("Categoría", rendered)
            self.assertIn("Tipo", rendered)
            self.assertIn("Active", rendered)
            self.assertIn("Run", rendered)
            self.assertIn("danielgube", rendered)
            self.assertIn("runtime", rendered)
            self.assertIn("Java JDK · 21.0.12+8", rendered)
            self.assertRegex(
                rendered, r"danielgube\s+\?\s+No\s+\[1i\]"
            )

    def test_component_table_marks_runnable_applications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = dbeaver_component(
                Path(temporary) / "dbeaver.json"
            )
            component.value["category"] = "database-clients"
            launcher = SimpleNamespace(
                id="dbeaver", component_id="dbeaver"
            )
            app = SimpleNamespace(
                catalog=SimpleNamespace(
                    component=lambda component_id: component
                ),
                available_launchers=lambda environment_id: [launcher],
            )
            inventory = [
                {
                    "id": "dbeaver",
                    "provider": "community",
                    "version": "26.2.0",
                    "active": True,
                }
            ]

            with patch.object(
                cli_module.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((180, 24)),
            ):
                [(_, rows)] = cli_module._inventory_sections(
                    app,
                    "default",
                    inventory,
                    {"state": "done", "updates": []},
                    numbered=True,
                )
            rendered = "\n".join(rows)

            self.assertIn("Clientes BBDD", rendered)
            self.assertIn("application", rendered)
            self.assertIn("[1r]", rendered)

    def test_inactive_table_entry_can_activate_its_local_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            component = java_component(paths.temp / "java.json")
            payload = SimpleNamespace(
                component_id="java",
                display_name="Java JDK",
                provider="temurin",
                provider_name="Eclipse Temurin",
                track=21,
                version="21.0.12+8",
                install_path=(
                    paths.components / "java" / "temurin" / "21.0.12+8"
                ),
                restorable=True,
            )
            activated: list[Any] = []
            app = SimpleNamespace(
                paths=paths,
                environments=environments,
                catalog=SimpleNamespace(
                    component=lambda component_id: component
                ),
                available_component_payloads=lambda environment_id: [payload],
                activate_component_payload=lambda environment_id, selected: (
                    activated.append(selected) or selected
                ),
            )
            selected = {
                "id": "java",
                "provider": "temurin",
                "track": 21,
                "version": "21.0.12+8",
                "active": False,
                "_payload": payload,
            }
            status = {"updates": []}
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="1"),
                redirect_stdout(output),
            ):
                returned, changed = cli_module._interactive_component_entry(
                    app, "default", selected, status
                )

            self.assertIs(status, returned)
            self.assertTrue(changed)
            self.assertEqual([payload], activated)
            self.assertIn("Descargado · inactivo", output.getvalue())
            self.assertIn("origen de descarga conservado", output.getvalue())

    def test_install_component_catalog_lists_all_and_opens_active_actions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            java = java_component(paths.temp / "java.json")
            maven = maven_component(paths.temp / "maven.json")
            java.value["category"] = "runtimes"
            java.value["description"] = (
                "Distribuciones OpenJDK portables para Windows"
            )
            maven.value["category"] = "build-tools"
            maven.value["description"] = (
                "Construcción y gestión de dependencias para Java"
            )
            active = {
                "id": "java",
                "provider": "temurin",
                "track": 21,
                "version": "21.0.12+8",
            }
            app = SimpleNamespace(
                catalog=SimpleNamespace(
                    definitions={"java": java, "maven": maven}
                ),
                inventory=lambda environment_id: [active],
                available_component_payloads=lambda environment_id: [],
            )
            status = {
                "state": "cached",
                "updates": [],
                "resolved": [],
                "error": None,
            }
            returned_status = {**status, "opened": "java"}
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="1"),
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((180, 24)),
                ),
                patch.object(
                    cli_module,
                    "_interactive_component_actions",
                    return_value=returned_status,
                ) as actions,
                redirect_stdout(output),
            ):
                result = cli_module._interactive_install_new_component(
                    app, "default", status
                )

            self.assertIs(returned_status, result)
            actions.assert_called_once_with(
                app, "default", active, status
            )
            rendered = output.getvalue()
            self.assertIn("Runtimes (1)", rendered)
            self.assertIn("Herramientas de construcción (1)", rendered)
            self.assertIn("Componente", rendered)
            self.assertIn("Estado", rendered)
            self.assertIn("Descripción", rendered)
            self.assertIn("Fuente", rendered)
            self.assertIn("[1]  Java JDK", rendered)
            self.assertIn("Activo", rendered)
            self.assertIn("21.0.12+8", rendered)
            self.assertIn(
                "Distribuciones OpenJDK portables para Windows", rendered
            )
            self.assertIn("[2]  Apache Maven", rendered)
            self.assertIn("No instalado", rendered)
            self.assertIn(
                "Construcción y gestión de dependencias para Java", rendered
            )
            self.assertIn("bootstrap", rendered)

    def test_empty_install_catalog_offers_refresh_and_lists_results(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            java = java_component(paths.temp / "java.json")
            catalog = SimpleNamespace(definitions={}, sources={})
            refreshed: list[bool] = []

            app = SimpleNamespace(
                catalog=catalog,
                inventory=lambda environment_id: [],
                available_component_payloads=lambda environment_id: [],
            )

            def refresh_catalogs() -> Any:
                refreshed.append(True)
                app.catalog = SimpleNamespace(
                    definitions={"java": java},
                    sources={"official": object()},
                )
                return app.catalog

            app.refresh_component_catalogs = refresh_catalogs
            status = {"state": "cached", "updates": []}
            output = StringIO()
            with (
                patch.object(
                    cli_module, "_read_input", side_effect=["s", "\x1b"]
                ),
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((180, 24)),
                ),
                redirect_stdout(output),
            ):
                result = cli_module._interactive_install_new_component(
                    app, "default", status
                )

            self.assertIs(status, result)
            self.assertEqual([True], refreshed)
            rendered = output.getvalue()
            self.assertIn(
                "No hay componentes instalables en el catálogo", rendered
            )
            self.assertIn(
                "Puede actualizar ahora los catálogos configurados", rendered
            )
            self.assertIn("Catálogos actualizados: 1 componente(s)", rendered)
            self.assertIn("Java JDK", rendered)

    def test_install_component_table_uses_detailed_compact_layout_when_narrow(
        self,
    ) -> None:
        rows = [
            [
                "[1]",
                "Apache Maven",
                "No instalado",
                "--",
                "Construcción y gestión de dependencias para Java",
                "danielgube",
            ]
        ]
        with patch.object(
            cli_module.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((60, 24)),
        ):
            rendered = cli_module._install_component_table_rows(rows)

        self.assertEqual(
            [
                "[1] Apache Maven · No instalado",
                "    Construcción y gestión de dependencias para Java",
                "    Fuente: danielgube",
            ],
            rendered,
        )

    def test_page_start_clears_terminal_and_renders_breadcrumb(self) -> None:
        output = TtyStringIO()
        with (
            patch.object(cli_module, "_INTERACTIVE_ACTIVE", True),
            redirect_stdout(output),
        ):
            cli_module._start_page("Inicio > Catálogo > Node.js")

        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("\x1b[2J\x1b[H"))
        self.assertIn("Navegación", rendered)
        self.assertIn("Inicio > Catálogo > Node.js", rendered)

    def test_page_start_keeps_redirected_output_free_of_clear_codes(
        self,
    ) -> None:
        output = StringIO()
        with (
            patch.object(cli_module, "_INTERACTIVE_ACTIVE", True),
            redirect_stdout(output),
        ):
            cli_module._start_page("Inicio")

        rendered = output.getvalue()
        self.assertNotIn("\x1b[2J", rendered)
        self.assertIn("Inicio", rendered)

    def test_interactive_results_and_errors_wait_for_acknowledgement(
        self,
    ) -> None:
        error_output = StringIO()
        with (
            patch.object(cli_module, "_INTERACTIVE_ACTIVE", True),
            patch.object(
                cli_module, "_pause_for_acknowledgement"
            ) as pause,
            redirect_stderr(error_output),
        ):
            cli_module._pause_after_result()
            cli_module._print_error("fallo interactivo")

        self.assertEqual(2, pause.call_count)
        self.assertIn("ERROR: fallo interactivo", error_output.getvalue())

    def test_panel_color_is_added_after_width_calculation(self) -> None:
        output = TtyStringIO()
        with (
            patch.object(cli_module, "_COLOR_ENABLED", True),
            patch.object(
                cli_module.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((60, 24)),
            ),
            redirect_stdout(output),
        ):
            cli_module._print_panel(
                "Integraciones con el Host",
                [
                    (
                        "",
                        [
                            "Nombre: java11",
                            "Firefox: OK",
                            "[W] Workspace",
                            "[CC] Custom Commands",
                            "[10] Gestionar",
                            "Estado: KO",
                        ],
                    )
                ],
            )

        rendered = output.getvalue()
        self.assertIn("\x1b[1;36m", rendered)
        self.assertIn("\x1b[32mOK\x1b[0m", rendered)
        self.assertIn(
            "Nombre: \x1b[32mjava11\x1b[0m",
            rendered,
        )
        self.assertIn("\x1b[31mKO\x1b[0m", rendered)
        self.assertIn("\x1b[33m[W]\x1b[0m", rendered)
        self.assertIn("\x1b[33m[CC]\x1b[0m", rendered)
        self.assertIn("\x1b[33m[10]\x1b[0m", rendered)
        visible = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        self.assertTrue(
            all(len(line) == 60 for line in visible.splitlines() if line)
        )

    def test_color_does_not_change_component_table_layout(self) -> None:
        components = [
            [
                "[1]",
                "Java JDK · 21",
                "Runtimes",
                "runtime",
                "Eclipse Temurin",
                "danielgube",
                "25",
                "Sí",
                "[1r]",
                "[1i]",
            ],
            [
                "[2]",
                "Node.js · 24",
                "Runtimes",
                "runtime",
                "Node.js Foundation",
                "danielgube",
                "",
                "No",
                "",
                "[2i]",
            ],
        ]

        def render(color_enabled: bool) -> str:
            output = TtyStringIO()
            with (
                patch.object(
                    cli_module, "_COLOR_ENABLED", color_enabled
                ),
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((180, 24)),
                ),
                redirect_stdout(output),
            ):
                rows = cli_module._component_table_rows(components)
                cli_module._print_panel("Componentes (2)", [("", rows)])
            return output.getvalue()

        plain = render(False)
        colored = render(True)
        visible = re.sub(r"\x1b\[[0-9;]*m", "", colored)
        self.assertEqual(plain, visible)
        self.assertNotIn("…", visible)
        self.assertIn("Java JDK · 21", visible)
        self.assertIn("Node.js · 24", visible)
        self.assertIn("\x1b[33m[1]\x1b[0m", colored)
        self.assertIn("\x1b[33m[1r]\x1b[0m", colored)
        self.assertIn("\x1b[33m[1i]\x1b[0m", colored)

    def test_panel_color_is_suppressed_when_output_is_redirected(self) -> None:
        output = StringIO()
        with (
            patch.object(cli_module, "_COLOR_ENABLED", True),
            redirect_stdout(output),
        ):
            cli_module._print_panel("Estado", [("", ["Firefox: OK"])])
        self.assertNotIn("\x1b[", output.getvalue())

    def test_profile_management_menu_uses_profile_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create(
                "default",
                workspace_id="project",
                data_profile_id="developer",
            )
            app = SimpleNamespace(environments=environments)
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="\x1b"),
                redirect_stdout(output),
            ):
                selected = cli_module._interactive_manage_environments(
                    app, "default"
                )

            self.assertEqual("default", selected)
            rendered = output.getvalue()
            self.assertIn("Gestionar profile", rendered)
            self.assertIn("Workspace: project", rendered)
            self.assertIn("Datos: developer", rendered)
            self.assertIn(
                "[4] Cambiar datos del profile actual", rendered
            )
            self.assertIn("[5] Exportar profile", rendered)
            self.assertIn("[6] Importar profile", rendered)
            self.assertIn("[8] Duplicar profile actual", rendered)
            self.assertIn("[9] Eliminar profile", rendered)

    def test_interactive_duplicate_creates_and_selects_based_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create(
                "default",
                workspace_id="project",
                data_profile_id="developer",
            )
            app = SimpleNamespace(
                environments=environments,
                duplicate_profile=lambda source, target: (
                    environments.duplicate(source, target)
                ),
            )
            output = StringIO()
            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["java11", "s"],
                ),
                redirect_stdout(output),
            ):
                selected = cli_module._interactive_duplicate_environment(
                    app, "default"
                )

            self.assertEqual("java11", selected)
            duplicated = environments.read_desired("java11")
            self.assertEqual("java11", duplicated["workspace"])
            self.assertEqual("developer", duplicated["dataProfile"])
            rendered = output.getvalue()
            self.assertIn("Configuración privada", rendered)
            self.assertIn("Workspace nuevo: java11", rendered)

    def test_interactive_delete_removes_definition_but_not_shared_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create(
                "default",
                workspace_id="project",
                data_profile_id="developer",
            )
            environments.create(
                "java11",
                workspace_id="project",
                data_profile_id="developer",
            )
            app = SimpleNamespace(
                environments=environments,
                delete_profile=lambda profile_id: environments.delete(
                    profile_id
                ),
            )
            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["2", "s"],
                ),
                redirect_stdout(StringIO()),
            ):
                selected = cli_module._interactive_delete_environment(
                    app, "default"
                )

            self.assertEqual("default", selected)
            self.assertEqual(["default"], environments.list())
            self.assertTrue((paths.workspaces / "project").is_dir())
            self.assertTrue(
                (paths.data / "profiles" / "developer").is_dir()
            )

    def test_advanced_menu_contains_and_routes_maintenance_actions(self) -> None:
        output = StringIO()
        with (
            patch.object(
                cli_module,
                "_read_input",
                side_effect=["3", "4", "5", "6", "7", "8", "\x1b"],
            ),
            patch.object(cli_module, "_interactive_doctor") as doctor,
            patch.object(
                cli_module, "_interactive_clean_temporary_storage"
            ) as clean,
            patch.object(
                cli_module, "_interactive_host_integrations"
            ) as integrations,
            patch.object(
                cli_module, "_interactive_update_eap", return_value=False
            ) as update_eap,
            patch.object(
                cli_module, "_interactive_open_general_configuration"
            ) as general_configuration,
            patch.object(
                cli_module, "_interactive_open_profile_configuration"
            ) as profile_configuration,
            redirect_stdout(output),
        ):
            selected = cli_module._interactive_advanced_options(
                SimpleNamespace(), "default"
            )
        self.assertEqual("default", selected)
        doctor.assert_called_once()
        clean.assert_called_once()
        integrations.assert_called_once_with(unittest.mock.ANY, "default")
        update_eap.assert_called_once()
        general_configuration.assert_called_once()
        profile_configuration.assert_called_once_with(
            unittest.mock.ANY, "default"
        )
        rendered = output.getvalue()
        self.assertIn("[1] Exportar todos los profiles", rendered)
        self.assertIn("[2] Importar todos los profiles", rendered)
        self.assertIn("[3] Diagnóstico", rendered)
        self.assertIn("[4] Limpiar temporales", rendered)
        self.assertIn("[5] Integraciones con el Host", rendered)
        self.assertIn("[6] Actualizar EAP", rendered)
        self.assertIn("[7] Abrir configuración general", rendered)
        self.assertIn("[8] Abrir configuración de un profile", rendered)
        self.assertIn("[0] Exportar EAP", rendered)

    def test_cli_installs_public_eap_update(self) -> None:
        release = SimpleNamespace(published_at="2026-08-28T12:00:00Z")
        update = SimpleNamespace(
            current_version="0.19.0",
            latest_version="0.20.0",
            update_available=True,
            release=release,
        )
        result = EapUpdateResult(
            previous_version="0.19.0",
            version="0.20.0",
            archive=Path(r"C:\eap\temp\eap-0.20.0.zip"),
            sha256="a" * 64,
        )
        installed: list[Any] = []
        app = SimpleNamespace(
            check_eap_update=lambda: update,
            install_eap_update=lambda selected: (
                installed.append(selected) or result
            ),
        )
        arguments = cli_module.build_parser().parse_args(
            ["update", "--yes"]
        )
        output = StringIO()
        with redirect_stdout(output):
            code = cli_module.dispatch(app, arguments)

        self.assertEqual(0, code)
        self.assertEqual([update], installed)
        self.assertIn("0.19.0 -> 0.20.0", output.getvalue())
        self.assertIn("vuelva a abrir EAP", output.getvalue())

    def test_cli_release_routes_to_the_administrative_publisher(self) -> None:
        result = EapReleaseResult(
            version="0.19.0",
            tag="v0.19.0",
            archive=Path(r"C:\eap\exports\releases\eap-0.19.0.zip"),
            sha256="a" * 64,
            release_url="https://github.com/danielgube/eap/releases/v0.19.0",
            created=True,
        )
        app = SimpleNamespace(publish_eap_release=lambda: result)
        arguments = cli_module.build_parser().parse_args(["release"])
        output = StringIO()

        with redirect_stdout(output):
            code = cli_module.dispatch(app, arguments)

        self.assertEqual(0, code)
        self.assertIn("Release publicada: v0.19.0", output.getvalue())
        self.assertIn(result.release_url, output.getvalue())

    def test_interactive_error_is_red_and_waits_for_one_key(self) -> None:
        output = TtyStringIO()
        error_output = TtyStringIO()
        pressed: list[str] = []
        keyboard = SimpleNamespace(
            getwch=lambda: pressed.append("x") or "x"
        )
        with (
            patch.object(cli_module, "_COLOR_ENABLED", True),
            patch.object(cli_module, "msvcrt", keyboard),
            patch.object(cli_module.sys, "stdin", TtyStringIO()),
            redirect_stdout(output),
            redirect_stderr(error_output),
        ):
            cli_module._print_error(
                "No se pudo eliminar el temporal", pause=True
            )

        self.assertEqual(["x"], pressed)
        self.assertIn(
            "\x1b[31mERROR: No se pudo eliminar el temporal\x1b[0m",
            error_output.getvalue(),
        )
        self.assertIn("Pulse una tecla para continuar...", output.getvalue())

    def test_bulk_export_uses_default_options_and_continues_on_error(self) -> None:
        calls: list[tuple[str, str, bool, bool, bool]] = []

        def export_profile(
            source: str,
            name: str,
            include_components: bool,
            include_configuration: bool,
            include_custom_commands: bool,
        ) -> Any:
            calls.append(
                (
                    source,
                    name,
                    include_components,
                    include_configuration,
                    include_custom_commands,
                )
            )
            if source == "alpha":
                raise ValidationError("archive exists")
            return SimpleNamespace(archive=Path(f"{name}.7z"))

        app = SimpleNamespace(
            environments=SimpleNamespace(list=lambda: ["alpha", "beta"]),
            export_environment=export_profile,
        )
        exported, failures = cli_module._export_all_profiles(app)

        self.assertEqual(
            [
                ("alpha", "alpha", False, False, False),
                ("beta", "beta", False, False, False),
            ],
            calls,
        )
        self.assertEqual(["beta"], [item[0] for item in exported])
        self.assertEqual([("alpha", "archive exists")], failures)

    def test_interactive_profile_export_can_include_custom_commands(
        self,
    ) -> None:
        calls: list[tuple[str, str, dict[str, bool]]] = []
        result = SimpleNamespace(
            archive=Path("hbx.7z"),
            configuration_included=False,
            custom_commands_included=True,
            sha256="a" * 64,
        )
        app = SimpleNamespace(
            export_environment=lambda source, name, **options: (
                calls.append((source, name, options)) or result
            )
        )
        output = StringIO()
        with (
            patch.object(
                cli_module,
                "_read_input",
                side_effect=["hbx", "n", "n", "s"],
            ) as read_input,
            redirect_stdout(output),
        ):
            cli_module._interactive_export_environment(app, "default")

        self.assertEqual(
            [
                (
                    "default",
                    "hbx",
                    {
                        "include_components": False,
                        "include_configuration": False,
                        "include_custom_commands": True,
                    },
                )
            ],
            calls,
        )
        rendered = output.getvalue()
        self.assertIn(
            "¿Incluir custom-commands del profile?",
            read_input.call_args_list[3].args[0],
        )
        self.assertIn("Custom Commands incluidos: sí", rendered)

    def test_bulk_import_deletes_only_successful_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            successful = paths.envs / "alpha.7z"
            failed = paths.envs / "beta.7z"
            successful.write_bytes(b"alpha")
            failed.write_bytes(b"beta")

            def import_profile(archive: Path) -> Any:
                if archive == failed:
                    raise ValidationError("invalid package")
                return SimpleNamespace(environment_id="alpha")

            app = SimpleNamespace(
                paths=paths,
                import_environment=import_profile,
            )
            imported, failures = cli_module._import_all_profiles(app)

            self.assertEqual(1, len(imported))
            self.assertTrue(imported[0][2])
            self.assertFalse(successful.exists())
            self.assertTrue(failed.is_file())
            self.assertEqual([("beta.7z", "invalid package")], failures)

    def test_cli_disables_component_with_profile_and_env_aliases(self) -> None:
        disabled: list[tuple[str, str]] = []
        component = SimpleNamespace(display_name="Git")
        app = SimpleNamespace(
            environments=SimpleNamespace(
                read_desired=lambda profile_id: {"id": profile_id}
            ),
            inventory=lambda profile_id: [
                {"id": "git", "version": "2.55.0.5"}
            ],
            catalog=SimpleNamespace(
                component=lambda component_id: component
            ),
            disable_component=lambda profile_id, component_id: (
                disabled.append((profile_id, component_id))
            ),
        )
        parser = cli_module.build_parser()
        arguments = parser.parse_args(
            [
                "component",
                "disable",
                "git",
                "--profile",
                "default",
                "--yes",
            ]
        )
        with redirect_stdout(StringIO()):
            self.assertEqual(0, cli_module.dispatch(app, arguments))
        self.assertEqual([("default", "git")], disabled)

        compatible = parser.parse_args(
            ["component", "disable", "git", "--env", "legacy", "--yes"]
        )
        self.assertEqual("legacy", compatible.environment)

    def test_cli_uninstalls_component_and_reports_removed_payload(self) -> None:
        uninstalled: list[tuple[str, str]] = []
        component = SimpleNamespace(
            display_name="Git",
            is_external=False,
        )
        result = SimpleNamespace(
            payload_removed=True,
            shared_profiles=(),
            residual_path=None,
        )
        app = SimpleNamespace(
            environments=SimpleNamespace(
                read_desired=lambda profile_id: {"id": profile_id}
            ),
            inventory=lambda profile_id: [
                {"id": "git", "version": "2.55.0.5"}
            ],
            catalog=SimpleNamespace(
                component=lambda component_id: component
            ),
            uninstall_component=lambda profile_id, component_id: (
                uninstalled.append((profile_id, component_id)) or result
            ),
        )
        arguments = cli_module.build_parser().parse_args(
            [
                "component",
                "uninstall",
                "git",
                "--profile",
                "default",
                "--yes",
            ]
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, cli_module.dispatch(app, arguments))

        self.assertEqual([("default", "git")], uninstalled)
        self.assertIn("payload eliminado", output.getvalue())
        self.assertIn("datos personales", output.getvalue())

    def test_cli_cleans_temporary_storage(self) -> None:
        app = SimpleNamespace(
            paths=SimpleNamespace(
                temp=Path(r"C:\eap\temp"), logs=Path(r"C:\eap\logs")
            ),
            temporary_storage_usage=lambda: SimpleNamespace(
                bytes=1536, files=2
            ),
            clean_temporary_storage=lambda: SimpleNamespace(
                bytes_removed=1536, files_removed=2
            ),
        )
        arguments = cli_module.build_parser().parse_args(
            ["tool", "clean-temp", "--yes"]
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, cli_module.dispatch(app, arguments))
        self.assertIn("1.5 KiB", output.getvalue())
        self.assertIn("Temporales eliminados", output.getvalue())

    def test_cli_creates_and_reassigns_named_profile_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            app = SimpleNamespace(paths=paths, environments=environments)
            parser = cli_module.build_parser()

            create = parser.parse_args(
                [
                    "profile",
                    "create",
                    "java11",
                    "--workspace",
                    "legacy",
                    "--data",
                    "developer",
                ]
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(0, cli_module.dispatch(app, create))
            desired = environments.read_desired("java11")
            self.assertEqual("legacy", desired["workspace"])
            self.assertEqual("developer", desired["dataProfile"])

            reassign = parser.parse_args(
                [
                    "profile",
                    "data",
                    "isolated",
                    "--profile",
                    "java11",
                ]
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(0, cli_module.dispatch(app, reassign))
            self.assertEqual(
                "isolated",
                environments.read_desired("java11")["dataProfile"],
            )
            compatible = parser.parse_args(
                [
                    "env",
                    "data-profile",
                    "developer",
                    "--env",
                    "java11",
                ]
            )
            self.assertEqual("env", compatible.command)
            self.assertEqual("data-profile", compatible.env_command)
            self.assertEqual("java11", compatible.environment)

    def test_cli_duplicates_and_deletes_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create(
                "default",
                workspace_id="project",
                data_profile_id="developer",
            )
            app = SimpleNamespace(
                environments=environments,
                duplicate_profile=lambda source, target: (
                    environments.duplicate(source, target)
                ),
                delete_profile=lambda profile_id: environments.delete(
                    profile_id
                ),
            )
            parser = cli_module.build_parser()
            duplicate = parser.parse_args(
                [
                    "profile",
                    "duplicate",
                    "java11",
                    "--profile",
                    "default",
                ]
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(0, cli_module.dispatch(app, duplicate))
            self.assertEqual(["default", "java11"], environments.list())
            self.assertEqual(
                "java11",
                environments.read_desired("java11")["workspace"],
            )
            self.assertTrue((paths.workspaces / "java11").is_dir())

            delete = parser.parse_args(
                ["profile", "delete", "java11", "--yes"]
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(0, cli_module.dispatch(app, delete))
            self.assertEqual(["default"], environments.list())
            self.assertTrue((paths.workspaces / "project").is_dir())
            self.assertTrue(
                (paths.data / "profiles" / "developer").is_dir()
            )

    def test_cli_exposes_bulk_profile_commands(self) -> None:
        parser = cli_module.build_parser()
        self.assertEqual(
            "export-all",
            parser.parse_args(["profile", "export-all"]).env_command,
        )
        self.assertEqual(
            "import-all",
            parser.parse_args(["profile", "import-all"]).env_command,
        )
        export = parser.parse_args(
            [
                "profile",
                "export",
                "portable",
                "--include-custom-commands",
            ]
        )
        self.assertTrue(export.include_custom_commands)

    def test_interactive_profile_creation_can_reuse_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            app = SimpleNamespace(environments=environments)
            output = StringIO()

            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["java21", "modern", "2", "1"],
                ),
                redirect_stdout(output),
            ):
                selected = cli_module._interactive_create_environment(
                    app, "default"
                )

            self.assertEqual("java21", selected)
            desired = environments.read_desired("java21")
            self.assertEqual("modern", desired["workspace"])
            self.assertEqual("default", desired["dataProfile"])
            self.assertIn("Reutilizar datos existentes", output.getvalue())
            self.assertIn("default · default", output.getvalue())

    def test_interactive_profile_creation_can_create_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            app = SimpleNamespace(environments=environments)

            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["java11", "legacy", "1", "developer"],
                ),
                redirect_stdout(StringIO()),
            ):
                selected = cli_module._interactive_create_environment(
                    app, "default"
                )

            self.assertEqual("java11", selected)
            desired = environments.read_desired("java11")
            self.assertEqual("legacy", desired["workspace"])
            self.assertEqual("developer", desired["dataProfile"])
            self.assertTrue(
                (paths.data / "profiles" / "developer" / "home").is_dir()
            )

    def test_interactive_import_uses_envs_inbox_and_removes_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            first = paths.envs / "alpha.7z"
            second = paths.envs / "zeta.7z"
            first.write_bytes(b"first package")
            second.write_bytes(b"second package")
            imported: list[Path] = []
            result = SimpleNamespace(
                environment_id="alpha",
                configuration_included=False,
                components_missing=0,
            )
            app = SimpleNamespace(
                paths=paths,
                import_environment=lambda archive: (
                    imported.append(archive) or result
                ),
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="1"),
                redirect_stdout(output),
            ):
                selected = cli_module._interactive_import_environment(app)

            self.assertEqual("alpha", selected)
            self.assertEqual([first.resolve()], imported)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            rendered = output.getvalue()
            self.assertLess(rendered.index("alpha.7z"), rendered.index("zeta.7z"))
            self.assertIn("Paquete importado y eliminado", rendered)

    def test_interactive_import_keeps_package_when_import_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            archive = paths.envs / "broken.7z"
            archive.write_bytes(b"not a valid package")

            def fail_import(selected: Path) -> None:
                raise ValidationError(f"Paquete no válido: {selected}")

            app = SimpleNamespace(
                paths=paths,
                import_environment=fail_import,
            )
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value="1"),
                self.assertRaises(ValidationError),
                redirect_stdout(output),
            ):
                cli_module._interactive_import_environment(app)

            self.assertTrue(archive.exists())

    def test_interactive_import_reports_empty_envs_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            app = SimpleNamespace(paths=paths)
            output = StringIO()
            with (
                patch.object(cli_module, "_read_input", return_value=""),
                redirect_stdout(output),
            ):
                selected = cli_module._interactive_import_environment(app)

            self.assertIsNone(selected)
            self.assertIn("No hay paquetes .7z", output.getvalue())
            self.assertIn("Copie los paquetes directamente en:", output.getvalue())

    def test_first_interactive_run_bootstraps_managed_terminal(self) -> None:
        created: list[str] = []
        started: list[str] = []
        environments = SimpleNamespace(
            selected=lambda configured_default: None,
            list=lambda: [],
            create=lambda environment_id: created.append(environment_id),
            read_desired=lambda environment_id: {
                "id": environment_id,
                "workspace": environment_id,
            },
        )
        app = SimpleNamespace(
            environments=environments,
            settings=SimpleNamespace(
                get=lambda key: "default"
            ),
            start_managed_terminal=lambda environment_id: (
                started.append(environment_id)
                or SimpleNamespace(process_id=4321)
            ),
        )
        output = StringIO()
        with (
            patch.object(cli_module, "EapApplication", return_value=app),
            patch.object(
                cli_module.sys,
                "stdin",
                SimpleNamespace(isatty=lambda: True),
            ),
            patch.dict(cli_module.os.environ, {}, clear=True),
            patch.object(cli_module, "interactive") as interactive_menu,
            redirect_stdout(output),
        ):
            self.assertEqual(0, cli_module.main([]))
        self.assertEqual(["default"], created)
        self.assertEqual(["default"], started)
        interactive_menu.assert_not_called()
        self.assertIn("Primera ejecución", output.getvalue())

    def test_activation_notice_does_not_request_eap_restart(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            cli_module._print_activation_notice("default")
        rendered = output.getvalue()
        self.assertIn("Abra una pestaña nueva con +", rendered)
        self.assertIn("No es necesario reiniciar ni cerrar EAP", rendered)

    def test_component_flow_shows_breadcrumb_and_active_java(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            component = java_component(paths.temp / "java.json")
            inventory = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "version": "21.0.12+8",
                }
            ]
            app = SimpleNamespace(
                paths=paths,
                environments=environments,
                inventory=lambda environment_id: inventory,
            )
            output = StringIO()
            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["1", "\x1b", "\x1b"],
                ),
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((180, 24)),
                ),
                redirect_stdout(output),
            ):
                installed = cli_module._interactive_install_component(
                    app, "default", component
                )
            rendered = output.getvalue()
            self.assertFalse(installed)
            self.assertIn("Catálogo > Java JDK", rendered)
            self.assertIn(
                "Eclipse Temurin · activo: Java 21 LTS · 21.0.12+8",
                rendered,
            )
            self.assertIn(
                "Catálogo > Java JDK > Eclipse Temurin",
                rendered,
            )
            self.assertIn(
                "Java 21 LTS · activo · 21.0.12+8",
                rendered,
            )
            self.assertIn("Descripción", rendered)
            self.assertIn("Rutas importantes", rendered)
            self.assertIn(
                "Home del usuario Java: "
                + str(paths.data / "profiles" / "default" / "home"),
                rendered,
            )
        self.assertIn("Bootstrap · incluida con EAP", rendered)

    def test_component_flow_shows_repository_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            environments = EnvironmentStore(paths)
            environments.create("default")
            bundled = java_component(paths.temp / "java.json")
            component = ComponentDefinition(
                bundled.manifest_path,
                bundled.value,
                ComponentCatalogSource(
                    "empresa",
                    "https://github.com/empresa/eap-components",
                    "https://example.test/catalog.json",
                    "a" * 40,
                    "github",
                ),
                "components/java.json",
            )
            app = SimpleNamespace(
                paths=paths,
                environments=environments,
                inventory=lambda environment_id: [],
            )
            output = StringIO()
            with (
                patch.object(
                    cli_module, "_read_input", return_value="\x1b"
                ),
                redirect_stdout(output),
            ):
                installed = cli_module._interactive_install_component(
                    app, "default", component
                )

            rendered = output.getvalue()
            self.assertFalse(installed)
            self.assertIn("Repositorio: empresa", rendered)
            self.assertIn(
                "URL: https://github.com/empresa/eap-components",
                rendered,
            )
            self.assertIn("Revisión: " + "a" * 40, rendered)

    def test_interactive_component_repositories_adds_refreshes_and_removes(
        self,
    ) -> None:
        sources = [
            {
                "id": "official",
                "repositoryUrl": "https://github.com/example/official",
                "catalogUrl": "https://example.test/official/catalog.json",
                "sourceType": "github",
                "revision": "b" * 40,
            }
        ]
        added: list[tuple[str, str]] = []
        removed: list[str] = []
        refreshed: list[bool] = []

        def add_repository(source_id: str, url: str) -> None:
            added.append((source_id, url))
            sources.append(
                {
                    "id": source_id,
                    "repositoryUrl": url,
                    "catalogUrl": url + "/catalog.json",
                    "sourceType": "github",
                    "revision": None,
                }
            )

        def remove_repository(source_id: str) -> None:
            removed.append(source_id)
            sources[:] = [
                source for source in sources if source["id"] != source_id
            ]

        app = SimpleNamespace(
            component_repositories=SimpleNamespace(
                cached_sources=lambda: list(sources)
            ),
            add_component_repository=add_repository,
            remove_component_repository=remove_repository,
            refresh_component_catalogs=lambda: (
                refreshed.append(True)
                or SimpleNamespace(
                    definitions={"java": object()},
                    sources={"official": object()},
                )
            ),
        )
        output = StringIO()
        with (
            patch.object(
                cli_module,
                "_read_input",
                side_effect=[
                    "1",
                    "empresa",
                    "https://github.com/empresa/eap-components",
                    "s",
                    "3",
                    "2",
                    "2",
                    "s",
                    "\x1b",
                ],
            ),
            redirect_stdout(output),
        ):
            cli_module._interactive_component_repositories(app)

        self.assertEqual(
            [
                (
                    "empresa",
                    "https://github.com/empresa/eap-components",
                )
            ],
            added,
        )
        self.assertEqual([True], refreshed)
        self.assertEqual(["empresa"], removed)
        rendered = output.getvalue()
        self.assertIn("Catálogo de bootstrap", rendered)
        self.assertIn("revisión: " + "b" * 40, rendered)
        self.assertIn("[3] Actualizar catálogos", rendered)
        self.assertIn("Catálogos actualizados: 1 componente(s)", rendered)

    def test_component_actions_can_disable_component_in_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component = java_component(Path(temporary) / "java.json")
            active = {
                "id": "java",
                "provider": "temurin",
                "track": 21,
                "version": "21.0.12+8",
            }
            disabled: list[tuple[str, str]] = []
            app = SimpleNamespace(
                catalog=SimpleNamespace(
                    component=lambda component_id: component
                ),
                inventory=lambda environment_id: [active],
                missing_components=lambda environment_id: [],
                available_launchers=lambda environment_id: [],
                disable_component=lambda environment_id, component_id: (
                    disabled.append((environment_id, component_id))
                ),
            )
            status = {
                "state": "cached",
                "updates": [],
                "resolved": [],
                "error": None,
                "checked": True,
            }
            output = StringIO()
            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["3", "s"],
                ),
                redirect_stdout(output),
            ):
                result = cli_module._interactive_component_actions(
                    app, "default", active, status
                )

            self.assertIs(status, result)
            self.assertEqual([("default", "java")], disabled)
            self.assertIn("Desactivar en este profile", output.getvalue())
            self.assertIn(
                "El payload global y sus datos se conservarán",
                output.getvalue(),
            )

    def test_raw_console_escape_is_returned_immediately(self) -> None:
        keys = iter(["\x1b"])
        keyboard = SimpleNamespace(getwch=lambda: next(keys))
        terminal_input = SimpleNamespace(isatty=lambda: True)
        output = StringIO()
        with (
            patch.object(cli_module, "msvcrt", keyboard),
            patch.object(cli_module.sys, "stdin", terminal_input),
            redirect_stdout(output),
        ):
            self.assertEqual("\x1b", cli_module._read_input("> "))

    def test_escape_exits_main_screen(self) -> None:
        app = SimpleNamespace()
        status = {
            "state": "cached",
            "updates": [],
            "resolved": [],
            "error": None,
        }
        with (
            patch.object(
                cli_module,
                "_ensure_interactive_environment",
                return_value="default",
            ),
            patch.object(
                cli_module,
                "_initial_update_status",
                return_value=status,
            ),
            patch.object(cli_module, "_render_main_dashboard"),
            patch.object(cli_module, "_read_input", return_value="\x1b"),
            patch.object(cli_module, "console_title") as title,
            patch.object(cli_module, "set_console_title") as set_title,
        ):
            self.assertEqual(0, cli_module.interactive(app))
        title.assert_called_once_with("EAP")
        set_title.assert_called_once_with("EAP (default)")
        self.assertTrue(_is_escape("\x1b"))
        self.assertTrue(_is_escape("esc"))

    def test_main_screen_routes_context_shortcuts_and_components(self) -> None:
        status = {
            "state": "cached",
            "updates": [],
            "resolved": [],
            "error": None,
        }
        selected = {"id": "java"}
        component = SimpleNamespace(id="java")
        custom_commands_path = Path("custom-commands")
        profile_path = Path("profile")
        app = SimpleNamespace(
            environments=SimpleNamespace(
                custom_commands_path=lambda environment_id: (
                    custom_commands_path
                ),
                ensure_profile=lambda environment_id: profile_path,
            ),
            inventory=lambda environment_id: [selected],
            catalog=SimpleNamespace(
                component=lambda component_id: component
            ),
        )
        with (
            patch.object(
                cli_module,
                "_ensure_interactive_environment",
                return_value="default",
            ),
            patch.object(cli_module, "_interactive_restore_missing"),
            patch.object(
                cli_module,
                "_initial_update_status",
                return_value=status,
            ),
            patch.object(cli_module, "_render_main_dashboard"),
            patch.object(
                cli_module,
                "_read_input",
                side_effect=[
                    "w",
                    "d",
                    "t",
                    "h",
                    "cc",
                    "c",
                    "p",
                    "1r",
                    "1i",
                    "1",
                    "m",
                    "\x1b",
                ],
            ),
            patch.object(
                cli_module, "_interactive_change_workspace"
            ) as workspace,
            patch.object(
                cli_module, "_interactive_change_data_profile"
            ) as data_profile,
            patch.object(
                cli_module, "_interactive_clean_temporary_storage"
            ) as temporary_storage,
            patch.object(
                cli_module, "_open_component_information_path"
            ) as open_path,
            patch.object(
                cli_module,
                "_interactive_catalog",
                return_value=status,
            ) as catalog,
            patch.object(cli_module, "_interactive_pocketools") as pocketools,
            patch.object(
                cli_module,
                "_ordered_inventory",
                return_value=[selected],
            ),
            patch.object(
                cli_module,
                "_interactive_component_actions",
                return_value=status,
            ) as component_actions,
            patch.object(
                cli_module,
                "_interactive_launch_component",
            ) as launch_component,
            patch.object(
                cli_module,
                "_interactive_component_information",
            ) as component_information,
            patch.object(
                cli_module,
                "_interactive_manage_environments",
                return_value="default",
            ) as manage,
            patch.object(cli_module, "console_title"),
            patch.object(cli_module, "set_console_title"),
        ):
            self.assertEqual(0, cli_module.interactive(app))

        workspace.assert_called_once_with(app, "default")
        data_profile.assert_called_once_with(app, "default")
        temporary_storage.assert_called_once_with(app)
        self.assertEqual(
            [
                (
                    app,
                    "Home del profile",
                    profile_path / "home",
                    "directory",
                ),
                (
                    app,
                    "Custom Commands",
                    custom_commands_path,
                    "directory",
                ),
            ],
            [call.args for call in open_path.call_args_list],
        )
        catalog.assert_called_once_with(app, "default", status)
        pocketools.assert_called_once_with(app, "default")
        component_actions.assert_called_once_with(
            app, "default", selected, status
        )
        component_information.assert_called_once_with(
            app, "default", component
        )
        launch_component.assert_called_once_with(app, "default", "java")
        manage.assert_called_once_with(app, "default")

    def test_catalog_routes_component_run_shortcut(self) -> None:
        status = {
            "state": "cached",
            "updates": [],
            "resolved": [],
            "error": None,
        }
        selected = {"id": "dbeaver"}
        app = SimpleNamespace()
        output = StringIO()
        with (
            patch.object(
                cli_module,
                "_profile_component_entries",
                return_value=[selected],
            ),
            patch.object(
                cli_module, "_ordered_inventory", return_value=[selected]
            ),
            patch.object(cli_module, "_missing_components", return_value=[]),
            patch.object(
                cli_module,
                "_inventory_sections",
                return_value=[("Componentes", ["[1] DBeaver"])],
            ),
            patch.object(
                cli_module, "_read_input", side_effect=["1r", "\x1b"]
            ),
            patch.object(
                cli_module, "_interactive_launch_component"
            ) as launch_component,
            redirect_stdout(output),
        ):
            result = cli_module._interactive_catalog(
                app, "default", status
            )

        self.assertIs(status, result)
        launch_component.assert_called_once_with(
            app, "default", "dbeaver"
        )

    def test_managed_menu_escape_enters_the_environment_cmd(self) -> None:
        calls: list[tuple[str, str]] = []
        app = SimpleNamespace(
            open_shell=lambda environment_id, shell_type: (
                calls.append((environment_id, shell_type)) or 0
            )
        )
        status = {
            "state": "cached",
            "updates": [],
            "resolved": [],
            "error": None,
        }
        with (
            patch.object(
                cli_module,
                "_ensure_interactive_environment",
                return_value="default",
            ),
            patch.object(
                cli_module,
                "_initial_update_status",
                return_value=status,
            ),
            patch.object(cli_module, "_render_main_dashboard"),
            patch.object(cli_module, "_read_input", return_value="\x1b"),
            patch.object(cli_module, "console_title"),
            patch.object(cli_module, "set_console_title"),
        ):
            self.assertEqual(
                0, cli_module.interactive(app, shell_on_exit=True)
            )
        self.assertEqual([("default", "cmd")], calls)

    def test_catalog_centralizes_application_actions(self) -> None:
        active = {
            "id": "dbeaver",
            "provider": "community",
            "track": "26.1",
            "version": "26.1.5",
            "installPath": "components/dbeaver/community/26.1.5",
        }
        component = SimpleNamespace(
            id="dbeaver",
            display_name="DBeaver Community",
            is_external=False,
            tracks=[{"id": "26.1", "displayName": "DBeaver 26.1"}],
            value={"kind": "application"},
            provider=lambda provider_id: {
                "id": provider_id,
                "displayName": "DBeaver Community",
            },
        )
        launcher = SimpleNamespace(
            id="dbeaver",
            component_id="dbeaver",
            display_name="DBeaver Community",
            start_mode="detached",
        )
        launched: list[tuple[str, str]] = []
        shortcuts: list[tuple[str, str]] = []
        app = SimpleNamespace(
            environments=SimpleNamespace(
                list=lambda: ["default", "prueba"]
            ),
            catalog=SimpleNamespace(
                component=lambda component_id: component,
                definitions={"dbeaver": component},
            ),
            inventory=lambda environment_id: [active],
            missing_components=lambda environment_id: [],
            available_launchers=lambda environment_id: [launcher],
            launch=lambda environment_id, launcher_id: (
                launched.append((environment_id, launcher_id)) or 1234
            ),
            create_launcher_shortcut=lambda environment_id, launcher_id: (
                shortcuts.append((environment_id, launcher_id))
                or SimpleNamespace(path=Path(r"C:\Desktop\DBeaver.lnk"))
            ),
        )
        status = {
            "state": "cached",
            "updates": [],
            "resolved": [],
            "error": None,
            "checked": True,
        }
        output = StringIO()
        with (
            patch.object(
                cli_module,
                "_inventory_sections",
                return_value=[
                    ("Aplicaciones (1)", ["[1] DBeaver Community"])
                ],
            ),
            patch.object(
                cli_module,
                "_read_input",
                side_effect=[
                    "1",
                    "3",
                    "2",
                    "4",
                    "2",
                    "\x1b",
                    "\x1b",
                ],
            ),
            redirect_stdout(output),
        ):
            result = cli_module._interactive_catalog(
                app, "default", status
            )
        self.assertIs(status, result)
        self.assertEqual([("prueba", "dbeaver")], launched)
        self.assertEqual([("prueba", "dbeaver")], shortcuts)
        rendered = output.getvalue()
        self.assertIn("[N] Instalar nuevo componente", rendered)
        self.assertIn("[A] Activar componentes disponibles", rendered)
        self.assertIn(
            "[F] Actualizar catálogos desde repositorios", rendered
        )
        self.assertIn("[G] Gestionar repositorios", rendered)
        self.assertIn("[3] Lanzar aplicación", rendered)
        self.assertIn("Profile de lanzamiento", rendered)
        self.assertIn(
            "[4] Crear acceso directo en el escritorio", rendered
        )
        self.assertIn("Profile del acceso directo", rendered)
        self.assertIn("[1] default (actual)", rendered)
        self.assertIn("[2] prueba", rendered)

    def test_launch_profile_selector_only_lists_active_profiles(self) -> None:
        launcher = SimpleNamespace(
            id="dbeaver", component_id="dbeaver"
        )
        inventory = {
            "default": [{"id": "dbeaver"}],
            "prueba": [{"id": "dbeaver"}],
            "sin-dbeaver": [{"id": "java"}],
        }
        app = SimpleNamespace(
            environments=SimpleNamespace(
                list=lambda: ["default", "prueba", "sin-dbeaver"]
            ),
            inventory=lambda profile_id: inventory[profile_id],
            available_launchers=lambda profile_id: [launcher],
        )
        output = StringIO()
        with (
            patch.object(cli_module, "_read_input", return_value="2"),
            redirect_stdout(output),
        ):
            selection = cli_module._select_launch_profile(
                app, "default", "dbeaver"
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual("prueba", selection[0])
        rendered = output.getvalue()
        self.assertIn("[1] default (actual)", rendered)
        self.assertIn("[2] prueba", rendered)
        self.assertNotIn("sin-dbeaver", rendered)

    def test_shortcut_profile_selector_rejects_unavailable_launcher(self) -> None:
        launcher = SimpleNamespace(id="dbeaver")
        app = SimpleNamespace(
            environments=SimpleNamespace(
                list=lambda: ["default", "prueba"]
            ),
            available_launchers=lambda profile_id: (
                [launcher] if profile_id == "default" else []
            ),
        )
        output = StringIO()
        with (
            patch.object(
                cli_module, "_read_input", side_effect=["2", "1"]
            ),
            redirect_stdout(output),
        ):
            selected = cli_module._select_shortcut_profile(
                app, "default", "dbeaver"
            )

        self.assertEqual("default", selected)
        self.assertIn(
            "dbeaver no está disponible en el profile prueba",
            output.getvalue(),
        )

    def test_catalog_adds_external_component_from_executable_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = external_component(
                Path(temporary) / "core" / "catalog" / "kiro.json"
            )
            executable = Path(temporary) / "Kiro" / "kiro.exe"
            executable.parent.mkdir()
            executable.touch()
            linked: list[tuple[str, str, Path]] = []
            app = SimpleNamespace(
                paths=paths,
                environments=SimpleNamespace(
                    read_desired=lambda environment_id: {
                        "workspace": environment_id,
                        "dataProfile": environment_id,
                    }
                ),
                inventory=lambda environment_id: [],
                catalog=SimpleNamespace(definitions={"kiro": component}),
                link_external_component=(
                    lambda environment_id, component_id, path: (
                        linked.append(
                            (environment_id, component_id, path)
                        )
                        or path.resolve()
                    )
                ),
            )
            output = StringIO()
            with (
                patch.object(
                    cli_module,
                    "_read_input",
                    side_effect=["1", str(executable)],
                ),
                redirect_stdout(output),
            ):
                result = cli_module._interactive_add_external_component(
                    app, "default"
                )

            self.assertTrue(result)
            self.assertEqual(
                [("default", "kiro", executable)], linked
            )
            rendered = output.getvalue()
            self.assertIn("Agregar componente externo", rendered)
            self.assertIn("Kiro vinculado en default", rendered)

    def test_dashboard_shows_boxes_catalog_and_update_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = EapPaths.from_root(Path(temporary))
            paths.ensure_layout()
            component = java_component(
                paths.components / "java_eap_component.json"
            )
            maven = maven_component(
                paths.components / "maven_eap_component.json"
            )
            nodejs = nodejs_component(
                paths.components / "nodejs_eap_component.json"
            )
            catalog = Catalog(
                paths,
                {},
                {"java": component, "maven": maven, "nodejs": nodejs},
            )
            environments = EnvironmentStore(paths)
            environments.create("default")
            commands_root = environments.custom_commands_path("default")
            (commands_root / "mvndepens.cmd").touch()
            (commands_root / "testqa.CMD").touch()
            (commands_root / "not-a-command.txt").touch()
            inventory = [
                {
                    "id": "java",
                    "provider": "temurin",
                    "track": 21,
                    "version": "21.0.12+8",
                    "installPath": "components/java/temurin/21.0.12+8",
                },
                {
                    "id": "maven",
                    "provider": "apache",
                    "track": 3,
                    "version": "3.9.16",
                    "installPath": "components/maven/apache/3.9.16",
                },
                {
                    "id": "nodejs",
                    "provider": "nodejs",
                    "track": 24,
                    "version": "24.19.0",
                    "installPath": "components/nodejs/nodejs/24.19.0",
                },
            ]
            app = SimpleNamespace(
                version="0.2.0",
                paths=paths,
                catalog=catalog,
                environments=environments,
                inventory=lambda environment_id: inventory,
                temporary_storage_usage=lambda: SimpleNamespace(
                    bytes=1536,
                    files=2,
                ),
                component_repositories=SimpleNamespace(
                    sources=lambda: [
                        {
                            "id": "official",
                            "repositoryUrl": (
                                "https://github.com/example/official"
                            ),
                            "revision": "c" * 40,
                        },
                        {
                            "id": "empresa",
                            "repositoryUrl": (
                                "https://github.com/example/empresa"
                            ),
                            "revision": None,
                        },
                    ]
                ),
                pocketools=SimpleNamespace(
                    sources=lambda: [
                        {
                            "id": "danielgube",
                            "repositoryUrl": (
                                "https://github.com/danielgube/eap-pocketools"
                            ),
                        }
                    ],
                    installed=lambda: [
                        {
                            "id": "sessionkeep",
                            "repository": "danielgube",
                            "version": "1.0.0",
                        }
                    ],
                ),
                configured_host_integration_statuses=lambda environment_id: [
                    SimpleNamespace(
                        display_name="Firefox",
                        ok=True,
                        detail="datos compartidos correctamente con el host",
                    ),
                    SimpleNamespace(
                        display_name="Integración de prueba",
                        ok=False,
                        detail="integración no activa",
                    ),
                ],
            )
            status = {
                "state": "done",
                "updates": [
                    {"family": "java", "latestVersion": "21.0.13+9"}
                ],
                "resolved": [],
                "error": None,
            }
            output = StringIO()
            with (
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((140, 40)),
                ),
                redirect_stdout(output),
            ):
                _render_main_dashboard(app, "default", status)
            rendered = output.getvalue()
            self.assertIn("┌─ Navegación", rendered)
            self.assertIn("Inicio", rendered)
            self.assertLess(
                rendered.index("┌─ Navegación"),
                rendered.index("┌─ Acciones "),
            )
            self.assertIn("┌─ EAP 0.2.0", rendered)
            self.assertIn("┌─ Componentes (3)", rendered)
            self.assertIn("ID", rendered)
            self.assertIn("Nombre", rendered)
            self.assertIn("Categoría", rendered)
            self.assertIn("Tipo", rendered)
            self.assertIn("Proveedor", rendered)
            self.assertIn("Repositorio", rendered)
            self.assertIn("Update", rendered)
            self.assertIn("Active", rendered)
            self.assertIn("Run", rendered)
            self.assertIn("Info", rendered)
            self.assertIn("[1]", rendered)
            self.assertIn("[1i]", rendered)
            self.assertIn("Java JDK · 21.0.12+8", rendered)
            self.assertIn("bootstrap", rendered)
            self.assertIn("Sí", rendered)
            self.assertIn("21.0.13+9", rendered)
            self.assertNotIn("Arrancable:", rendered)
            self.assertNotIn("Actualización:", rendered)
            self.assertNotIn("Tipo:", rendered)
            self.assertIn("[M] Gestionar profile", rendered)
            self.assertNotIn("[3] Diagnóstico", rendered)
            self.assertNotIn("[4] Limpiar temporales", rendered)
            self.assertIn("[T] Temporales: 1.5 KiB · 2 archivo(s)", rendered)
            self.assertIn(
                "[CC] Custom Commands: " + str(commands_root),
                rendered,
            )
            self.assertIn("· mvndepens,", rendered)
            self.assertIn("testqa", rendered)
            self.assertNotIn("mvndepens.cmd", rendered)
            self.assertNotIn("testqa.CMD", rendered)
            self.assertIn(
                "[C] Catálogo Components: 2 repositorio(s) externo(s)",
                rendered,
            )
            self.assertIn(
                "[P] Catálogo Pocketools: 1 repositorio(s) externo(s) "
                "(sessionkeep)",
                rendered,
            )
            self.assertLess(
                rendered.index("Temporales:"),
                rendered.index("Catálogo Components"),
            )
            self.assertNotIn("Abrir CMD del entorno", rendered)
            self.assertNotIn("Aplicaciones arrancables", rendered)
            self.assertNotIn("Componentes del entorno", rendered)
            self.assertNotIn("[8] Exportar entorno", rendered)
            self.assertNotIn("[9] Importar entorno", rendered)
            self.assertIn("[0] Opciones avanzadas", rendered)
            self.assertIn("[Esc] Cerrar interfaz", rendered)
            self.assertIn("Integraciones con el Host", rendered)
            self.assertIn("Firefox: OK", rendered)
            self.assertIn("Integración de prueba: KO", rendered)
            self.assertIn(
                "[D] Datos: " + str(paths.data / "profiles" / "default"),
                rendered,
            )
            self.assertIn(
                "[H] Home: "
                + str(paths.data / "profiles" / "default" / "home"),
                rendered,
            )
            self.assertLess(
                rendered.index("[D] Datos:"),
                rendered.index("[H] Home:"),
            )
            self.assertLess(
                rendered.index("[H] Home:"),
                rendered.index("[W] Workspace:"),
            )
            self.assertIn(
                "[W] Workspace: " + str(paths.workspaces / "default"),
                rendered,
            )

            app.configured_host_integration_statuses = (
                lambda environment_id: []
            )
            without_integrations = StringIO()
            with (
                patch.object(
                    cli_module.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((140, 40)),
                ),
                redirect_stdout(without_integrations),
            ):
                _render_main_dashboard(app, "default", status)
            self.assertNotIn(
                "┌─ Integraciones con el Host",
                without_integrations.getvalue(),
            )

    def test_component_update_column_distinguishes_all_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = java_component(root / "java.json")
            external = external_component(root / "kiro.json")

            self.assertEqual(
                "21.0.13+9",
                cli_module._component_update_version(
                    managed,
                    {
                        "state": "done",
                        "updates": [
                            {
                                "family": "java",
                                "latestVersion": "21.0.13+9",
                            }
                        ],
                    },
                ),
            )
            self.assertEqual(
                "No",
                cli_module._component_update_version(
                    managed, {"state": "done", "updates": []}
                ),
            )
            self.assertEqual(
                "--",
                cli_module._component_update_version(
                    external, {"state": "done", "updates": []}
                ),
            )
            self.assertEqual(
                "?",
                cli_module._component_update_version(
                    managed,
                    {
                        "state": "partial",
                        "updates": [],
                        "errors": {"java": "sin conexión"},
                    },
                ),
            )
            self.assertEqual(
                "?",
                cli_module._component_update_version(
                    managed,
                    {
                        "state": "cached",
                        "updates": [],
                        "checked": False,
                    },
                ),
            )

    def test_major_update_requires_yes_and_typed_component_name(self) -> None:
        component = SimpleNamespace(
            id="dbeaver", display_name="DBeaver Community"
        )
        update = UpdateInfo(
            family="dbeaver",
            provider="community",
            track=27,
            current_version="26.2.0",
            latest=ResolvedArtifact(
                family="dbeaver",
                component_id="dbeaver-community",
                provider="community",
                provider_name="DBeaver Community",
                track=27,
                version="27.0.0",
                url="https://example.test/dbeaver.zip",
                file_name="dbeaver.zip",
                sha256="a" * 64,
                size=None,
                metadata_url="https://example.test/releases",
            ),
            major_update=True,
        )
        output = StringIO()
        with (
            patch.object(cli_module, "_confirm", return_value=True),
            patch.object(
                cli_module, "_read_input", return_value="dbeaver"
            ) as read_input,
            redirect_stdout(output),
        ):
            self.assertTrue(
                cli_module._confirm_component_update(component, update)
            )

        self.assertIn("¡Aviso importante! Versión mayor", output.getvalue())
        self.assertIn("cambios incompatibles", output.getvalue())
        self.assertIn("Escriba dbeaver", read_input.call_args.args[0])

        with (
            patch.object(cli_module, "_confirm", return_value=True),
            patch.object(cli_module, "_read_input", return_value="otro"),
            redirect_stdout(StringIO()),
        ):
            self.assertFalse(
                cli_module._confirm_component_update(component, update)
            )

    def test_noninteractive_major_update_requires_named_option(self) -> None:
        component = SimpleNamespace(
            id="dbeaver", display_name="DBeaver Community"
        )
        update = UpdateInfo(
            family="dbeaver",
            provider="community",
            track=27,
            current_version="26.2.0",
            latest=ResolvedArtifact(
                family="dbeaver",
                component_id="dbeaver-community",
                provider="community",
                provider_name="DBeaver Community",
                track=27,
                version="27.0.0",
                url="https://example.test/dbeaver.zip",
                file_name="dbeaver.zip",
                sha256="a" * 64,
                size=None,
                metadata_url="https://example.test/releases",
            ),
            major_update=True,
        )
        installed: list[ResolvedArtifact] = []
        app = SimpleNamespace(
            environments=SimpleNamespace(
                read_desired=lambda profile_id: {"id": profile_id}
            ),
            resolve_update=lambda profile_id, component_id: update,
            catalog=SimpleNamespace(
                component=lambda component_id: component
            ),
            install=lambda *args, **kwargs: (
                installed.append(kwargs["artifact"])
                or (kwargs["artifact"], Path("dbeaver"))
            ),
        )
        parser = cli_module.build_parser()
        missing_name = parser.parse_args(
            [
                "component",
                "update",
                "dbeaver",
                "--profile",
                "default",
                "--yes",
            ]
        )
        with (
            self.assertRaisesRegex(ValidationError, "--confirm-major"),
            redirect_stdout(StringIO()),
        ):
            cli_module.dispatch(app, missing_name)

        confirmed = parser.parse_args(
            [
                "component",
                "update",
                "dbeaver",
                "--profile",
                "default",
                "--yes",
                "--confirm-major",
                "dbeaver",
            ]
        )
        with redirect_stdout(StringIO()):
            self.assertEqual(0, cli_module.dispatch(app, confirmed))
        self.assertEqual([update.latest], installed)


if __name__ == "__main__":
    unittest.main()
