from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import webbrowser
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import msvcrt
except ImportError:  # pragma: no cover - EAP se ejecuta en Windows
    msvcrt = None

from .application import EapApplication, UpdateInfo
from .config import Settings
from .console import console_title, set_console_title
from .errors import EapError, ValidationError

_ESCAPE = "\x1b"
_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD_CYAN = "\x1b[1;36m"
_ANSI_CYAN = "\x1b[36m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_RED = "\x1b[31m"
_COLOR_ENABLED = False
_INTERACTIVE_ACTIVE = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eap.cmd",
        description="EAP - profiles y aplicaciones portables",
    )
    parser.add_argument("--version", action="store_true", help="mostrar versión")
    parser.add_argument(
        "--inline",
        action="store_true",
        help="ejecutar el menú en la terminal actual",
    )
    parser.add_argument(
        "--shell-on-exit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command")

    _configure_profile_parser(
        subparsers.add_parser("profile", help="gestionar profiles EAP")
    )
    _configure_profile_parser(
        subparsers.add_parser(
            "env", help="alias compatible de profile"
        )
    )

    tool_parser = subparsers.add_parser(
        "tool", help="empaquetar y mantener la herramienta EAP"
    )
    tool_subparsers = tool_parser.add_subparsers(
        dest="tool_command", required=True
    )
    tool_export = tool_subparsers.add_parser(
        "export", help="exportar una distribución portable de EAP"
    )
    tool_export.add_argument(
        "name", nargs="?", help="nombre del 7z (por defecto eap-<versión>)"
    )
    tool_export.add_argument(
        "--include-components",
        action="store_true",
        help="incluir todos los payloads almacenados en components",
    )
    tool_export.add_argument("--force", action="store_true")
    tool_clean_temp = tool_subparsers.add_parser(
        "clean-temp", help="eliminar descargas y temporales de EAP"
    )
    tool_clean_temp.add_argument("--yes", action="store_true")

    eap_update = subparsers.add_parser(
        "update", help="comprobar o instalar una nueva release de EAP"
    )
    eap_update.add_argument(
        "--check",
        action="store_true",
        help="comprobar sin instalar",
    )
    eap_update.add_argument("--yes", action="store_true")
    eap_update.add_argument("--json", action="store_true")

    release_parser = subparsers.add_parser(
        "release", help="publicar una release administrativa de EAP"
    )
    release_parser.add_argument("--json", action="store_true")

    catalog_parser = subparsers.add_parser("catalog", help="mostrar catálogo")
    catalog_parser.add_argument("--json", action="store_true")

    component_parser = subparsers.add_parser(
        "component",
        help="resolver, activar, actualizar y desactivar componentes",
    )
    component_subparsers = component_parser.add_subparsers(
        dest="component_command", required=True
    )
    component_list = component_subparsers.add_parser(
        "list", help="listar componentes del profile"
    )
    _add_profile_argument(component_list)
    component_refresh = component_subparsers.add_parser(
        "refresh", help="actualizar los catálogos de componentes"
    )
    component_refresh.add_argument("repository", nargs="?")
    component_refresh.add_argument("--json", action="store_true")
    component_repository = component_subparsers.add_parser(
        "repository", help="gestionar repositorios de componentes"
    )
    component_repository_subparsers = component_repository.add_subparsers(
        dest="component_repository_command", required=True
    )
    component_repository_list = component_repository_subparsers.add_parser(
        "list", help="listar repositorios configurados"
    )
    component_repository_list.add_argument("--json", action="store_true")
    component_repository_add = component_repository_subparsers.add_parser(
        "add", help="añadir un repositorio HTTPS"
    )
    component_repository_add.add_argument("id")
    component_repository_add.add_argument("url")
    component_repository_add.add_argument("--yes", action="store_true")
    component_repository_remove = component_repository_subparsers.add_parser(
        "remove", help="quitar un repositorio"
    )
    component_repository_remove.add_argument("id")
    component_repository_remove.add_argument("--yes", action="store_true")
    resolve_parser = component_subparsers.add_parser(
        "resolve", help="resolver la última versión"
    )
    _add_component_selection(resolve_parser, include_environment=False)
    resolve_parser.add_argument("--json", action="store_true")

    install_parser = component_subparsers.add_parser(
        "install", help="activar o cambiar un componente en un profile"
    )
    _add_component_selection(install_parser, include_environment=True)
    install_parser.add_argument("--yes", action="store_true")
    install_parser.add_argument(
        "--executable",
        help="ruta local para vincular un componente externo",
    )

    check_parser = component_subparsers.add_parser(
        "check-updates", help="buscar actualizaciones disponibles"
    )
    _add_profile_argument(check_parser)
    check_parser.add_argument("--json", action="store_true")

    update_parser = component_subparsers.add_parser(
        "update", help="actualizar un componente"
    )
    update_parser.add_argument("component")
    _add_profile_argument(update_parser)
    update_parser.add_argument("--yes", action="store_true")
    update_parser.add_argument(
        "--confirm-major",
        metavar="COMPONENTE",
        help=(
            "confirmación escrita requerida con --yes para una versión mayor"
        ),
    )

    disable_parser = component_subparsers.add_parser(
        "disable", help="desactivar un componente sólo en un profile"
    )
    disable_parser.add_argument("component")
    _add_profile_argument(disable_parser)
    disable_parser.add_argument("--yes", action="store_true")

    uninstall_parser = component_subparsers.add_parser(
        "uninstall",
        help="desactivar y eliminar el payload si ningún profile lo usa",
    )
    uninstall_parser.add_argument("component")
    _add_profile_argument(uninstall_parser)
    uninstall_parser.add_argument("--yes", action="store_true")

    pocketool_parser = subparsers.add_parser(
        "pocketool", help="gestionar utilidades Pocketools globales"
    )
    pocketool_subparsers = pocketool_parser.add_subparsers(
        dest="pocketool_command", required=True
    )
    pocketool_list = pocketool_subparsers.add_parser(
        "list", help="listar Pocketools instaladas o disponibles"
    )
    pocketool_list.add_argument("--available", action="store_true")
    pocketool_list.add_argument("--refresh", action="store_true")
    pocketool_list.add_argument("--json", action="store_true")
    pocketool_search = pocketool_subparsers.add_parser(
        "search", help="buscar Pocketools disponibles"
    )
    pocketool_search.add_argument("query", nargs="?", default="")
    pocketool_search.add_argument("--refresh", action="store_true")
    pocketool_search.add_argument("--json", action="store_true")
    pocketool_install = pocketool_subparsers.add_parser(
        "install", help="instalar una Pocketool y sus dependencias"
    )
    pocketool_install.add_argument("pocketool")
    _add_profile_argument(pocketool_install)
    pocketool_install.add_argument("--offline", action="store_true")
    pocketool_install.add_argument("--yes", action="store_true")
    pocketool_update = pocketool_subparsers.add_parser(
        "update", help="actualizar una Pocketool instalada"
    )
    pocketool_update.add_argument("pocketool")
    _add_profile_argument(pocketool_update)
    pocketool_update.add_argument("--yes", action="store_true")
    pocketool_uninstall = pocketool_subparsers.add_parser(
        "uninstall", help="desinstalar una Pocketool"
    )
    pocketool_uninstall.add_argument("pocketool")
    pocketool_uninstall.add_argument("--yes", action="store_true")
    pocketool_help = pocketool_subparsers.add_parser(
        "help", help="mostrar la ayuda propia de una Pocketool"
    )
    pocketool_help.add_argument("pocketool")
    pocketool_help.add_argument("--json", action="store_true")
    pocketool_refresh = pocketool_subparsers.add_parser(
        "refresh", help="actualizar los índices Pocketools desde GitHub"
    )
    pocketool_refresh.add_argument("repository", nargs="?")
    pocketool_refresh.add_argument("--json", action="store_true")
    pocketool_run = pocketool_subparsers.add_parser(
        "run", help="ejecutor interno utilizado por los shims"
    )
    pocketool_run.add_argument("pocketool")
    pocketool_run.add_argument("pocketool_entrypoint")
    pocketool_run.add_argument("pocketool_arguments", nargs=argparse.REMAINDER)
    pocketool_repository = pocketool_subparsers.add_parser(
        "repository", help="gestionar repositorios Pocketools"
    )
    repository_subparsers = pocketool_repository.add_subparsers(
        dest="repository_command", required=True
    )
    repository_list = repository_subparsers.add_parser(
        "list", help="listar repositorios configurados"
    )
    repository_list.add_argument("--json", action="store_true")
    repository_add = repository_subparsers.add_parser(
        "add", help="añadir un repositorio HTTPS"
    )
    repository_add.add_argument("id")
    repository_add.add_argument("url")
    repository_add.add_argument("--yes", action="store_true")
    repository_remove = repository_subparsers.add_parser(
        "remove", help="quitar un repositorio"
    )
    repository_remove.add_argument("id")
    repository_remove.add_argument("--yes", action="store_true")

    proxy_parser = subparsers.add_parser(
        "proxy", help="consultar o autenticar el proxy corporativo"
    )
    proxy_subparsers = proxy_parser.add_subparsers(
        dest="proxy_command", required=True
    )
    proxy_status = proxy_subparsers.add_parser(
        "status", help="mostrar la configuración proxy efectiva"
    )
    proxy_status.add_argument("--json", action="store_true")
    proxy_authenticate = proxy_subparsers.add_parser(
        "authenticate", help="validarse ahora en el portal proxy"
    )
    proxy_authenticate.add_argument("--force", action="store_true")
    proxy_authenticate.add_argument("--json", action="store_true")

    trust_parser = subparsers.add_parser(
        "trust", help="integrar la confianza TLS de Windows en un profile"
    )
    trust_subparsers = trust_parser.add_subparsers(
        dest="trust_command", required=True
    )
    for trust_action, trust_help in (
        ("status", "mostrar la política de confianza del profile"),
        ("enable", "activar la confianza de Windows"),
        ("disable", "desactivar la confianza de Windows"),
    ):
        trust_command = trust_subparsers.add_parser(
            trust_action, help=trust_help
        )
        _add_profile_argument(trust_command)
        trust_command.add_argument("--json", action="store_true")

    shell_parser = subparsers.add_parser("shell", help="abrir shell activado")
    _add_profile_argument(shell_parser)
    shell_parser.add_argument(
        "--type", choices=["cmd", "powershell"], default="cmd"
    )

    terminal_parser = subparsers.add_parser(
        "terminal", help="gestionar Windows Terminal portable"
    )
    terminal_subparsers = terminal_parser.add_subparsers(
        dest="terminal_command", required=True
    )
    terminal_start = terminal_subparsers.add_parser(
        "start", help="abrir la instancia de terminal administrada por EAP"
    )
    _add_profile_argument(terminal_start)

    launch_parser = subparsers.add_parser(
        "launch", help="listar o arrancar aplicaciones del profile"
    )
    launch_parser.add_argument("launcher", nargs="?")
    _add_profile_argument(launch_parser)
    launch_parser.add_argument("--json", action="store_true")
    launch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolver el launcher sin crear el proceso",
    )

    shortcut_parser = subparsers.add_parser(
        "shortcut", help="crear accesos directos de aplicaciones"
    )
    shortcut_subparsers = shortcut_parser.add_subparsers(
        dest="shortcut_command", required=True
    )
    shortcut_create = shortcut_subparsers.add_parser(
        "create", help="crear un acceso directo en el escritorio"
    )
    shortcut_create.add_argument("launcher")
    _add_profile_argument(shortcut_create)
    shortcut_create.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="diagnóstico local")
    doctor_parser.add_argument("--json", action="store_true")
    return parser


def _configure_profile_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="env_command", required=True)
    subparsers.add_parser("list", help="listar profiles")
    create = subparsers.add_parser("create", help="crear profile")
    create.add_argument("name")
    create.add_argument("--workspace")
    create.add_argument(
        "--data",
        "--data-profile",
        dest="data_profile",
        metavar="DATA",
        help="datos nuevos o existentes (se conserva --data-profile como alias)",
    )
    duplicate = subparsers.add_parser(
        "duplicate", help="crear un profile basado en otro"
    )
    duplicate.add_argument("name", help="nombre del nuevo profile")
    _add_profile_argument(duplicate)
    delete = subparsers.add_parser("delete", help="eliminar un profile")
    delete.add_argument("name", help="profile que se eliminará")
    delete.add_argument("--yes", action="store_true")
    use = subparsers.add_parser("use", help="seleccionar profile")
    use.add_argument("name")
    use.add_argument(
        "--restore",
        action="store_true",
        help="descargar inmediatamente los components ausentes",
    )
    workspace = subparsers.add_parser(
        "workspace", help="asociar un workspace al profile"
    )
    workspace.add_argument("name")
    _add_profile_argument(workspace)
    data_profile = subparsers.add_parser(
        "data", help="asociar datos al profile"
    )
    data_profile.add_argument("name")
    _add_profile_argument(data_profile)
    compatible_data_profile = subparsers.add_parser(
        "data-profile", help="alias compatible de profile data"
    )
    compatible_data_profile.add_argument("name")
    _add_profile_argument(compatible_data_profile)
    restore = subparsers.add_parser(
        "restore", help="restaurar components ausentes desde el lock"
    )
    _add_profile_argument(restore)
    restore.add_argument("--yes", action="store_true")
    export = subparsers.add_parser(
        "export", help="exportar un profile a un archivo 7z"
    )
    export.add_argument("name", help="nombre del profile exportado")
    _add_profile_argument(export)
    export.add_argument(
        "--include-components",
        action="store_true",
        help="incluir los payloads instalados",
    )
    export.add_argument(
        "--include-config",
        action="store_true",
        help="incluir config.properties privado del profile",
    )
    export.add_argument(
        "--include-custom-commands",
        action="store_true",
        help="incluir custom-commands del profile",
    )
    export.add_argument("--force", action="store_true")
    import_parser = subparsers.add_parser(
        "import", help="importar un profile desde un archivo 7z"
    )
    import_parser.add_argument("archive")
    subparsers.add_parser(
        "export-all",
        help="exportar todos los profiles con las opciones predeterminadas",
    )
    subparsers.add_parser(
        "import-all",
        help="importar todos los paquetes 7z de la bandeja envs",
    )


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        "--env",
        dest="environment",
        metavar="PROFILE",
        help="profile EAP (se conserva --env como alias)",
    )


def _add_component_selection(
    parser: argparse.ArgumentParser, include_environment: bool
) -> None:
    parser.add_argument("component")
    parser.add_argument("--provider")
    parser.add_argument("--track")
    if include_environment:
        _add_profile_argument(parser)


def main(argv: list[str] | None = None) -> int:
    _configure_console_color()
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        status = (
            (lambda message: None)
            if getattr(arguments, "json", False)
            else _print_status
        )
        app = EapApplication(status=status)
        if arguments.version:
            print(app.version)
            return 0
        if arguments.command is None:
            selected = app.environments.selected(
                app.settings.get("profile.default")
            )
            should_start_terminal = (
                not arguments.inline
                and sys.stdin.isatty()
                and os.environ.get("EAP_MANAGED_TERMINAL") != "1"
            )
            if (
                should_start_terminal
                and selected is None
                and not app.environments.list()
            ):
                selected = app.settings.get("profile.default")
                app.environments.create(selected)
                desired = app.environments.read_desired(selected)
                print(
                    f"Primera ejecución: creado el profile {selected} · "
                    f"workspace {desired['workspace']}"
                )
            if should_start_terminal and selected is not None:
                result = app.start_managed_terminal(selected)
                print(
                    f"Windows Terminal EAP iniciado para {selected} · "
                    f"PID {result.process_id}"
                )
                return 0
            return interactive(
                app, shell_on_exit=arguments.shell_on_exit
            )
        return dispatch(app, arguments)
    except KeyboardInterrupt:
        if sys.stderr is not None:
            print("\nOperación cancelada.", file=sys.stderr)
        return 130
    except EapError as exc:
        _print_error(str(exc))
        return 1


def dispatch(app: EapApplication, arguments: argparse.Namespace) -> int:
    if arguments.command == "release":
        result = app.publish_eap_release()
        if arguments.json:
            _print_json(result.as_json())
        else:
            print(f"Release publicada: {result.tag}")
            print(f"Asset: {result.archive}")
            print(f"SHA256: {result.sha256}")
            print(f"GitHub: {result.release_url}")
        return 0

    if arguments.command == "update":
        update = app.check_eap_update()
        if arguments.json and (
            arguments.check or not update.update_available
        ):
            _print_json(update.as_json())
            return 0
        if update.latest_version is None:
            print("No hay releases públicas de EAP.")
            return 0
        if not update.update_available:
            print(f"EAP ya está actualizado: {update.current_version}")
            return 0
        if not arguments.json:
            print(
                f"Actualización de EAP: {update.current_version} -> "
                f"{update.latest_version}"
            )
        if arguments.check:
            return 0
        if arguments.json and not arguments.yes:
            raise ValidationError(
                "eap update --json requiere --yes para instalar"
            )
        if not arguments.yes and not _confirm("¿Actualizar EAP?"):
            print("Cancelado.")
            return 0
        result = app.install_eap_update(update)
        if arguments.json:
            _print_json(result.as_json())
        else:
            print(f"EAP actualizado a {result.version}.")
            print("Cierre y vuelva a abrir EAP para aplicar el nuevo código.")
        return 0

    if arguments.command == "proxy":
        if arguments.proxy_command == "status":
            status = app.proxy_status()
            if arguments.json:
                _print_json(status)
            else:
                print(
                    "Proxy configurado: "
                    + ("sí" if status["configured"] else "no")
                )
                proxies = status["proxies"]
                if isinstance(proxies, dict):
                    for name, value in proxies.items():
                        print(f"{name}: {value}")
                if status["authenticationEnabled"]:
                    print(
                        "Autenticación: "
                        f"{status['authenticationType']} · "
                        + (
                            "sesión activa"
                            if status["authenticatedInProcess"]
                            else "pendiente"
                        )
                    )
                else:
                    print("Autenticación interactiva: desactivada")
            return 0
        if arguments.proxy_command == "authenticate":
            result = app.authenticate_proxy(force=arguments.force)
            if arguments.json:
                _print_json(result.as_json())
            else:
                print(result.detail)
            return 0

    if arguments.command == "trust":
        environment_id = _require_environment(app, arguments.environment)
        if arguments.trust_command == "status":
            result = app.windows_trust_status(environment_id)
        else:
            result = app.set_windows_trust(
                environment_id,
                enabled=arguments.trust_command == "enable",
            )
        if arguments.json:
            _print_json(result)
        else:
            state = "activada" if result["enabled"] else "desactivada"
            print(
                f"Confianza TLS de Windows {state} en "
                f"{result['profile']}."
            )
            if arguments.trust_command != "status":
                _print_activation_notice(environment_id)
        return 0

    if arguments.command == "terminal":
        if arguments.terminal_command == "start":
            environment_id = _require_environment(
                app, arguments.environment
            )
            result = app.start_managed_terminal(environment_id)
            print(
                f"Windows Terminal EAP iniciado para {environment_id} · "
                f"PID {result.process_id}"
            )
            print(f"Configuración: {result.settings_path}")
            return 0

    if arguments.command in {"profile", "env"}:
        if arguments.env_command == "list":
            selected = app.environments.selected(
                app.settings.get("profile.default")
            )
            for name in app.environments.list():
                marker = "*" if name == selected else " "
                desired = app.environments.read_desired(name)
                print(
                    f"{marker} {name} · "
                    f"workspace: workspaces/{desired['workspace']} · "
                    f"datos: data/profiles/{desired['dataProfile']}"
                )
            return 0
        if arguments.env_command == "create":
            app.environments.create(
                arguments.name,
                workspace_id=arguments.workspace,
                data_profile_id=arguments.data_profile,
            )
            desired = app.environments.read_desired(arguments.name)
            print(
                f"Profile creado y seleccionado: {arguments.name} · "
                f"workspaces/{desired['workspace']} · "
                f"data/profiles/{desired['dataProfile']}"
            )
            return 0
        if arguments.env_command == "duplicate":
            source_id = _require_environment(app, arguments.environment)
            app.duplicate_profile(source_id, arguments.name)
            desired = app.environments.read_desired(arguments.name)
            print(
                f"Profile duplicado y seleccionado: {source_id} -> "
                f"{arguments.name} · workspace {desired['workspace']} · "
                f"datos {desired['dataProfile']}"
            )
            return 0
        if arguments.env_command == "delete":
            desired = app.environments.read_desired(arguments.name)
            print(f"Se eliminará el profile {arguments.name}.")
            print(
                "Se conservarán su workspace, sus datos y todos los "
                "payloads compartidos."
            )
            print("Su config.properties privado sí se eliminará.")
            if not arguments.yes and not _confirm("¿Eliminar profile?"):
                print("Cancelado.")
                return 0
            selected = app.delete_profile(arguments.name)
            print(
                f"Profile eliminado: {arguments.name} · "
                f"workspace {desired['workspace']} y datos "
                f"{desired['dataProfile']} conservados."
            )
            if selected is not None:
                print(f"Profile seleccionado: {selected}")
            else:
                print("No quedan profiles configurados.")
            return 0
        if arguments.env_command == "use":
            app.environments.select(arguments.name)
            print(f"Profile seleccionado: {arguments.name}")
            missing = app.missing_components(arguments.name)
            if missing and arguments.restore:
                restored = app.restore_missing_components(arguments.name)
                print(f"Components restaurados: {len(restored)}")
                _print_activation_notice(arguments.name)
            elif missing:
                print(
                    f"AVISO: faltan {len(missing)} component(s). "
                    "Use eap.cmd profile restore "
                    f"--profile {arguments.name}"
                )
            return 0
        if arguments.env_command == "workspace":
            environment_id = _require_environment(
                app, arguments.environment
            )
            workspace = app.environments.set_workspace(
                environment_id, arguments.name
            )
            print(
                f"Workspace de {environment_id}: "
                f"{workspace.relative_to(app.paths.root)}"
            )
            return 0
        if arguments.env_command in {"data", "data-profile"}:
            environment_id = _require_environment(
                app, arguments.environment
            )
            profile = app.environments.set_data_profile(
                environment_id, arguments.name
            )
            print(
                f"Datos del profile {environment_id}: "
                f"{profile.relative_to(app.paths.root)}"
            )
            _print_activation_notice(environment_id)
            return 0
        if arguments.env_command == "restore":
            environment_id = _require_environment(
                app, arguments.environment
            )
            missing = app.missing_components(environment_id)
            if not missing:
                print("Todos los components del lock están disponibles.")
                return 0
            for item in missing:
                print(
                    f"- {item['id']} · {item['version']} · {item['reason']}"
                )
            if not arguments.yes and not _confirm(
                "¿Descargar y verificar los components ausentes?"
            ):
                print("Cancelado.")
                return 0
            restored = app.restore_missing_components(environment_id)
            print(f"Components restaurados: {len(restored)}")
            _print_activation_notice(environment_id)
            return 0
        if arguments.env_command == "export":
            environment_id = _require_environment(
                app, arguments.environment
            )
            result = app.export_environment(
                environment_id,
                arguments.name,
                include_components=arguments.include_components,
                include_configuration=arguments.include_config,
                include_custom_commands=(
                    arguments.include_custom_commands
                ),
                force=arguments.force,
            )
            print(f"Exportado: {result.archive}")
            print(
                f"Profile: {result.environment_id} · "
                f"workspace: {result.workspace_id}"
            )
            print(
                "Components incluidos: "
                + ("sí" if result.components_included else "no")
            )
            print(
                "Configuración privada incluida: "
                + ("sí" if result.configuration_included else "no")
            )
            print(
                "Custom Commands incluidos: "
                + ("sí" if result.custom_commands_included else "no")
            )
            print(f"Tamaño: {result.size / (1024 * 1024):.1f} MiB")
            print(f"SHA256: {result.sha256}")
            return 0
        if arguments.env_command == "import":
            result = app.import_environment(Path(arguments.archive))
            print(f"Profile importado y seleccionado: {result.environment_id}")
            print(f"Workspace: workspaces/{result.workspace_id}")
            print(f"Components copiados: {result.components_copied}")
            print(
                "Configuración privada importada: "
                + ("sí" if result.configuration_included else "no")
            )
            print(
                "Custom Commands importados: "
                + ("sí" if result.custom_commands_included else "no")
            )
            if result.components_missing:
                print(
                    f"Faltan {result.components_missing} component(s); "
                    "use profile restore para descargarlos desde el lock."
                )
            return 0
        if arguments.env_command == "export-all":
            exported, failures = _export_all_profiles(app)
            _print_batch_export_summary(exported, failures)
            return 1 if failures else 0
        if arguments.env_command == "import-all":
            imported, failures = _import_all_profiles(app)
            _print_batch_import_summary(imported, failures)
            return 1 if failures else 0

    if arguments.command == "tool":
        if arguments.tool_command == "export":
            result = app.export_tool(
                arguments.name or f"eap-{app.version}",
                include_components=arguments.include_components,
                force=arguments.force,
            )
            print(f"EAP exportado: {result.archive}")
            print(
                "Almacén de components incluido: "
                + ("sí" if result.components_included else "no")
            )
            print(f"Tamaño: {result.size / (1024 * 1024):.1f} MiB")
            print(f"SHA256: {result.sha256}")
            return 0
        if arguments.tool_command == "clean-temp":
            usage = app.temporary_storage_usage()
            print(
                f"Temporales: {_format_bytes(usage.bytes)} · "
                f"{usage.files} archivo(s) · {app.paths.temp}"
            )
            if not arguments.yes and not _confirm(
                "¿Eliminar todos los temporales de EAP?"
            ):
                print("Cancelado.")
                return 0
            result = app.clean_temporary_storage()
            print(
                f"Temporales eliminados: "
                f"{_format_bytes(result.bytes_removed)} · "
                f"{result.files_removed} archivo(s)."
            )
            return 0

    if arguments.command == "catalog":
        values = [
            {
                "id": component.id,
                "name": component.display_name,
                "source": component.source_id,
                "revision": (
                    component.source.revision
                    if component.source is not None
                    else None
                ),
                "providers": [
                    {
                        "id": provider["id"],
                        "name": provider["displayName"],
                    }
                    for provider in component.providers
                ],
                "tracks": component.tracks,
                "launchable": bool(component.value["launchers"]),
            }
            for component in app.catalog.definitions.values()
        ]
        if arguments.json:
            _print_json(values)
        else:
            for component in values:
                print(
                    f"{component['name']} ({component['id']}) - "
                    f"fuente: {component['source']} - "
                    f"arrancable: {'sí' if component['launchable'] else 'no'}"
                )
                for provider in component["providers"]:
                    print(f"  proveedor: {provider['name']} ({provider['id']})")
                print(
                    "  líneas: "
                    + ", ".join(str(track["id"]) for track in component["tracks"])
                )
        return 0

    if arguments.command == "pocketool":
        if arguments.pocketool_command == "list":
            if arguments.available:
                values = [
                    item.as_json()
                    for item in app.available_pocketools(
                        refresh=arguments.refresh
                    )
                ]
                if arguments.json:
                    _print_json(values)
                elif not values:
                    print("No hay Pocketools disponibles.")
                else:
                    installed = {
                        (
                            str(item["repository"]).casefold(),
                            str(item["id"]).casefold(),
                        ): item
                        for item in app.pocketools.installed()
                    }
                    for item in values:
                        current = installed.get(
                            (
                                str(item["repository"]).casefold(),
                                str(item["id"]).casefold(),
                            )
                        )
                        suffix = (
                            f" · instalada {current['version']}"
                            if current is not None
                            else ""
                        )
                        print(
                            f"{item['repository']}/{item['id']} · "
                            f"{item['name']} · {item['version']}{suffix}"
                        )
                return 0
            values = app.pocketools.installed()
            if arguments.json:
                _print_json(values)
            elif not values:
                print("No hay Pocketools instaladas.")
            else:
                for item in values:
                    commands = ", ".join(
                        str(command["name"])
                        for command in item["manifest"]["commands"]
                    )
                    print(
                        f"{item['repository']}/{item['id']} · "
                        f"{item['name']} · {item['version']} · {commands}"
                    )
            return 0

        if arguments.pocketool_command == "search":
            query = arguments.query.casefold()
            values = [
                item
                for item in app.available_pocketools(
                    refresh=arguments.refresh
                )
                if query
                in " ".join(
                    (
                        item.source.id,
                        item.id,
                        item.name,
                        str(item.value["description"]),
                        *(
                            str(command["name"])
                            for command in item.commands
                        ),
                    )
                ).casefold()
            ]
            if arguments.json:
                _print_json([item.as_json() for item in values])
            elif not values:
                print("No se encontraron Pocketools.")
            else:
                for item in values:
                    print(
                        f"{item.selector} · {item.name} · {item.version}\n"
                        f"  {item.value['description']}"
                    )
            return 0

        if arguments.pocketool_command == "install":
            environment_id = _require_environment(
                app, arguments.environment
            )
            if not arguments.yes and not _confirm(
                f"¿Instalar {arguments.pocketool} desde sus repositorios "
                "configurados?"
            ):
                print("Cancelado.")
                return 0
            results = app.install_pocketool(
                arguments.pocketool,
                environment_id,
                refresh=not arguments.offline,
            )
            for result in results:
                action = "Instalada" if result.changed else "Ya instalada"
                print(
                    f"{action}: {result.selector} {result.version} · "
                    f"{result.install_path}"
                )
            print(
                "Abra una nueva shell EAP para disponer de sus comandos "
                "en PATH."
            )
            return 0

        if arguments.pocketool_command == "update":
            environment_id = _require_environment(
                app, arguments.environment
            )
            current = app.pocketools.find_installed(arguments.pocketool)
            print(
                f"{current['repository']}/{current['id']} · "
                f"versión instalada {current['version']}"
            )
            if not arguments.yes and not _confirm("¿Buscar e instalar actualización?"):
                print("Cancelado.")
                return 0
            results = app.update_pocketool(
                arguments.pocketool, environment_id
            )
            changed = [item for item in results if item.changed]
            if not changed:
                print("Ya está instalada la última versión disponible.")
            for result in changed:
                print(f"Actualizada: {result.selector} {result.version}")
            return 0

        if arguments.pocketool_command == "uninstall":
            current = app.pocketools.find_installed(arguments.pocketool)
            selector = f"{current['repository']}/{current['id']}"
            print(f"Se desinstalará {selector} {current['version']}.")
            print("Sus datos persistentes se conservarán.")
            if not arguments.yes and not _confirm("¿Desinstalar Pocketool?"):
                print("Cancelado.")
                return 0
            result = app.uninstall_pocketool(arguments.pocketool)
            print(f"Pocketool desinstalada: {result['pocketool']}")
            if not result["payloadRemoved"]:
                print(
                    "AVISO: no se pudo eliminar el payload residual: "
                    f"{result['residualPath']}"
                )
            return 0

        if arguments.pocketool_command == "help":
            value = app.pocketool_help(arguments.pocketool)
            if arguments.json:
                _print_json(value)
            else:
                help_value = value["help"]
                print(
                    f"{value['name']} {value['version']} "
                    f"({value['repository']}/{value['id']})"
                )
                print(help_value["summary"])
                print(f"Uso: {help_value['usage']}")
                for detail in help_value.get("details", []):
                    print(f"  {detail}")
                print(
                    "Comandos: "
                    + ", ".join(
                        str(item["name"]) for item in value["commands"]
                    )
                )
            return 0

        if arguments.pocketool_command == "refresh":
            values = app.refresh_pocketools(arguments.repository)
            if arguments.json:
                _print_json([item.as_json() for item in values])
            else:
                repositories = sorted(
                    {item.source.id for item in values}, key=str.casefold
                )
                print(
                    f"Índice actualizado: {len(values)} Pocketool(s) · "
                    + ", ".join(repositories)
                )
            return 0

        if arguments.pocketool_command == "run":
            forwarded = list(arguments.pocketool_arguments)
            if forwarded[:1] == ["--"]:
                forwarded.pop(0)
            return app.run_pocketool(
                arguments.pocketool,
                arguments.pocketool_entrypoint,
                forwarded,
            )

        if arguments.pocketool_command == "repository":
            if arguments.repository_command == "list":
                values = [source.as_json() for source in app.pocketools.sources()]
                if arguments.json:
                    _print_json(values)
                elif not values:
                    print("No hay repositorios Pocketools configurados.")
                else:
                    for source in values:
                        print(
                            f"{source['id']} · {source['repositoryUrl']}\n"
                            f"  índice: {source['catalogUrl']}"
                        )
                return 0
            if arguments.repository_command == "add":
                print(
                    "Los manifiestos y archivos publicados por este "
                    "repositorio podrán instalarse como Pocketools."
                )
                print(f"Repositorio: {arguments.url}")
                if not arguments.yes and not _confirm(
                    "¿Confiar y añadir este repositorio?"
                ):
                    print("Cancelado.")
                    return 0
                app.add_pocketool_repository(arguments.id, arguments.url)
                print(f"Repositorio añadido: {arguments.id}")
                return 0
            if arguments.repository_command == "remove":
                source = app.pocketools.source(arguments.id)
                print(f"Se quitará la fuente {source.id}: {source.repository_url}")
                if not arguments.yes and not _confirm("¿Quitar repositorio?"):
                    print("Cancelado.")
                    return 0
                app.remove_pocketool_repository(arguments.id)
                print(f"Repositorio eliminado: {arguments.id}")
                return 0

    if arguments.command == "component":
        if arguments.component_command == "refresh":
            catalog = app.refresh_component_catalogs(arguments.repository)
            values = [
                {
                    "id": component.id,
                    "source": component.source_id,
                    "revision": (
                        component.source.revision
                        if component.source is not None
                        else None
                    ),
                }
                for component in catalog.definitions.values()
            ]
            if arguments.json:
                _print_json(values)
            else:
                repositories = sorted(
                    {
                        component.source_id
                        for component in catalog.definitions.values()
                        if component.source is not None
                    },
                    key=str.casefold,
                )
                print(
                    f"Catálogo actualizado: {len(values)} componente(s) · "
                    + (", ".join(repositories) or "catálogo de bootstrap")
                )
            return 0

        if arguments.component_command == "repository":
            if arguments.component_repository_command == "list":
                values = app.component_repositories.cached_sources()
                if arguments.json:
                    _print_json(values)
                elif not values:
                    print("No hay repositorios de componentes configurados.")
                else:
                    for source in values:
                        revision = source["revision"] or "sin caché"
                        print(
                            f"{source['id']} · {source['repositoryUrl']}\n"
                            f"  revisión: {revision}"
                        )
                return 0
            if arguments.component_repository_command == "add":
                print(
                    "Este repositorio podrá publicar manifiestos que controlan "
                    "qué binarios descarga y ejecuta EAP."
                )
                print(f"Repositorio: {arguments.url}")
                if not arguments.yes and not _confirm(
                    "¿Confiar y añadir este repositorio?"
                ):
                    print("Cancelado.")
                    return 0
                app.add_component_repository(arguments.id, arguments.url)
                print(f"Repositorio añadido: {arguments.id}")
                print("Use component refresh para descargar su catálogo.")
                return 0
            if arguments.component_repository_command == "remove":
                source = app.component_repositories.source(arguments.id)
                print(
                    f"Se quitará la fuente {source.id}: "
                    f"{source.repository_url}"
                )
                if not arguments.yes and not _confirm(
                    "¿Quitar repositorio?"
                ):
                    print("Cancelado.")
                    return 0
                app.remove_component_repository(arguments.id)
                print(f"Repositorio eliminado: {arguments.id}")
                return 0

        if arguments.component_command == "resolve":
            provider, track = _component_selection(app, arguments)
            artifact = app.resolve(
                arguments.component, provider, track
            )
            if arguments.json:
                _print_json(artifact.as_json())
            else:
                _print_resolution(artifact)
            return 0

        if arguments.component_command == "list":
            environment_id = _require_environment(app, arguments.environment)
            inventory = app.inventory(environment_id)
            missing_ids = {
                str(item["id"])
                for item in app.missing_components(environment_id)
            }
            if not inventory:
                print(f"{environment_id}: sin componentes")
            for item in inventory:
                component = app.catalog.component(str(item["id"]))
                provider = component.provider(str(item["provider"]))
                print(
                    f"{component.display_name} · {provider['displayName']} · "
                    f"línea {item['track']} · {item['version']}"
                    + (
                        " · AUSENTE"
                        if str(item["id"]) in missing_ids
                        else ""
                    )
                )
            return 0

        if arguments.component_command == "install":
            environment_id = _require_environment(app, arguments.environment)
            component = app.catalog.component(arguments.component)
            if component.is_external:
                if not arguments.executable:
                    raise ValidationError(
                        f"{component.display_name} requiere --executable RUTA"
                    )
                executable = app.link_external_component(
                    environment_id,
                    component.id,
                    Path(arguments.executable),
                )
                print(
                    f"Componente externo vinculado en {environment_id}: "
                    f"{executable}"
                )
                return 0
            provider, track = _component_selection(app, arguments)
            artifact = app.resolve(
                arguments.component, provider, track
            )
            _print_resolution(artifact)
            if not arguments.yes and not _confirm("¿Descargar e instalar?"):
                print("Cancelado.")
                return 0
            _, install_path = app.install(
                environment_id,
                arguments.component,
                provider,
                track,
                artifact=artifact,
            )
            print(f"Profile {environment_id} actualizado: {install_path}")
            _print_activation_notice(environment_id)
            return 0

        if arguments.component_command == "check-updates":
            environment_id = _require_environment(app, arguments.environment)
            errors: dict[str, str] = {}
            updates = app.check_updates(environment_id, errors=errors)
            if arguments.json:
                _print_json([item.as_json() for item in updates])
            else:
                _print_updates(updates)
                for component_id, error in errors.items():
                    print(f"AVISO: {component_id}: {error}")
            return 0

        if arguments.component_command == "update":
            environment_id = _require_environment(app, arguments.environment)
            update = app.resolve_update(
                environment_id, arguments.component
            )
            if update is None:
                print("Ya está instalada la última versión disponible.")
                return 0
            print(
                f"{update.current_version} -> {update.latest.version} "
                f"({update.provider}, línea {update.track})"
            )
            component = app.catalog.component(update.family)
            if not _confirm_component_update(
                component,
                update,
                assume_yes=arguments.yes,
                written_confirmation=arguments.confirm_major,
            ):
                print("Cancelado.")
                return 0
            artifact, install_path = app.install(
                environment_id,
                arguments.component,
                update.provider,
                update.track,
                artifact=update.latest,
            )
            print(f"Actualizado a {artifact.version}: {install_path}")
            _print_activation_notice(environment_id)
            return 0

        if arguments.component_command == "disable":
            environment_id = _require_environment(app, arguments.environment)
            current = _find_locked_component(
                app.inventory(environment_id), arguments.component
            )
            component = app.catalog.component(arguments.component)
            print(
                f"Se desactivará {component.display_name} "
                f"({current['version']}) sólo en el profile {environment_id}."
            )
            print("El payload global y sus datos no se eliminarán.")
            if not arguments.yes and not _confirm("¿Desactivar?"):
                print("Cancelado.")
                return 0
            app.disable_component(environment_id, arguments.component)
            print(
                f"{component.display_name} desactivado en "
                f"{environment_id}."
            )
            _print_activation_notice(environment_id)
            return 0

        if arguments.component_command == "uninstall":
            environment_id = _require_environment(app, arguments.environment)
            current = _find_locked_component(
                app.inventory(environment_id), arguments.component
            )
            component = app.catalog.component(arguments.component)
            if component.is_external:
                raise ValidationError(
                    f"{component.display_name} es externo; use disable "
                    "para desvincularlo"
                )
            print(
                f"Se quitará {component.display_name} "
                f"({current['version']}) del profile {environment_id}."
            )
            print(
                "Su payload se eliminará si ningún otro profile lo usa; "
                "los datos personales se conservarán."
            )
            if not arguments.yes and not _confirm("¿Desinstalar?"):
                print("Cancelado.")
                return 0
            result = app.uninstall_component(
                environment_id, arguments.component
            )
            _print_uninstall_result(component.display_name, result)
            _print_activation_notice(environment_id)
            return 0

    if arguments.command == "shell":
        environment_id = _require_environment(app, arguments.environment)
        return app.open_shell(environment_id, arguments.type)

    if arguments.command == "launch":
        environment_id = _require_environment(app, arguments.environment)
        launchers = app.available_launchers(environment_id)
        if arguments.launcher is None:
            if arguments.json:
                _print_json([launcher.as_json() for launcher in launchers])
            elif not launchers:
                print(f"{environment_id}: sin aplicaciones arrancables")
            else:
                for launcher in launchers:
                    print(
                        f"{launcher.id} · {launcher.display_name} · "
                        f"{launcher.component_name}"
                    )
            return 0
        selected = next(
            (
                launcher
                for launcher in launchers
                if launcher.id == arguments.launcher
            ),
            None,
        )
        if selected is None:
            raise ValidationError(
                f"Launcher {arguments.launcher!r} no disponible en "
                f"{environment_id}"
            )
        if arguments.dry_run:
            _print_json(selected.as_json())
            return 0
        result = app.launch(environment_id, selected.id)
        if selected.start_mode == "detached" and sys.stdout is not None:
            print(f"{selected.display_name} arrancado · PID {result}")
        return result if selected.start_mode == "wait" else 0

    if arguments.command == "shortcut":
        if arguments.shortcut_command == "create":
            environment_id = _require_environment(
                app, arguments.environment
            )
            result = app.create_launcher_shortcut(
                environment_id, arguments.launcher
            )
            if arguments.json:
                _print_json(result.as_json())
            else:
                print(f"Acceso directo creado: {result.path}")
            return 0

    if arguments.command == "doctor":
        checks = app.doctor()
        if arguments.json:
            _print_json(checks)
        else:
            for check in checks:
                print(
                    f"[{check['status'].upper():7}] "
                    f"{check['name']}: {check['detail']}"
                )
        return 1 if any(check["status"] == "error" for check in checks) else 0

    raise ValidationError("Comando no implementado")


def interactive(
    app: EapApplication, shell_on_exit: bool = False
) -> int:
    global _INTERACTIVE_ACTIVE
    previous = _INTERACTIVE_ACTIVE
    _INTERACTIVE_ACTIVE = True
    try:
        return _run_interactive(app, shell_on_exit)
    except EapError as exc:
        _print_error(str(exc))
        return 1
    finally:
        _INTERACTIVE_ACTIVE = previous


def _run_interactive(
    app: EapApplication, shell_on_exit: bool = False
) -> int:
    with console_title("EAP"):
        environment_id = _ensure_interactive_environment(app)
        if environment_id is None:
            return 0
        _interactive_restore_missing(app, environment_id)
        update_status = _initial_update_status(app, environment_id)

        while True:
            # También restaura el título después de volver de un shell hijo y
            # refleja inmediatamente cualquier cambio de profile.
            set_console_title(f"EAP ({environment_id})")
            _render_main_dashboard(app, environment_id, update_status)
            option = _read_input("> ").strip().lower()
            if _is_escape(option):
                return _close_interactive(
                    app, environment_id, shell_on_exit
                )
            try:
                if option == "h":
                    _open_component_information_path(
                        app,
                        "Home del profile",
                        app.environments.ensure_profile(environment_id)
                        / "home",
                        "directory",
                    )
                elif option == "cc":
                    _open_component_information_path(
                        app,
                        "Custom Commands",
                        app.environments.custom_commands_path(
                            environment_id
                        ),
                        "directory",
                    )
                elif option == "c":
                    update_status = _interactive_catalog(
                        app, environment_id, update_status
                    )
                elif option == "p":
                    _interactive_pocketools(app, environment_id)
                elif option == "w":
                    _interactive_change_workspace(app, environment_id)
                elif option == "d":
                    _interactive_change_data_profile(app, environment_id)
                elif option == "t":
                    _interactive_clean_temporary_storage(app)
                elif option == "m":
                    previous_environment = environment_id
                    environment_id = _interactive_manage_environments(
                        app, environment_id
                    )
                    if environment_id != previous_environment:
                        _interactive_restore_missing(app, environment_id)
                    update_status = _initial_update_status(app, environment_id)
                elif option == "0":
                    previous_environment = environment_id
                    selected_environment = _interactive_advanced_options(
                        app, environment_id
                    )
                    if selected_environment is None:
                        return 0
                    environment_id = selected_environment
                    if environment_id != previous_environment:
                        _interactive_restore_missing(app, environment_id)
                    update_status = _initial_update_status(
                        app, environment_id
                    )
                elif run_match := re.fullmatch(r"(\d+)r", option):
                    ordered_inventory = _ordered_inventory(
                        app,
                        _profile_component_entries(app, environment_id),
                    )
                    selected_index = int(run_match.group(1)) - 1
                    if not 0 <= selected_index < len(ordered_inventory):
                        print("Opción no válida.")
                        _pause_after_result()
                        continue
                    _interactive_launch_component(
                        app,
                        environment_id,
                        str(ordered_inventory[selected_index]["id"]),
                    )
                elif info_match := re.fullmatch(r"(\d+)i", option):
                    ordered_inventory = _ordered_inventory(
                        app,
                        _profile_component_entries(app, environment_id),
                    )
                    selected_index = int(info_match.group(1)) - 1
                    if not 0 <= selected_index < len(ordered_inventory):
                        print("Opción no válida.")
                        _pause_after_result()
                        continue
                    selected = ordered_inventory[selected_index]
                    component = app.catalog.component(str(selected["id"]))
                    _interactive_component_information(
                        app, environment_id, component
                    )
                elif option.isdigit():
                    ordered_inventory = _ordered_inventory(
                        app,
                        _profile_component_entries(app, environment_id),
                    )
                    selected_index = int(option) - 1
                    if not 0 <= selected_index < len(ordered_inventory):
                        print("Opción no válida.")
                        _pause_after_result()
                        continue
                    update_status, changed = _interactive_component_entry(
                        app,
                        environment_id,
                        ordered_inventory[selected_index],
                        update_status,
                    )
                    if changed:
                        update_status = _refresh_updates(
                            app,
                            environment_id,
                            previous=update_status,
                            announce=False,
                        )
                elif option in {"q", "quit", "salir"}:
                    return _close_interactive(
                        app, environment_id, shell_on_exit
                    )
                else:
                    print("Opción no válida.")
                    _pause_after_result()
            except EapError as exc:
                _print_error(str(exc), pause=True)


def _close_interactive(
    app: EapApplication,
    environment_id: str,
    shell_on_exit: bool,
) -> int:
    if not shell_on_exit:
        return 0
    return app.open_shell(environment_id, "cmd")


def _initial_update_status(
    app: EapApplication, environment_id: str
) -> dict[str, Any]:
    cached = app.cached_updates(environment_id)
    status: dict[str, Any] = {
        "state": "cached",
        "updates": cached,
        "resolved": [],
        "error": None,
        "errors": {},
        "checked": app.has_cached_update_check(environment_id),
    }
    if _missing_components(app, environment_id):
        return status
    if app.inventory(environment_id) and app.should_check_updates(environment_id):
        return _refresh_updates(
            app, environment_id, previous=status, announce=True
        )
    return status


def _refresh_updates(
    app: EapApplication,
    environment_id: str,
    previous: dict[str, Any] | None = None,
    announce: bool = True,
) -> dict[str, Any]:
    if announce:
        print(f"Comprobando actualizaciones de {environment_id}...")
    try:
        errors: dict[str, str] = {}
        updates = app.check_updates(environment_id, errors=errors)
        return {
            "state": "partial" if errors else "done",
            "updates": [item.as_json() for item in updates],
            "resolved": updates,
            "error": next(iter(errors.values()), None),
            "errors": errors,
            "checked": True,
        }
    except EapError as exc:
        cached = previous.get("updates", []) if previous else []
        return {
            "state": "error",
            "updates": cached,
            "resolved": [],
            "error": str(exc),
            "errors": {},
            "checked": previous.get("checked", False) if previous else False,
        }


def _render_main_dashboard(
    app: EapApplication,
    environment_id: str,
    update_status: dict[str, Any],
) -> None:
    _start_page("Inicio")
    desired = app.environments.read_desired(environment_id)
    profile_path = (
        app.paths.data / "profiles" / str(desired["dataProfile"])
    )
    workspace_path = (
        app.paths.workspaces / str(desired["workspace"])
    )
    home_path = profile_path / "home"
    temporary_usage = app.temporary_storage_usage()
    component_source_count = len(app.component_repositories.sources())
    pocketool_source_count = len(app.pocketools.sources())
    installed_pocketool_ids = sorted(
        (str(item["id"]) for item in app.pocketools.installed()),
        key=str.casefold,
    )
    installed_pocketool_suffix = (
        " (" + ", ".join(installed_pocketool_ids) + ")"
        if installed_pocketool_ids
        else ""
    )
    custom_commands_path = app.environments.custom_commands_path(
        environment_id
    )
    custom_commands = app.environments.custom_commands(environment_id)
    custom_commands_display = (
        ", ".join(custom_commands) if custom_commands else "(sin comandos)"
    )
    inventory = app.inventory(environment_id)
    component_entries = _profile_component_entries(app, environment_id)
    missing_ids = {
        str(item["id"]) for item in _missing_components(app, environment_id)
    }
    component_sections = _inventory_sections(
        app,
        environment_id,
        component_entries,
        update_status,
        numbered=True,
        missing_ids=missing_ids,
    )
    status_rows: list[str] = []
    if update_status["state"] == "error":
        status_rows.append(
            "! No se pudo consultar la red; se muestran datos guardados"
        )
    elif update_status["state"] == "partial":
        status_rows.append(
            "! Comprobación parcial; alguna fuente no respondió"
        )
    elif (
        not update_status["updates"]
        and update_status.get("state") == "done"
        and inventory
        and not missing_ids
    ):
        status_rows.append("✓ Componentes al día en la última comprobación")
    elif (
        not update_status["updates"]
        and update_status.get("checked")
        and inventory
        and not missing_ids
    ):
        status_rows.append(
            "✓ Sin actualizaciones en la última comprobación guardada"
        )
    elif (
        not update_status["updates"]
        and inventory
        and not missing_ids
    ):
        status_rows.append("? Actualizaciones pendientes de comprobar")
    if missing_ids:
        status_rows.append(
            "! = componente ausente; use Gestionar > Restaurar"
        )

    _print_panel(
        f"EAP {app.version}",
        [
            (
                "Profile activo",
                [
                    f"Nombre: {environment_id}",
                    f"[D] Datos: {profile_path}",
                    f"[H] Home: {home_path}",
                    f"[W] Workspace: {workspace_path}",
                    "[T] Temporales: "
                    f"{_format_bytes(temporary_usage.bytes)} · "
                    f"{temporary_usage.files} archivo(s) · {app.paths.temp}",
                    f"[CC] Custom Commands: {custom_commands_path} · "
                    f"{custom_commands_display}",
                    f"[C] Catálogo Components: {component_source_count} "
                    "repositorio(s) externo(s)",
                    f"[P] Catálogo Pocketools: {pocketool_source_count} "
                    "repositorio(s) externo(s)"
                    f"{installed_pocketool_suffix}",
                ],
            ),
        ],
    )
    for title, rows in component_sections:
        _print_panel(title, [("", rows)])
    if status_rows:
        _print_panel("Estado de componentes", [("", status_rows)])
    integration_rows = [
        f"{status.display_name}: "
        f"{'OK' if status.ok else 'KO'} · {status.detail}"
        for status in app.configured_host_integration_statuses(
            environment_id
        )
    ]
    if integration_rows:
        _print_panel(
            "Integraciones con el Host",
            [("", integration_rows)],
        )
    _print_panel(
        "Acciones",
        [
            (
                "",
                [
                    "[M] Gestionar profile",
                    "[0] Opciones avanzadas",
                    "[Esc] Cerrar interfaz",
                ],
            )
        ],
    )


def _inventory_sections(
    app: EapApplication,
    environment_id: str,
    inventory: list[dict[str, Any]],
    update_status: dict[str, Any],
    numbered: bool = False,
    missing_ids: set[str] | None = None,
) -> list[tuple[str, list[str]]]:
    if not inventory:
        return [("Componentes", ["(sin componentes descargados)"])]
    missing_ids = missing_ids or set()
    ordered_inventory = _ordered_inventory(app, inventory)
    table_rows: list[list[str]] = []
    for index, item in enumerate(ordered_inventory, start=1):
        component = app.catalog.component(str(item["id"]))
        provider = component.provider(str(item["provider"]))
        missing_marker = " !" if component.id in missing_ids else ""
        table_rows.append(
            [
                f"[{index}]" if numbered else str(index),
                f"{component.display_name} · {item['version']}{missing_marker}",
                component.kind,
                str(provider["displayName"]),
                str(
                    item.get("repository")
                    or _component_repository_id(component, item)
                ),
                _component_update_version(component, update_status),
                "Sí" if item.get("active", True) else "No",
                (
                    f"[{index}r]"
                    if numbered
                    and item.get("active", True)
                    and component.id not in missing_ids
                    and bool(component.value.get("launchers"))
                    else ""
                ),
                f"[{index}i]" if numbered else "",
            ]
        )
    return [
        (
            f"Componentes ({len(table_rows)})",
            _component_table_rows(table_rows),
        )
    ]


def _component_table_rows(rows: list[list[str]]) -> list[str]:
    headers = [
        "ID",
        "Nombre",
        "Tipo",
        "Proveedor",
        "Repositorio",
        "Update",
        "Active",
        "Run",
        "Info",
    ]
    available = _terminal_panel_width() - 4
    gap = "  "
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    minimums = [len(header) for header in headers]
    while sum(widths) + len(gap) * (len(headers) - 1) > available:
        candidates = [
            index
            for index in (4, 3, 1, 2, 5, 6, 0, 7, 8)
            if widths[index] > minimums[index]
        ]
        if not candidates:
            break
        selected = max(
            candidates,
            key=lambda index: widths[index] - minimums[index],
        )
        widths[selected] -= 1

    def render(values: list[str]) -> str:
        return gap.join(
            _fit_text(value, widths[index]).ljust(widths[index])
            for index, value in enumerate(values)
        ).rstrip()

    return [
        render(headers),
        render(["─" * width for width in widths]),
        *(render(row) for row in rows),
    ]


def _component_update_version(
    component: Any, update_status: dict[str, Any]
) -> str:
    if component.is_external:
        return "--"
    for update in update_status.get("updates", []):
        if (
            not isinstance(update, dict)
            or str(update.get("family")) != component.id
        ):
            continue
        latest = update.get("latestVersion")
        if latest is None and isinstance(update.get("artifact"), dict):
            latest = update["artifact"].get("version")
        return str(latest) if latest else "Sí"
    errors = update_status.get("errors", {})
    if isinstance(errors, dict) and component.id in errors:
        return "?"
    if update_status.get("state") in {"done", "partial"}:
        return "No"
    if update_status.get("checked"):
        return "No"
    return "?"


def _ordered_inventory(
    app: EapApplication, inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    kind_order = {
        "runtime": 0,
        "server": 1,
        "tool": 2,
        "application": 3,
        "external": 4,
        "service": 5,
    }
    return sorted(
        inventory,
        key=lambda item: (
            kind_order.get(
                str(
                    app.catalog.component(str(item["id"])).value["kind"]
                ),
                99,
            ),
            str(
                app.catalog.component(str(item["id"])).display_name
            ).casefold(),
            0 if item.get("active", True) else 1,
            str(item.get("provider", "")).casefold(),
            str(item.get("version", "")).casefold(),
        ),
    )


def _profile_component_entries(
    app: EapApplication, environment_id: str
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in app.inventory(environment_id):
        component = app.catalog.component(str(item["id"]))
        entries.append(
            {
                **item,
                "active": True,
                "repository": _component_repository_id(component, item),
            }
        )
    payload_loader = getattr(app, "available_component_payloads", None)
    payloads = payload_loader(environment_id) if callable(payload_loader) else []
    for payload in payloads:
        component = app.catalog.component(str(payload.component_id))
        entries.append(
            {
                "id": payload.component_id,
                "provider": payload.provider,
                "track": payload.track,
                "version": payload.version,
                "installPath": str(payload.install_path),
                "active": False,
                "repository": _component_repository_id(component),
                "_payload": payload,
            }
        )
    return entries


def _component_repository_id(
    component: Any, item: dict[str, Any] | None = None
) -> str:
    if item is not None:
        manifest_source = item.get("manifestSource")
        if isinstance(manifest_source, dict):
            source_id = manifest_source.get("id")
            if isinstance(source_id, str) and source_id.strip():
                return source_id.strip()
    source = getattr(component, "source", None)
    return str(source.id) if source is not None else "bootstrap"


def _interactive_catalog(
    app: EapApplication,
    environment_id: str,
    update_status: dict[str, Any],
) -> dict[str, Any]:
    while True:
        _start_page("Inicio > Catálogo")
        component_entries = _profile_component_entries(
            app, environment_id
        )
        ordered_inventory = _ordered_inventory(app, component_entries)
        missing_ids = {
            str(item["id"])
            for item in _missing_components(app, environment_id)
        }
        _print_panel(
            f"Catálogo de componentes · {environment_id}",
            [
                (
                    "Componentes del profile",
                    [
                        "Seleccione un componente por su número."
                        if component_entries
                        else "Este profile todavía no tiene componentes "
                        "descargados.",
                    ],
                )
            ],
        )
        if component_entries:
            for title, rows in _inventory_sections(
                app,
                environment_id,
                component_entries,
                update_status,
                numbered=True,
                missing_ids=missing_ids,
            ):
                _print_panel(title, [("", rows)])
        _print_panel(
            "Acciones del catálogo",
            [
                (
                    "",
                    [
                        "[N] Instalar nuevo componente",
                        "[A] Activar componentes disponibles",
                        "[E] Agregar componente externo",
                        "[R] Comprobar actualizaciones",
                        "[F] Actualizar catálogos desde repositorios",
                        "[G] Gestionar repositorios",
                        "[Esc] Volver",
                    ],
                )
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return update_status
        if run_match := re.fullmatch(r"(\d+)r", option):
            selected_index = int(run_match.group(1)) - 1
            if not 0 <= selected_index < len(ordered_inventory):
                print("Opción no válida.")
                _pause_after_result()
                continue
            _interactive_launch_component(
                app,
                environment_id,
                str(ordered_inventory[selected_index]["id"]),
            )
            continue
        if info_match := re.fullmatch(r"(\d+)i", option):
            selected_index = int(info_match.group(1)) - 1
            if not 0 <= selected_index < len(ordered_inventory):
                print("Opción no válida.")
                _pause_after_result()
                continue
            selected = ordered_inventory[selected_index]
            component = app.catalog.component(str(selected["id"]))
            _interactive_component_information(
                app, environment_id, component
            )
            continue
        if option == "r":
            update_status = _refresh_updates(
                app,
                environment_id,
                previous=update_status,
                announce=True,
            )
            continue
        if option == "f":
            print("Actualizando catálogos de componentes...")
            catalog = app.refresh_component_catalogs()
            print(
                f"Catálogos actualizados: {len(catalog.definitions)} "
                f"componente(s) · {len(catalog.sources)} "
                "repositorio(s)."
            )
            _pause_after_result()
            continue
        if option == "g":
            _interactive_component_repositories(app)
            continue
        if option == "n":
            update_status = _interactive_install_new_component(
                app, environment_id, update_status
            )
            continue
        if option == "a":
            _interactive_activate_component(app, environment_id)
            continue
        if option == "e":
            _interactive_add_external_component(app, environment_id)
            continue
        if (
            not option.isdigit()
            or not 1 <= int(option) <= len(ordered_inventory)
        ):
            print("Opción no válida.")
            _pause_after_result()
            continue
        selected = ordered_inventory[int(option) - 1]
        update_status, changed = _interactive_component_entry(
            app,
            environment_id,
            selected,
            update_status,
        )
        if changed:
            update_status = _refresh_updates(
                app,
                environment_id,
                previous=update_status,
                announce=False,
            )


def _interactive_activate_component(
    app: EapApplication, environment_id: str
) -> bool:
    _start_page("Inicio > Catálogo > Activar componentes")
    payloads = app.available_component_payloads(environment_id)
    if not payloads:
        _print_panel(
            "Activar componentes",
            [
                (
                    "",
                    [
                        "No hay payloads locales desactivados disponibles.",
                        "Use Instalar nuevo componente para descargar uno.",
                    ],
                )
            ],
        )
        _read_input("Pulse Intro o Esc para volver: ")
        return False
    rows: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        availability = (
            "origen de descarga conservado"
            if payload.restorable
            else "sólo disponible localmente"
        )
        rows.extend(
            [
                f"[{index}] {payload.display_name} · "
                f"{payload.provider_name} · línea {payload.track} · "
                f"{payload.version}",
                f"    {availability} · {payload.install_path}",
            ]
        )
    _print_panel(
        f"Activar componentes · {environment_id}",
        [
            (
                "Payloads disponibles",
                rows,
            ),
            (
                "Activación local",
                [
                    "No se descargará ningún archivo.",
                    "[Esc] Volver al catálogo",
                ],
            ),
        ],
    )
    selected_index = _read_index(len(payloads))
    if selected_index is None:
        return False
    activated = app.activate_component_payload(
        environment_id, payloads[selected_index]
    )
    print(
        f"{activated.display_name} {activated.version} activado "
        f"en {environment_id} desde el payload local."
    )
    _print_activation_notice(environment_id)
    _pause_after_result()
    return True


def _interactive_component_repositories(app: EapApplication) -> None:
    while True:
        _start_page("Inicio > Catálogo > Repositorios")
        sources = app.component_repositories.cached_sources()
        repository_rows: list[str] = []
        for source in sources:
            revision = str(source["revision"] or "sin caché")
            repository_rows.extend(
                [
                    f"{source['id']} · {source['repositoryUrl']}",
                    f"    revisión: {revision}",
                ]
            )
        if not repository_rows:
            repository_rows = ["(sin repositorios configurados)"]
        _print_panel(
            "Catálogo de componentes > Repositorios",
            [
                (
                    "Catálogo de bootstrap",
                    ["Incluido con EAP · sólo infraestructura imprescindible"],
                ),
                ("Repositorios externos", repository_rows),
                (
                    "Acciones",
                    [
                        "[1] Añadir repositorio",
                        "[2] Quitar repositorio",
                        "[3] Actualizar catálogos",
                        "[Esc] Volver",
                    ],
                ),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        if option == "1":
            source_id = _read_input("Id corto del repositorio: ").strip()
            if _is_escape(source_id) or not source_id:
                continue
            url = _read_input(
                "URL HTTPS del repositorio o catalog.json: "
            ).strip()
            if _is_escape(url) or not url:
                continue
            _print_panel(
                "Confiar en repositorio de componentes",
                [
                    (
                        "Alcance",
                        [
                            f"Id: {source_id}",
                            f"URL: {url}",
                            "Podrá publicar definiciones y sustituir las "
                            "internas con el mismo id.",
                            "Los catálogos son declarativos; EAP no ejecuta "
                            "código del repositorio.",
                        ],
                    )
                ],
            )
            if _confirm("¿Confiar y añadir este repositorio?"):
                app.add_component_repository(source_id, url)
                print(f"Repositorio añadido: {source_id}")
                print("Use Actualizar catálogos para descargarlo.")
                _pause_after_result()
            continue
        if option == "2":
            if not sources:
                print("No hay repositorios que quitar.")
                _pause_after_result()
                continue
            choices = [
                f"[{index}] {source['id']} · {source['repositoryUrl']}"
                for index, source in enumerate(sources, start=1)
            ]
            choices.append("[Esc] Volver")
            _print_panel("Quitar repositorio", [("Fuentes", choices)])
            selected_index = _read_index(len(sources))
            if selected_index is None:
                continue
            selected = sources[selected_index]
            if _confirm(f"¿Quitar el repositorio {selected['id']}?"):
                app.remove_component_repository(str(selected["id"]))
                print(f"Repositorio eliminado: {selected['id']}")
                _pause_after_result()
            continue
        if option == "3":
            print("Actualizando catálogos de componentes...")
            catalog = app.refresh_component_catalogs()
            print(
                f"Catálogos actualizados: {len(catalog.definitions)} "
                f"componente(s) · {len(catalog.sources)} "
                "repositorio(s)."
            )
            _pause_after_result()
            continue
        print("Opción no válida.")
        _pause_after_result()


def _interactive_install_new_component(
    app: EapApplication,
    environment_id: str,
    update_status: dict[str, Any],
) -> dict[str, Any]:
    _start_page("Inicio > Catálogo > Instalar componente")
    active_by_id = {
        str(item["id"]): item for item in app.inventory(environment_id)
    }
    available = sorted(
        (
            component
            for component in app.catalog.definitions.values()
            if not component.is_external
        ),
        key=_install_component_sort_key,
    )
    if not available:
        _print_panel(
            "Catálogo > Instalar",
            [
                (
                    "",
                    [
                        "No hay componentes instalables en el catálogo.",
                        "Pulse Intro o Esc para volver.",
                    ],
                )
            ],
        )
        _read_input("> ")
        return update_status
    payloads = app.available_component_payloads(environment_id)
    payloads_by_id: dict[str, list[Any]] = {}
    for payload in payloads:
        payloads_by_id.setdefault(str(payload.component_id), []).append(payload)
    grouped_rows: dict[str, list[list[str]]] = {}
    category_labels: dict[str, str] = {}
    for index, component in enumerate(available, start=1):
        active = active_by_id.get(component.id)
        if active is not None:
            state = "Activo"
            version = str(active["version"])
        elif component.id in payloads_by_id:
            state = "Descargado · inactivo"
            version = _downloaded_component_versions(
                payloads_by_id[component.id]
            )
        else:
            state = "No instalado"
            version = "--"
        category_id, category_label = _component_category(component)
        category_labels[category_id] = category_label
        grouped_rows.setdefault(category_id, []).append(
            [
                f"[{index}]",
                component.display_name,
                state,
                version,
                _component_catalog_description(component),
                _component_repository_id(component),
            ]
        )
    sections = [
        (
            f"{category_labels[category_id]} ({len(rows)})",
            _install_component_table_rows(rows),
        )
        for category_id, rows in grouped_rows.items()
    ]
    sections.append(("Acciones", ["[Esc] Volver"]))
    _print_panel(
        "Catálogo > Instalar componente",
        sections,
    )
    selected_index = _read_index(len(available))
    if selected_index is None:
        return update_status
    component = available[selected_index]
    active = active_by_id.get(component.id)
    if active is not None:
        return _interactive_component_actions(
            app, environment_id, active, update_status
        )
    local_payloads = payloads_by_id.get(component.id, [])
    if local_payloads:
        changed = _interactive_downloaded_component(
            app, environment_id, component, local_payloads
        )
    else:
        changed = _interactive_install_component(
            app, environment_id, component
        )
    if not changed:
        return update_status
    return _refresh_updates(
        app,
        environment_id,
        previous=update_status,
        announce=False,
    )


_COMPONENT_CATEGORY_LABELS = {
    "runtimes": "Runtimes",
    "servers": "Servidores",
    "build-tools": "Herramientas de construcción",
    "version-control": "Control de versiones",
    "applications": "Aplicaciones",
    "services": "Servicios",
    "tools": "Herramientas",
}

_COMPONENT_KIND_CATEGORIES = {
    "runtime": "runtimes",
    "server": "servers",
    "tool": "tools",
    "application": "applications",
    "service": "services",
}

_COMPONENT_CATEGORY_ORDER = {
    "runtimes": 0,
    "servers": 1,
    "build-tools": 2,
    "version-control": 3,
    "applications": 4,
    "services": 5,
    "tools": 6,
}


def _component_category(component: Any) -> tuple[str, str]:
    raw_category = component.value.get("category")
    if isinstance(raw_category, str) and raw_category.strip():
        category_id = raw_category.strip().casefold()
    else:
        category_id = _COMPONENT_KIND_CATEGORIES.get(
            str(component.kind).casefold(),
            str(component.kind).casefold(),
        )
    label = _COMPONENT_CATEGORY_LABELS.get(
        category_id,
        category_id.replace("-", " ").capitalize(),
    )
    return category_id, label


def _install_component_sort_key(component: Any) -> tuple[int, str, str]:
    category_id, category_label = _component_category(component)
    return (
        _COMPONENT_CATEGORY_ORDER.get(category_id, 99),
        category_label.casefold(),
        component.display_name.casefold(),
    )


def _component_catalog_description(component: Any) -> str:
    description = component.value.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return str(component.information_description)


def _downloaded_component_versions(payloads: list[Any]) -> str:
    versions = list(
        dict.fromkeys(
            str(payload.version)
            for payload in payloads
            if getattr(payload, "version", None)
        )
    )
    return ", ".join(versions) if versions else "--"


def _install_component_table_rows(rows: list[list[str]]) -> list[str]:
    headers = [
        "ID",
        "Componente",
        "Estado",
        "Versión",
        "Descripción",
        "Fuente",
    ]
    available = _terminal_panel_width() - 4
    gap = "  "
    minimums = [4, 18, 21, len("Versión"), 18, len("Fuente")]
    minimum_width = sum(minimums) + len(gap) * (len(headers) - 1)
    if available < minimum_width:
        compact: list[str] = []
        for component_id, name, state, version, description, source in rows:
            version_suffix = f" · {version}" if version != "--" else ""
            compact.extend(
                [
                    f"{component_id} {name} · {state}{version_suffix}",
                    f"    {description}",
                    f"    Fuente: {source}",
                ]
            )
        return compact

    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    while sum(widths) + len(gap) * (len(headers) - 1) > available:
        candidates = [
            index
            for index in (4, 1, 2, 3, 5)
            if widths[index] > minimums[index]
        ]
        if not candidates:
            break
        selected = max(
            candidates,
            key=lambda index: widths[index] - minimums[index],
        )
        widths[selected] -= 1

    def render(values: list[str]) -> str:
        return gap.join(
            _fit_text(value, widths[index]).ljust(widths[index])
            for index, value in enumerate(values)
        ).rstrip()

    return [
        render(headers),
        render(["─" * width for width in widths]),
        *(render(row) for row in rows),
    ]


def _interactive_add_external_component(
    app: EapApplication, environment_id: str
) -> bool:
    _start_page("Inicio > Catálogo > Agregar componente externo")
    installed_ids = {
        str(item["id"]) for item in app.inventory(environment_id)
    }
    available = sorted(
        (
            component
            for component in app.catalog.definitions.values()
            if component.is_external and component.id not in installed_ids
        ),
        key=lambda item: item.display_name.casefold(),
    )
    if not available:
        _print_panel(
            "Catálogo > Componentes externos",
            [
                (
                    "",
                    [
                        "No hay componentes externos pendientes de agregar.",
                        "Puede cambiar una ruta desde el componente instalado.",
                        "Pulse Intro o Esc para volver.",
                    ],
                )
            ],
        )
        _read_input("> ")
        return False
    rows = [
        f"[{index}] {component.display_name} · "
        f"Fuente: {_component_source_label(component)}"
        for index, component in enumerate(available, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel(
        "Catálogo > Agregar componente externo",
        [("Aplicaciones disponibles", rows)],
    )
    selected_index = _read_index(len(available))
    if selected_index is None:
        return False
    return _interactive_link_external_component(
        app, environment_id, available[selected_index]
    )


def _interactive_link_external_component(
    app: EapApplication,
    environment_id: str,
    component: Any,
) -> bool:
    _start_page(
        f"Inicio > Catálogo > {component.display_name} > Vincular"
    )
    _print_panel(
        f"Vincular {component.display_name}",
        [
            ("Fuente", _component_source_rows(component)),
            *_component_information_sections(
                app, environment_id, component
            ),
        ],
    )
    prompt = str(component.value["install"]["prompt"])
    raw_path = _read_input(f"{prompt} [Esc]: ").strip()
    if _is_escape(raw_path) or not raw_path:
        return False
    executable = app.link_external_component(
        environment_id,
        component.id,
        Path(raw_path.strip('"')),
    )
    print(
        f"{component.display_name} vinculado en {environment_id}: "
        f"{executable}"
    )
    _pause_after_result()
    return True


def _interactive_component_entry(
    app: EapApplication,
    environment_id: str,
    selected: dict[str, Any],
    update_status: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if selected.get("active", True):
        return (
            _interactive_component_actions(
                app, environment_id, selected, update_status
            ),
            False,
        )
    component = app.catalog.component(str(selected["id"]))
    payload = selected.get("_payload")
    payloads = [payload] if payload is not None else []
    if not payloads:
        payloads = [
            candidate
            for candidate in app.available_component_payloads(environment_id)
            if candidate.component_id == component.id
        ]
    if not payloads:
        return (
            update_status,
            _interactive_install_component(app, environment_id, component),
        )
    return (
        update_status,
        _interactive_downloaded_component(
            app, environment_id, component, payloads
        ),
    )


def _interactive_downloaded_component(
    app: EapApplication,
    environment_id: str,
    component: Any,
    payloads: list[Any],
) -> bool:
    while True:
        _start_page(f"Inicio > Catálogo > {component.display_name}")
        payload_rows: list[str] = []
        for index, payload in enumerate(payloads, start=1):
            availability = (
                "origen de descarga conservado"
                if payload.restorable
                else "sólo disponible localmente"
            )
            payload_rows.append(
                f"[{index}] Activar {payload.provider_name} · "
                f"{_track_display(component, payload.track)} · "
                f"{payload.version} · {availability}"
            )
        payload_rows.extend(
            [
                "[N] Elegir otro proveedor o línea",
                "[Esc] Volver al catálogo",
            ]
        )
        _print_panel(
            f"Catálogo > {component.display_name}",
            [
                ("Fuente", _component_source_rows(component)),
                *_component_information_sections(
                    app, environment_id, component
                ),
                ("Descargado · inactivo", payload_rows),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return False
        if option == "n":
            return _interactive_install_component(
                app, environment_id, component
            )
        if not option.isdigit() or not 1 <= int(option) <= len(payloads):
            print("Opción no válida.")
            _pause_after_result()
            continue
        activated = app.activate_component_payload(
            environment_id, payloads[int(option) - 1]
        )
        print(
            f"{activated.display_name} {activated.version} activado "
            f"en {environment_id} desde el payload local."
        )
        _print_activation_notice(environment_id)
        _pause_after_result()
        return True


def _interactive_component_actions(
    app: EapApplication,
    environment_id: str,
    selected: dict[str, Any],
    update_status: dict[str, Any],
) -> dict[str, Any]:
    component_id = str(selected["id"])
    component = app.catalog.component(component_id)
    while True:
        _start_page(f"Inicio > Catálogo > {component.display_name}")
        active = _active_component(app, environment_id, component_id)
        if active is None:
            return update_status
        missing_items = _missing_components(app, environment_id)
        missing_ids = {str(item["id"]) for item in missing_items}
        launchers: list[Any] = []
        if component_id not in missing_ids:
            launchers = [
                launcher
                for launcher in app.available_launchers(environment_id)
                if launcher.component_id == component_id
            ]
        cached_update = next(
            (
                item
                for item in update_status.get("updates", [])
                if isinstance(item, dict)
                and str(item.get("family")) == component_id
            ),
            None,
        )
        if component.is_external:
            actions: list[tuple[str, str, str]] = [
                ("1", "Cambiar ruta del ejecutable", "relink"),
            ]
            next_index = 2
        else:
            update_action = "Buscar actualización"
            if cached_update is not None:
                update_action = (
                    "Actualizar a "
                    + str(
                        cached_update.get(
                            "latestVersion", "nueva versión"
                        )
                    )
                )
                if cached_update.get("majorUpdate") is True:
                    update_action += " · versión mayor"
            actions = [
                ("1", update_action, "update"),
                ("2", "Cambiar proveedor o línea", "change"),
            ]
            next_index = 3
            if component_id in missing_ids:
                actions.append(
                    (
                        str(next_index),
                        "Restaurar payload ausente",
                        "restore",
                    )
                )
                next_index += 1
        if launchers:
            actions.append(
                (str(next_index), "Lanzar aplicación", "launch")
            )
            next_index += 1
            actions.append(
                (
                    str(next_index),
                    "Crear acceso directo en el escritorio",
                    "shortcut",
                )
            )
            next_index += 1
        actions.append(
            (
                str(next_index),
                "Desactivar en este profile",
                "disable",
            )
        )
        next_index += 1
        if not component.is_external:
            actions.append(
                (
                    str(next_index),
                    "Desinstalar componente",
                    "uninstall",
                )
            )
        action_rows = [
            f"[{key}] {label}" for key, label, _ in actions
        ]
        action_rows.append("[Esc] Volver al catálogo")
        _print_panel(
            f"Catálogo > {component.display_name}",
            [
                ("Fuente", _component_source_rows(component)),
                (
                    "Selección activa",
                    (
                        _external_selection_rows(component, active)
                        if component.is_external
                        else [_active_selection_text(component, active)]
                    ),
                ),
                ("Acciones", action_rows),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return update_status
        action = next(
            (action_id for key, _, action_id in actions if key == option),
            None,
        )
        if action is None:
            print("Opción no válida.")
            _pause_after_result()
            continue
        if action == "update":
            update_status = _interactive_update_component(
                app, environment_id, active, update_status
            )
            continue
        if action == "relink":
            _interactive_link_external_component(
                app, environment_id, component
            )
            continue
        if action == "change":
            if _interactive_install_component(
                app, environment_id, component
            ):
                update_status = _refresh_updates(
                    app,
                    environment_id,
                    previous=update_status,
                    announce=False,
                )
            continue
        if action == "restore":
            restored = app.restore_missing_components(environment_id)
            print(f"Components restaurados: {len(restored)}")
            _print_activation_notice(environment_id)
            _pause_after_result()
            continue
        if action == "disable":
            _print_panel(
                f"Desactivar {component.display_name}",
                [
                    (
                        "Alcance",
                        [
                            f"Sólo se quitará del profile {environment_id}.",
                            "El payload global y sus datos se conservarán.",
                        ],
                    )
                ],
            )
            if not _confirm("¿Desactivar este componente?"):
                continue
            app.disable_component(environment_id, component_id)
            print(
                f"{component.display_name} desactivado en "
                f"{environment_id}."
            )
            _print_activation_notice(environment_id)
            _pause_after_result()
            update_status["updates"] = [
                item
                for item in update_status.get("updates", [])
                if not (
                    isinstance(item, dict)
                    and str(item.get("family")) == component_id
                )
            ]
            update_status["resolved"] = [
                item
                for item in update_status.get("resolved", [])
                if str(getattr(item, "family", "")) != component_id
            ]
            return update_status
        if action == "uninstall":
            _print_panel(
                f"Desinstalar {component.display_name}",
                [
                    (
                        "Alcance",
                        [
                            f"Se quitará del profile {environment_id}.",
                            "El payload global se eliminará si ningún otro "
                            "profile lo usa.",
                            "Los datos personales se conservarán.",
                        ],
                    )
                ],
            )
            if not _confirm("¿Desinstalar este componente?"):
                continue
            result = app.uninstall_component(
                environment_id, component_id
            )
            _print_uninstall_result(component.display_name, result)
            _print_activation_notice(environment_id)
            _pause_after_result()
            update_status["updates"] = [
                item
                for item in update_status.get("updates", [])
                if not (
                    isinstance(item, dict)
                    and str(item.get("family")) == component_id
                )
            ]
            update_status["resolved"] = [
                item
                for item in update_status.get("resolved", [])
                if str(getattr(item, "family", "")) != component_id
            ]
            return update_status
        if action == "launch":
            _interactive_launch_component(
                app, environment_id, component_id
            )
            continue
        launcher = _select_component_launcher(launchers)
        if launcher is None:
            continue
        shortcut_environment_id = _select_shortcut_profile(
            app, environment_id, launcher.id
        )
        if shortcut_environment_id is None:
            continue
        shortcut = app.create_launcher_shortcut(
            shortcut_environment_id, launcher.id
        )
        print(f"Acceso directo creado: {shortcut.path}")
        _pause_after_result()


def _external_selection_rows(
    component: Any, active: dict[str, Any]
) -> list[str]:
    provider = component.provider(str(active["provider"]))
    installation = active.get("installation", {})
    executable = installation.get("executable")
    return [
        f"Proveedor: {provider['displayName']}",
        "Ejecutable: " + (
            str(executable) if executable else "sin vincular"
        ),
        "Actualización: manual, fuera de EAP",
    ]


def _select_component_launcher(launchers: list[Any]) -> Any | None:
    if not launchers:
        return None
    if len(launchers) == 1:
        return launchers[0]
    rows = [
        f"[{index}] {launcher.display_name}"
        for index, launcher in enumerate(launchers, start=1)
    ]
    rows.append("[Esc] Volver")
    _start_page("Inicio > Catálogo > Lanzar aplicación")
    _print_panel("Seleccione una aplicación", [("", rows)])
    selected_index = _read_index(len(launchers))
    if selected_index is None:
        return None
    return launchers[selected_index]


def _interactive_launch_component(
    app: EapApplication,
    current_profile_id: str,
    component_id: str,
) -> None:
    selection = _select_launch_profile(
        app, current_profile_id, component_id
    )
    if selection is None:
        return
    profile_id, launchers = selection
    launcher = _select_component_launcher(launchers)
    if launcher is None:
        return
    result = app.launch(profile_id, launcher.id)
    if launcher.start_mode == "detached":
        print(f"{launcher.display_name} arrancado · PID {result}")
        _pause_after_result()


def _select_launch_profile(
    app: EapApplication,
    current_profile_id: str,
    component_id: str,
) -> tuple[str, list[Any]] | None:
    active_profiles: list[str] = []
    for profile_id in app.environments.list():
        if any(
            str(item.get("id")) == component_id
            for item in app.inventory(profile_id)
        ):
            active_profiles.append(profile_id)
    if not active_profiles:
        print(
            f"{component_id} no tiene una aplicación arrancable en ningún "
            "profile."
        )
        _pause_after_result()
        return None
    profile_id = active_profiles[0]
    if len(active_profiles) > 1:
        rows = []
        for index, candidate in enumerate(active_profiles, start=1):
            marker = " (actual)" if candidate == current_profile_id else ""
            rows.append(f"[{index}] {candidate}{marker}")
        rows.append("[Esc] Volver")
        _start_page("Inicio > Catálogo > Lanzar aplicación")
        _print_panel("Profile de lanzamiento", [("", rows)])
        selected_index = _read_index(len(active_profiles))
        if selected_index is None:
            return None
        profile_id = active_profiles[selected_index]
    launchers = [
        launcher
        for launcher in app.available_launchers(profile_id)
        if launcher.component_id == component_id
    ]
    if not launchers:
        print(
            f"{component_id} no tiene una aplicación arrancable en el "
            f"profile {profile_id}."
        )
        _pause_after_result()
        return None
    return profile_id, launchers


def _select_shortcut_profile(
    app: EapApplication, current_profile_id: str, launcher_id: str
) -> str | None:
    profiles = app.environments.list()
    if len(profiles) <= 1:
        return current_profile_id

    rows = []
    for index, profile_id in enumerate(profiles, start=1):
        marker = " (actual)" if profile_id == current_profile_id else ""
        rows.append(f"[{index}] {profile_id}{marker}")
    rows.append("[Esc] Volver")
    _start_page("Inicio > Catálogo > Crear acceso directo")
    _print_panel("Profile del acceso directo", [("", rows)])

    while True:
        selected_index = _read_index(len(profiles))
        if selected_index is None:
            return None
        selected = profiles[selected_index]
        if any(
            launcher.id == launcher_id
            for launcher in app.available_launchers(selected)
        ):
            return selected
        print(
            f"{launcher_id} no está disponible en el profile {selected}."
        )
        _pause_after_result()


def _interactive_update_component(
    app: EapApplication,
    environment_id: str,
    selected: dict[str, Any],
    update_status: dict[str, Any],
) -> dict[str, Any]:
    component = app.catalog.component(str(selected["id"]))
    _start_page(
        f"Inicio > Catálogo > {component.display_name} > Actualizar"
    )
    update_status = _refresh_updates(
        app,
        environment_id,
        previous=update_status,
        announce=True,
    )
    update = next(
        (
            item
            for item in update_status.get("resolved", [])
            if item.family == selected["id"]
        ),
        None,
    )
    if update is None:
        component_error = update_status.get("errors", {}).get(
            str(selected["id"])
        )
        rows = (
            [
                "No se pudo comprobar esta actualización:",
                str(component_error),
                "Pulse Intro o Esc para volver.",
            ]
            if component_error
            else [
                f"{component.display_name} ya está actualizado.",
                "Pulse Intro o Esc para volver.",
            ]
        )
        _print_panel(
            f"Actualizar {component.display_name}",
            [("", rows)],
        )
        _read_input("> ")
        return update_status
    _print_panel(
        f"Actualizar {component.display_name}",
        [
            (
                "",
                [
                    f"Fuente: {_component_source_label(component)}",
                    f"Actual: {update.current_version}",
                    f"Nueva: {update.latest.version}",
                    f"Proveedor: {update.latest.provider_name}",
                    f"Línea: {update.track}",
                    "Tipo: "
                    + (
                        "versión mayor"
                        if update.major_update
                        else "actualización compatible"
                    ),
                ],
            )
        ],
    )
    if not _confirm_component_update(component, update):
        return update_status
    artifact, path = app.install(
        environment_id,
        update.family,
        update.provider,
        update.track,
        artifact=update.latest,
    )
    print(f"Actualizado a {artifact.version}: {path}")
    _print_activation_notice(environment_id)
    _pause_after_result()
    return _refresh_updates(
        app,
        environment_id,
        previous=update_status,
        announce=False,
    )


def _interactive_install_component(
    app: EapApplication,
    environment_id: str,
    component: Any,
) -> bool:
    if component.is_external:
        return _interactive_link_external_component(
            app, environment_id, component
        )
    while True:
        _start_page(f"Inicio > Catálogo > {component.display_name}")
        active = _active_component(
            app, environment_id, component.id
        )
        provider_rows: list[str] = []
        for index, provider in enumerate(component.providers, start=1):
            if active and active["provider"] == provider["id"]:
                state = (
                    f"activo: {_track_display(component, active['track'])} "
                    f"· {active['version']}"
                )
            else:
                state = "disponible"
            provider_rows.append(
                f"[{index}] {provider['displayName']} · {state}"
            )
        provider_rows.append("[Esc] Volver al catálogo")
        _print_panel(
            f"Catálogo > {component.display_name}",
            [
                ("Fuente", _component_source_rows(component)),
                *_component_information_sections(
                    app, environment_id, component
                ),
                ("Proveedor", provider_rows),
            ],
        )
        provider_index = _read_index(len(component.providers))
        if provider_index is None:
            return False
        provider_definition = component.providers[provider_index]
        provider = str(provider_definition["id"])

        while True:
            _start_page(
                f"Inicio > Catálogo > {component.display_name} > "
                f"{provider_definition['displayName']}"
            )
            active = _active_component(
                app, environment_id, component.id
            )
            track_rows: list[str] = []
            for index, track_definition in enumerate(
                component.tracks, start=1
            ):
                track_id = track_definition["id"]
                if (
                    active
                    and active["provider"] == provider
                    and str(active["track"]) == str(track_id)
                ):
                    state = f"activo · {active['version']}"
                else:
                    state = "disponible"
                track_rows.append(
                    f"[{index}] {track_definition['displayName']} · {state}"
                )
            track_rows.append("[Esc] Volver a proveedores")
            _print_panel(
                f"Catálogo > {component.display_name} > "
                f"{provider_definition['displayName']}",
                [("Línea", track_rows)],
            )
            track_index = _read_index(len(component.tracks))
            if track_index is None:
                break
            track = component.tracks[track_index]["id"]
            artifact = app.resolve(component.id, provider, track)
            active = _active_component(
                app, environment_id, component.id
            )
            if (
                active
                and active["provider"] == provider
                and str(active["track"]) == str(track)
                and active["version"] == artifact.version
            ):
                _start_page(
                    f"Inicio > Catálogo > {component.display_name} > "
                    "Selección activa"
                )
                _print_panel(
                    f"Catálogo > {component.display_name}",
                    [
                        (
                            "Selección activa",
                            [
                                _active_selection_text(
                                    component, active
                                ),
                                "Ya está instalada la última versión.",
                                "Pulse Intro o Esc para volver.",
                            ],
                        )
                    ],
                )
                _read_input("> ")
                continue

            plan_rows = [
                f"Fuente: {_component_source_label(component)}",
                f"Nueva: {provider_definition['displayName']} · "
                f"{_track_display(component, track)} · {artifact.version}"
            ]
            if active:
                plan_rows.insert(
                    0,
                    "Actual: "
                    + _active_selection_text(component, active),
                )
                plan_rows.append(
                    "La nueva selección sustituirá a la actual en este profile."
                )
            _start_page(
                f"Inicio > Catálogo > {component.display_name} > Instalar"
            )
            _print_panel(
                f"Catálogo > {component.display_name} > Instalar",
                [("Plan", plan_rows)],
            )
            _print_resolution(artifact)
            if not _confirm("¿Descargar e instalar?"):
                print("Cancelado.")
                _pause_after_result()
                continue
            _, path = app.install(
                environment_id,
                component.id,
                provider,
                track,
                artifact=artifact,
            )
            print(
                f"{component.display_name} activo en "
                f"{environment_id}: {path}"
            )
            _print_activation_notice(environment_id)
            _pause_after_result()
            return True


def _active_component(
    app: EapApplication,
    environment_id: str,
    component_id: str,
) -> dict[str, Any] | None:
    for item in app.inventory(environment_id):
        if item.get("id") == component_id:
            return item
    return None


def _component_source_label(component: Any) -> str:
    source = getattr(component, "source", None)
    if source is None:
        return "bootstrap (incluida con EAP)"
    return f"repositorio {source.id}"


def _component_source_rows(component: Any) -> list[str]:
    source = getattr(component, "source", None)
    if source is None:
        return ["Bootstrap · incluida con EAP"]
    return [
        f"Repositorio: {source.id}",
        f"URL: {source.repository_url}",
        f"Revisión: {source.revision}",
    ]


def _component_information_sections(
    app: EapApplication,
    environment_id: str,
    component: Any,
    numbered_paths: bool = False,
) -> list[tuple[str, list[str]]]:
    resolved_paths = _component_information_paths(
        app, environment_id, component
    )
    path_rows = []
    for index, (item, target, path_type) in enumerate(
        resolved_paths, start=1
    ):
        if numbered_paths:
            type_label = "Carpeta" if path_type == "directory" else "Archivo"
            path_rows.append(
                f"[{index}] {item['displayName']} · {type_label}: {target}"
            )
        else:
            path_rows.append(f"{item['displayName']}: {target}")
    return [
        ("Descripción", [component.information_description]),
        ("Rutas importantes", path_rows),
    ]


def _component_information_paths(
    app: EapApplication,
    environment_id: str,
    component: Any,
) -> list[tuple[dict[str, str], Path, str]]:
    desired = app.environments.read_desired(environment_id)
    roots = {
        "profile": (
            app.paths.data / "profiles" / str(desired["dataProfile"])
        ),
        "workspace": (
            app.paths.workspaces / str(desired["workspace"])
        ),
    }
    result: list[tuple[dict[str, str], Path, str]] = []
    for item in component.important_paths:
        root = roots[str(item["base"])].resolve()
        relative = PurePosixPath(str(item["relativePath"]))
        target = root.joinpath(*relative.parts).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                f"La ruta informativa sale de {item['base']}: "
                f"{item['relativePath']}"
            ) from exc
        path_type = str(item.get("type", "directory"))
        if path_type not in {"directory", "file"}:
            raise ValidationError(
                f"Tipo de ruta informativa no válido: {path_type}"
            )
        result.append((item, target, path_type))
    return result


def _interactive_component_information(
    app: EapApplication,
    environment_id: str,
    component: Any,
) -> None:
    while True:
        paths = _component_information_paths(
            app, environment_id, component
        )
        _start_page(f"Inicio > Información > {component.display_name}")
        _print_panel(
            f"Información > {component.display_name}",
            [
                *_component_information_sections(
                    app,
                    environment_id,
                    component,
                    numbered_paths=True,
                ),
                ("", ["[Esc] Volver"]),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        if not option.isdigit() or not 1 <= int(option) <= len(paths):
            print("Opción no válida.")
            _pause_after_result()
            continue
        item, target, path_type = paths[int(option) - 1]
        _open_component_information_path(
            app, str(item["displayName"]), target, path_type
        )


def _open_component_information_path(
    app: EapApplication,
    display_name: str,
    target: Path,
    path_type: str,
) -> None:
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if path_type == "directory":
        if target.exists() and not target.is_dir():
            raise ValidationError(
                f"La ruta declarada como carpeta es un archivo: {target}"
            )
        target.mkdir(parents=True, exist_ok=True)
        executable = windows_root / "explorer.exe"
        opened_message = "Carpeta abierta"
    elif path_type == "file":
        if target.exists() and not target.is_file():
            raise ValidationError(
                f"La ruta declarada como archivo es una carpeta: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        executable = _text_viewer_executable(app)
        opened_message = "Archivo abierto"
    else:
        raise ValidationError(
            f"Tipo de ruta informativa no válido: {path_type}"
        )
    if not executable.is_file():
        raise ValidationError(
            f"No se encuentra la aplicación de Windows: {executable}"
        )
    try:
        subprocess.Popen([str(executable), str(target)])
    except OSError as exc:
        raise ValidationError(
            f"No se pudo abrir {display_name}: {exc}"
        ) from exc
    print(f"{opened_message}: {target}")
    _pause_after_result()


def _text_viewer_executable(app: EapApplication) -> Path:
    configured = Settings.load(app.paths.config).get(
        "textViewer.executable"
    ).strip().strip('"')
    if not configured:
        raise ValidationError(
            "textViewer.executable no puede estar vacío"
        )
    expanded = os.path.expandvars(configured)
    candidate = Path(expanded).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    elif candidate.parent != Path("."):
        resolved = (app.paths.root / candidate).resolve()
    elif candidate.name.casefold() == "notepad.exe":
        windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        resolved = windows_root / "System32" / "notepad.exe"
    else:
        located = shutil.which(expanded)
        if located is None:
            raise ValidationError(
                "No se encuentra el visor de texto configurado en "
                f"textViewer.executable: {configured}"
            )
        resolved = Path(located).resolve()
    if not resolved.is_file():
        raise ValidationError(
            "No se encuentra el visor de texto configurado en "
            f"textViewer.executable: {resolved}"
        )
    return resolved


def _track_display(component: Any, track_id: int | str) -> str:
    for track in component.tracks:
        if str(track["id"]) == str(track_id):
            return str(track["displayName"])
    return f"Línea {track_id}"


def _active_selection_text(
    component: Any,
    active: dict[str, Any],
) -> str:
    provider = component.provider(str(active["provider"]))
    return (
        f"{provider['displayName']} · "
        f"{_track_display(component, active['track'])} · "
        f"{active['version']}"
    )


def _print_panel(
    title: str,
    sections: list[tuple[str, list[str]]],
) -> None:
    width = _terminal_panel_width()
    print()
    print(
        _style(
            _panel_border("┌", "┐", title, width),
            _ANSI_BOLD_CYAN,
        )
    )
    for section, rows in sections:
        if section:
            print(
                _style(
                    _panel_border("├", "┤", section, width),
                    _ANSI_CYAN,
                )
            )
        for row in rows:
            wrapped_rows = textwrap.wrap(
                row,
                width=width - 4,
                break_long_words=True,
                break_on_hyphens=False,
                subsequent_indent="  ",
            ) or [""]
            for content in wrapped_rows:
                print(
                    _colorize_panel_row(
                        _panel_row(content, width),
                        content,
                    )
                )
    print(
        _style(
            "└" + ("─" * (width - 2)) + "┘",
            _ANSI_CYAN,
        )
    )


def _start_page(breadcrumb: str) -> None:
    _clear_screen()
    _print_panel("Navegación", [("", [breadcrumb])])


def _clear_screen() -> None:
    if not _INTERACTIVE_ACTIVE or not _stream_is_terminal(sys.stdout):
        return
    print("\x1b[2J\x1b[H", end="", flush=True)


def _terminal_panel_width() -> int:
    terminal_width = shutil.get_terminal_size((96, 24)).columns
    return max(42, min(terminal_width, 180))


def _panel_border(
    left: str, right: str, label: str, width: int
) -> str:
    label = _fit_text(label, width - 5)
    prefix = f"{left}─ {label} "
    return prefix + ("─" * max(0, width - len(prefix) - 1)) + right


def _panel_row(text: str, width: int) -> str:
    content = _fit_text(text, width - 4)
    return f"│ {content.ljust(width - 4)} │"


def _fit_text(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    if length <= 1:
        return text[:length]
    return text[: length - 1] + "…"


def _configure_console_color() -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = False
    if (
        "NO_COLOR" in os.environ
        or os.environ.get("CLICOLOR") == "0"
        or os.environ.get("TERM", "").casefold() == "dumb"
        or not _stream_is_terminal(sys.stdout)
    ):
        return
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return
        except (AttributeError, OSError):
            return
    _COLOR_ENABLED = True


def _style(text: str, color: str, stream: Any | None = None) -> str:
    target = sys.stdout if stream is None else stream
    if not _COLOR_ENABLED or not _stream_is_terminal(target):
        return text
    return f"{color}{text}{_ANSI_RESET}"


def _colorize_panel_row(row: str, content: str) -> str:
    if not _COLOR_ENABLED or not _stream_is_terminal(sys.stdout):
        return row
    marker_pattern = re.compile(r"\[(?:\d+[ir]?|[A-Z]{1,2}|Esc)\]")
    if content.startswith("┌─"):
        colored = _style(row, _ANSI_CYAN)
        return marker_pattern.sub(
            lambda match: (
                f"{_ANSI_YELLOW}{match.group(0)}{_ANSI_CYAN}"
            ),
            colored,
        )
    if content.startswith("└─"):
        return _style(row, _ANSI_CYAN)
    replacements = (
        ("ERROR", _ANSI_RED),
        ("KO", _ANSI_RED),
        ("OK", _ANSI_GREEN),
        ("AVISO", _ANSI_YELLOW),
        ("✓", _ANSI_GREEN),
    )
    colored = row
    for token, color in replacements:
        colored = colored.replace(token, _style(token, color))
    profile_name = re.fullmatch(r"Nombre: (.+)", content)
    if profile_name:
        name = profile_name.group(1)
        colored = colored.replace(
            f"Nombre: {name}",
            f"Nombre: {_style(name, _ANSI_GREEN)}",
            1,
        )
    return marker_pattern.sub(
        lambda match: _style(match.group(0), _ANSI_YELLOW),
        colored,
    )


def _stream_is_terminal(stream: Any) -> bool:
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _print_status(message: str) -> None:
    if sys.stdout is not None:
        print(f"{_style('[EAP]', _ANSI_CYAN)} {message}")


def _print_error(message: str, *, pause: bool = False) -> None:
    if sys.stderr is not None:
        print(
            _style(f"ERROR: {message}", _ANSI_RED, stream=sys.stderr),
            file=sys.stderr,
        )
    if pause or _INTERACTIVE_ACTIVE:
        _pause_for_acknowledgement()


def _pause_after_error() -> None:
    _pause_for_acknowledgement()


def _pause_after_result() -> None:
    if _INTERACTIVE_ACTIVE:
        _pause_for_acknowledgement()


def _pause_for_acknowledgement() -> None:
    if not (
        _stream_is_terminal(sys.stdin) and _stream_is_terminal(sys.stdout)
    ):
        return
    print(
        _style("Pulse una tecla para continuar...", _ANSI_YELLOW),
        end="",
        flush=True,
    )
    if msvcrt is None:  # pragma: no cover - EAP se ejecuta en Windows
        input()
        return
    character = msvcrt.getwch()
    if character in {"\x00", "\xe0"}:
        msvcrt.getwch()
    print()
    if character == "\x03":
        raise KeyboardInterrupt


def _ensure_interactive_environment(app: EapApplication) -> str | None:
    configured = app.settings.get("profile.default")
    selected = app.environments.selected(configured)
    if selected:
        return selected
    environments = app.environments.list()
    _start_page("Inicio > Seleccionar profile")
    if not environments:
        print('EAP no tiene profiles. Se creará "default".')
        app.environments.create(configured)
        _pause_after_result()
        return configured
    print("Seleccione un profile:")
    for index, name in enumerate(environments, start=1):
        print(f"[{index}] {name}")
    print("[Esc] Salir")
    while True:
        raw = _read_input("> ").strip()
        if _is_escape(raw):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(environments):
            selected = environments[int(raw) - 1]
            app.environments.select(selected)
            return selected
        print("Selección no válida.")
        _pause_after_result()


def _interactive_manage_environments(
    app: EapApplication, current: str
) -> str:
    environments = app.environments.list()
    desired = app.environments.read_desired(current)
    _start_page("Inicio > Gestionar profile")
    _print_panel(
        "Gestionar profile",
        [
            (
                "Profile actual",
                [
                    f"Nombre: {current}",
                    f"Workspace: {desired['workspace']}",
                    f"Datos: {desired['dataProfile']}",
                ],
            ),
            (
                "Acciones",
                [
                    "[1] Seleccionar otro profile",
                    "[2] Crear profile",
                    "[3] Cambiar workspace del profile actual",
                    "[4] Cambiar datos del profile actual",
                    "[5] Exportar profile",
                    "[6] Importar profile",
                    "[7] Restaurar components ausentes",
                    "[8] Duplicar profile actual",
                    "[9] Eliminar profile",
                    "[Esc] Volver",
                ],
            ),
        ],
    )
    option = _read_input("> ").strip()
    if _is_escape(option):
        return current
    if option == "1":
        _start_page("Inicio > Gestionar profile > Seleccionar")
        for index, name in enumerate(environments, start=1):
            marker = " (actual)" if name == current else ""
            candidate = app.environments.read_desired(name)
            print(
                f"[{index}] {name}{marker} · "
                f"workspace {candidate['workspace']} · "
                f"datos {candidate['dataProfile']}"
            )
        print("[Esc] Volver")
        selected_index = _read_index(len(environments))
        if selected_index is None:
            return current
        selected = environments[selected_index]
        app.environments.select(selected)
        return selected
    if option == "2":
        return _interactive_create_environment(app, current)
    if option == "3":
        _interactive_change_workspace(app, current)
        return current
    if option == "4":
        _interactive_change_data_profile(app, current)
        return current
    if option == "5":
        _interactive_export_environment(app, current)
        return current
    if option == "6":
        return _interactive_import_environment(app) or current
    if option == "7":
        _interactive_restore_missing(app, current)
        return current
    if option == "8":
        return _interactive_duplicate_environment(app, current)
    if option == "9":
        return _interactive_delete_environment(app, current)
    print("Opción no válida.")
    _pause_after_result()
    return current


def _interactive_change_workspace(
    app: EapApplication, current: str
) -> None:
    _start_page("Inicio > Gestionar profile > Cambiar workspace")
    desired = app.environments.read_desired(current)
    workspace = _read_input(
        f"Workspace [{desired['workspace']}]: "
    ).strip()
    if _is_escape(workspace) or not workspace:
        return
    path = app.environments.set_workspace(current, workspace)
    print(f"Workspace activo: {path}")
    _pause_after_result()


def _interactive_change_data_profile(
    app: EapApplication, current: str
) -> None:
    data_profile = _interactive_choose_data_profile(
        app, f"{current}-data"
    )
    if data_profile is None:
        return
    _start_page("Inicio > Gestionar profile > Cambiar datos")
    path = app.environments.set_data_profile(current, data_profile)
    print(f"Datos activos del profile: {path}")
    _print_activation_notice(current)
    _pause_after_result()


def _interactive_create_environment(
    app: EapApplication, current: str
) -> str:
    _start_page("Inicio > Gestionar profile > Crear")
    name = _read_input("Nombre del profile: ").strip()
    if _is_escape(name) or not name:
        return current
    workspace = _read_input(f"Workspace [{name}]: ").strip()
    if _is_escape(workspace):
        return current
    data_profile = _interactive_choose_data_profile(app, name)
    if data_profile is None:
        return current
    _start_page("Inicio > Gestionar profile > Crear")
    app.environments.create(
        name,
        workspace_id=workspace or name,
        data_profile_id=data_profile,
    )
    desired = app.environments.read_desired(name)
    print(
        f"Profile creado: {name} · workspace {desired['workspace']} · "
        f"datos {desired['dataProfile']}"
    )
    _pause_after_result()
    return name


def _interactive_duplicate_environment(
    app: EapApplication, current: str
) -> str:
    _start_page("Inicio > Gestionar profile > Duplicar")
    name = _read_input(
        f"Nombre del nuevo profile basado en {current}: "
    ).strip()
    if _is_escape(name) or not name:
        return current
    desired = app.environments.read_desired(current)
    _print_panel(
        f"Duplicar profile · {current} -> {name}",
        [
            (
                "Se copiará",
                [
                    "Selección y versiones de componentes",
                    "Configuración privada del profile",
                    f"Workspace nuevo: {name}",
                    f"Datos compartidos: {desired['dataProfile']}",
                ],
            )
        ],
    )
    if not _confirm("¿Duplicar y seleccionar el nuevo profile?"):
        return current
    app.duplicate_profile(current, name)
    print(f"Profile duplicado y seleccionado: {current} -> {name}")
    _pause_after_result()
    return name


def _interactive_delete_environment(
    app: EapApplication, current: str
) -> str:
    _start_page("Inicio > Gestionar profile > Eliminar")
    environments = app.environments.list()
    if len(environments) <= 1:
        _print_panel(
            "Eliminar profile",
            [
                (
                    "",
                    [
                        "No se puede eliminar el único profile desde la "
                        "interfaz.",
                        "Cree otro profile primero.",
                        "Pulse Intro o Esc para volver.",
                    ],
                )
            ],
        )
        _read_input("> ")
        return current
    rows = [
        f"[{index}] {profile_id}"
        + (" (actual)" if profile_id == current else "")
        for index, profile_id in enumerate(environments, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel("Eliminar profile", [("Profiles", rows)])
    selected_index = _read_index(len(environments))
    if selected_index is None:
        return current
    profile_id = environments[selected_index]
    desired = app.environments.read_desired(profile_id)
    _print_panel(
        f"Eliminar profile · {profile_id}",
        [
            (
                "Alcance",
                [
                    "Se eliminarán su definición, lock y config.properties.",
                    f"Workspace conservado: {desired['workspace']}",
                    f"Datos conservados: {desired['dataProfile']}",
                    "Los payloads de components se conservarán.",
                ],
            )
        ],
    )
    if not _confirm("¿Eliminar este profile?"):
        return current
    selected = app.delete_profile(profile_id)
    print(f"Profile eliminado: {profile_id}")
    if profile_id != current:
        _pause_after_result()
        return current
    if selected is None:
        raise ValidationError("No queda ningún profile seleccionable")
    print(f"Profile seleccionado: {selected}")
    _pause_after_result()
    return selected


def _interactive_choose_data_profile(
    app: EapApplication, default_new_name: str
) -> str | None:
    profiles = app.environments.list_data_profiles()
    choice_rows = [
        "Los datos contienen el USERPROFILE portable, "
        "configuración y cachés.",
        "[1] Crear datos nuevos para el profile",
    ]
    if profiles:
        choice_rows.append("[2] Reutilizar datos existentes")
    choice_rows.append("[Esc] Cancelar")
    while True:
        _start_page("Inicio > Gestionar profile > Seleccionar datos")
        _print_panel("Datos del profile", [("Selección", choice_rows)])
        option = _read_input("> ").strip().lower()
        if _is_escape(option):
            return None
        if option in {"", "1"}:
            while True:
                name = _read_input(
                    f"Nombre del nuevo perfil [{default_new_name}] "
                    "[Esc: volver]: "
                ).strip()
                if _is_escape(name):
                    break
                selected = name or default_new_name
                if any(
                    selected.casefold() == existing.casefold()
                    for existing in profiles
                ):
                    print(
                        f"El perfil {selected} ya existe; puede reutilizarlo."
                    )
                    _pause_after_result()
                    break
                return selected
            continue
        if option == "2" and profiles:
            usage = _data_profile_usage(app)
            rows = []
            for index, profile in enumerate(profiles, start=1):
                environments = usage.get(profile.casefold(), [])
                detail = (
                    ", ".join(environments)
                    if environments
                    else "sin profile asociado"
                )
                rows.append(f"[{index}] {profile} · {detail}")
            rows.append("[Esc] Volver")
            _start_page(
                "Inicio > Gestionar profile > Seleccionar datos > Reutilizar"
            )
            _print_panel(
                "Datos del profile > Reutilizar",
                [("Perfiles disponibles", rows)],
            )
            selected_index = _read_index(len(profiles))
            if selected_index is not None:
                return profiles[selected_index]
            continue
        print("Opción no válida.")
        _pause_after_result()


def _data_profile_usage(app: EapApplication) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for environment_id in app.environments.list():
        desired = app.environments.read_desired(environment_id)
        profile_id = str(desired["dataProfile"])
        usage.setdefault(profile_id.casefold(), []).append(environment_id)
    return usage


def _interactive_export_environment(
    app: EapApplication, environment_id: str
) -> None:
    _start_page("Inicio > Gestionar profile > Exportar")
    name = _read_input("Nombre del profile exportado: ").strip()
    if _is_escape(name) or not name:
        return
    include_components = _confirm(
        "¿Incluir los payloads exactos de sus components?"
    )
    include_configuration = _confirm(
        "¿Incluir config.properties privado (puede contener tokens)?"
    )
    include_custom_commands = _confirm(
        "¿Incluir custom-commands del profile?"
    )
    result = app.export_environment(
        environment_id,
        name,
        include_components=include_components,
        include_configuration=include_configuration,
        include_custom_commands=include_custom_commands,
    )
    print(f"Paquete de profile: {result.archive}")
    print(
        "Configuración privada incluida: "
        + ("sí" if result.configuration_included else "no")
    )
    print(
        "Custom Commands incluidos: "
        + ("sí" if result.custom_commands_included else "no")
    )
    print(f"SHA256: {result.sha256}")
    _pause_after_result()


def _interactive_import_environment(app: EapApplication) -> str | None:
    _start_page("Inicio > Gestionar profile > Importar")
    archives = _environment_import_packages(app)
    if not archives:
        _print_panel(
            "Profiles > Importar",
            [
                (
                    "",
                    [
                        "No hay paquetes .7z pendientes de importar.",
                        f"Copie los paquetes directamente en: {app.paths.envs}",
                        "Pulse Intro o Esc para volver.",
                    ],
                )
            ],
        )
        _read_input("> ")
        return None

    rows = [
        f"[{index}] {archive.name} · "
        f"{archive.stat().st_size / (1024 * 1024):.1f} MiB"
        for index, archive in enumerate(archives, start=1)
    ]
    rows.extend(
        [
            "",
            "El paquete se eliminará únicamente si la importación finaliza bien.",
            "[Esc] Volver",
        ]
    )
    _print_panel("Profiles > Importar", [("Paquetes disponibles", rows)])
    selected_index = _read_index(len(archives))
    if selected_index is None:
        return None

    archive = archives[selected_index]
    result = app.import_environment(archive)
    print(f"Profile importado: {result.environment_id}")
    print(
        "Configuración privada importada: "
        + ("sí" if result.configuration_included else "no")
    )
    print(
        "Custom Commands importados: "
        + (
            "sí"
            if getattr(result, "custom_commands_included", False)
            else "no"
        )
    )
    try:
        _remove_imported_environment_package(app, archive)
    except (OSError, ValidationError) as exc:
        print(
            "AVISO: el profile se importó correctamente, pero no se pudo "
            f"eliminar el paquete {archive}: {exc}"
        )
    else:
        print(f"Paquete importado y eliminado: {archive}")
    _pause_after_result()
    if result.components_missing:
        _interactive_restore_missing(app, result.environment_id)
    return result.environment_id


def _environment_import_packages(app: EapApplication) -> list[Path]:
    inbox = app.paths.envs.resolve()
    archives: list[Path] = []
    for candidate in inbox.iterdir():
        if candidate.suffix.casefold() != ".7z" or candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.parent == inbox:
            archives.append(resolved)
    return sorted(archives, key=lambda archive: archive.name.casefold())


def _remove_imported_environment_package(
    app: EapApplication, archive: Path
) -> None:
    inbox = app.paths.envs.resolve()
    if archive.is_symlink():
        raise ValidationError("el paquete fue sustituido por un enlace")
    resolved = archive.resolve(strict=True)
    if resolved.parent != inbox or resolved.suffix.casefold() != ".7z":
        raise ValidationError("el paquete ya no pertenece a la bandeja envs")
    resolved.unlink()


def _interactive_export_tool(app: EapApplication) -> None:
    _start_page("Inicio > Opciones avanzadas > Exportar EAP")
    default_name = f"eap-{app.version}"
    name = _read_input(f"Nombre del 7z [{default_name}]: ").strip()
    if _is_escape(name):
        return
    include_components = _confirm(
        "¿Incluir todo el almacén components?"
    )
    result = app.export_tool(
        name or default_name,
        include_components=include_components,
    )
    print(f"Distribución portable de EAP: {result.archive}")
    print(f"SHA256: {result.sha256}")
    _pause_after_result()


def _interactive_pocketools(
    app: EapApplication, environment_id: str
) -> None:
    while True:
        _start_page("Inicio > Pocketools")
        installed = app.pocketools.installed()
        try:
            cached = app.available_pocketools(require_cache=True)
        except EapError:
            cached = []
        installed_rows = [
            f"{item['repository']}/{item['id']} · {item['version']} · "
            + ", ".join(
                str(command["name"])
                for command in item["manifest"]["commands"]
            )
            for item in installed
        ] or ["(sin Pocketools instaladas)"]
        _print_panel(
            "Pocketools",
            [
                ("Instaladas globalmente", installed_rows),
                (
                    "Índices guardados",
                    [
                        f"{len(cached)} Pocketool(s) indexada(s) · "
                        f"{len(app.pocketools.sources())} repositorio(s)"
                    ],
                ),
                (
                    "Acciones",
                    [
                        "[1] Explorar e instalar",
                        "[2] Buscar actualizaciones",
                        "[3] Desinstalar",
                        "[4] Ver ayuda",
                        "[5] Actualizar índices desde GitHub",
                        "[6] Gestionar repositorios",
                        "[Esc] Volver",
                    ],
                ),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        if option == "1":
            _interactive_install_pocketool(app, environment_id)
            continue
        if option == "2":
            _interactive_update_pocketool(app, environment_id)
            continue
        if option == "3":
            _interactive_uninstall_pocketool(app)
            continue
        if option == "4":
            _interactive_pocketool_help(app)
            continue
        if option == "5":
            values = app.refresh_pocketools()
            print(f"Índices actualizados: {len(values)} Pocketool(s).")
            _pause_after_result()
            continue
        if option == "6":
            _interactive_pocketool_repositories(app)
            continue
        print("Opción no válida.")
        _pause_after_result()


def _interactive_install_pocketool(
    app: EapApplication, environment_id: str
) -> None:
    _start_page("Inicio > Pocketools > Instalar")
    print("Consultando repositorios Pocketools...")
    definitions = sorted(
        app.available_pocketools(refresh=True),
        key=lambda item: (item.name.casefold(), item.source.id.casefold()),
    )
    installed = {
        (str(item["repository"]).casefold(), str(item["id"]).casefold()): item
        for item in app.pocketools.installed()
    }
    while True:
        _start_page("Inicio > Pocketools > Instalar")
        rows = []
        for index, definition in enumerate(definitions, start=1):
            current = installed.get(
                (definition.source.id.casefold(), definition.id.casefold())
            )
            suffix = (
                f" · instalada {current['version']}"
                if current is not None
                else ""
            )
            readme_option = (
                f" · [{index}i] README.md"
                if definition.readme_url is not None
                else ""
            )
            rows.append(
                f"[{index}] {definition.name} · {definition.version} · "
                f"{definition.selector}{suffix}{readme_option}"
            )
        rows.append("[Esc] Volver")
        _print_panel("Pocketools > Instalar", [("Disponibles", rows)])
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        if readme_match := re.fullmatch(r"(\d+)i", option):
            readme_index = int(readme_match.group(1)) - 1
            if not 0 <= readme_index < len(definitions):
                print("Opción no válida.")
                _pause_after_result()
                continue
            readme_url = definitions[readme_index].readme_url
            if readme_url is None:
                print("Esta Pocketool no publica un README.md navegable.")
                _pause_after_result()
                continue
            _open_pocketool_readme(
                definitions[readme_index].name, readme_url
            )
            continue
        if not option.isdigit() or not 1 <= int(option) <= len(definitions):
            print("Selección no válida.")
            _pause_after_result()
            continue
        selected_index = int(option) - 1
        break
    definition = definitions[selected_index]
    _start_page(
        f"Inicio > Pocketools > Instalar > {definition.name}"
    )
    _print_panel(
        f"Instalar {definition.name}",
        [
            (
                "Información",
                [
                    str(definition.value["description"]),
                    f"Origen: {definition.source.repository_url}",
                    f"Versión: {definition.version}",
                    "Comandos: "
                    + ", ".join(
                        str(command["name"])
                        for command in definition.commands
                    ),
                ],
            )
        ],
    )
    if not _confirm("¿Descargar e instalar esta Pocketool?"):
        return
    results = app.install_pocketool(
        definition.selector,
        environment_id,
        refresh=False,
    )
    for result in results:
        action = "Instalada" if result.changed else "Ya instalada"
        print(f"{action}: {result.selector} {result.version}")
    _pause_after_result()


def _open_pocketool_readme(name: str, url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
    except (OSError, webbrowser.Error) as exc:
        raise ValidationError(
            f"No se pudo abrir el README.md de {name}: {exc}"
        ) from exc
    if not opened:
        raise ValidationError(
            f"El navegador no pudo abrir el README.md de {name}"
        )
    print(f"README.md abierto en el navegador: {url}")
    _pause_after_result()


def _interactive_update_pocketool(
    app: EapApplication, environment_id: str
) -> None:
    _start_page("Inicio > Pocketools > Actualizar")
    print("Buscando actualizaciones Pocketools...")
    updates = app.pocketool_updates()
    if not updates:
        print("Todas las Pocketools están actualizadas.")
        _pause_after_result()
        return
    rows = [
        f"[{index}] {item['name']} · {item['currentVersion']} -> "
        f"{item['latestVersion']} · {item['repository']}/{item['id']}"
        for index, item in enumerate(updates, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel("Pocketools > Actualizar", [("Actualizaciones", rows)])
    selected_index = _read_index(len(updates))
    if selected_index is None:
        return
    selected = updates[selected_index]
    selector = f"{selected['repository']}/{selected['id']}"
    if not _confirm(f"¿Actualizar {selector}?"):
        return
    results = app.update_pocketool(selector, environment_id)
    for result in results:
        if result.changed:
            print(f"Actualizada: {result.selector} {result.version}")
    _pause_after_result()


def _interactive_uninstall_pocketool(app: EapApplication) -> None:
    _start_page("Inicio > Pocketools > Desinstalar")
    installed = app.pocketools.installed()
    if not installed:
        print("No hay Pocketools instaladas.")
        _pause_after_result()
        return
    rows = [
        f"[{index}] {item['name']} · {item['version']} · "
        f"{item['repository']}/{item['id']}"
        for index, item in enumerate(installed, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel("Pocketools > Desinstalar", [("Instaladas", rows)])
    selected_index = _read_index(len(installed))
    if selected_index is None:
        return
    selected = installed[selected_index]
    selector = f"{selected['repository']}/{selected['id']}"
    if not _confirm(
        f"¿Desinstalar {selector}? Sus datos persistentes se conservarán."
    ):
        return
    result = app.uninstall_pocketool(selector)
    print(f"Pocketool desinstalada: {selector}")
    if not result["payloadRemoved"]:
        print(f"Payload residual conservado: {result['residualPath']}")
    _pause_after_result()


def _interactive_pocketool_help(app: EapApplication) -> None:
    _start_page("Inicio > Pocketools > Ayuda")
    installed = app.pocketools.installed()
    if not installed:
        print("No hay Pocketools instaladas.")
        _pause_after_result()
        return
    rows = [
        f"[{index}] {item['name']} · {item['repository']}/{item['id']}"
        for index, item in enumerate(installed, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel("Pocketools > Ayuda", [("Instaladas", rows)])
    selected_index = _read_index(len(installed))
    if selected_index is None:
        return
    selected = installed[selected_index]
    value = app.pocketool_help(
        f"{selected['repository']}/{selected['id']}"
    )
    _start_page(
        f"Inicio > Pocketools > Ayuda > {value['name']}"
    )
    help_value = value["help"]
    _print_panel(
        f"{value['name']} {value['version']}",
        [
            (
                "Ayuda",
                [
                    help_value["summary"],
                    f"Uso: {help_value['usage']}",
                    *help_value.get("details", []),
                ],
            )
        ],
    )
    _read_input("Pulse Intro o Esc para volver. ")


def _interactive_pocketool_repositories(app: EapApplication) -> None:
    while True:
        _start_page("Inicio > Pocketools > Repositorios")
        sources = app.pocketools.sources()
        rows = [
            f"{source.id} · {source.repository_url}" for source in sources
        ] or ["(sin repositorios configurados)"]
        _print_panel(
            "Pocketools > Repositorios",
            [
                ("Fuentes", rows),
                (
                    "Acciones",
                    [
                        "[1] Añadir repositorio",
                        "[2] Quitar repositorio",
                        "[Esc] Volver",
                    ],
                ),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        if option == "1":
            source_id = _read_input("Id corto del repositorio: ").strip()
            if _is_escape(source_id) or not source_id:
                continue
            url = _read_input("URL HTTPS del repositorio GitHub: ").strip()
            if _is_escape(url) or not url:
                continue
            _print_panel(
                "Confiar en repositorio Pocketools",
                [
                    (
                        "Alcance",
                        [
                            f"Id: {source_id}",
                            f"URL: {url}",
                            "Podrá publicar manifiestos y archivos instalables.",
                        ],
                    )
                ],
            )
            if _confirm("¿Confiar y añadir este repositorio?"):
                app.add_pocketool_repository(source_id, url)
                print(f"Repositorio añadido: {source_id}")
                _pause_after_result()
            continue
        if option == "2":
            if not sources:
                print("No hay repositorios que quitar.")
                _pause_after_result()
                continue
            choices = [
                f"[{index}] {source.id} · {source.repository_url}"
                for index, source in enumerate(sources, start=1)
            ]
            choices.append("[Esc] Volver")
            _start_page(
                "Inicio > Pocketools > Repositorios > Quitar"
            )
            _print_panel("Quitar repositorio", [("Fuentes", choices)])
            selected_index = _read_index(len(sources))
            if selected_index is None:
                continue
            selected = sources[selected_index]
            if _confirm(f"¿Quitar el repositorio {selected.id}?"):
                app.remove_pocketool_repository(selected.id)
                print(f"Repositorio eliminado: {selected.id}")
                _pause_after_result()
            continue
        print("Opción no válida.")
        _pause_after_result()


def _interactive_advanced_options(
    app: EapApplication, current: str
) -> str | None:
    while True:
        _start_page("Inicio > Opciones avanzadas")
        _print_panel(
            "Opciones avanzadas",
            [
                (
                    "",
                    [
                        "[1] Exportar todos los profiles",
                        "[2] Importar todos los profiles",
                        "[3] Diagnóstico",
                        "[4] Limpiar temporales",
                        "[5] Integraciones con el Host",
                        "[6] Actualizar EAP",
                        "[7] Abrir configuración general",
                        "[8] Abrir configuración de un profile",
                        "[0] Exportar EAP",
                        "[Esc] Volver",
                    ],
                )
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return current
        if option == "0":
            _interactive_export_tool(app)
            continue
        if option == "3":
            _interactive_doctor(app)
            continue
        if option == "4":
            _interactive_clean_temporary_storage(app)
            continue
        if option == "5":
            _interactive_host_integrations(app, current)
            continue
        if option == "6":
            if _interactive_update_eap(app):
                return None
            continue
        if option == "7":
            _interactive_open_general_configuration(app)
            continue
        if option == "8":
            _interactive_open_profile_configuration(app, current)
            continue
        if option == "1":
            profiles = app.environments.list()
            if not profiles:
                print("No hay profiles para exportar.")
                _pause_after_result()
                continue
            _start_page(
                "Inicio > Opciones avanzadas > Exportar todos los profiles"
            )
            _print_panel(
                "Exportar todos los profiles",
                [
                    (
                        "Opciones predeterminadas",
                        [
                            f"Profiles: {len(profiles)}",
                            "Nombre de cada paquete: <profile>.7z",
                            "Payloads: no incluidos",
                            "config.properties privado: no incluido",
                            "custom-commands: no incluidos",
                        ],
                    )
                ],
            )
            if not _confirm("¿Exportar todos los profiles?"):
                continue
            exported, failures = _export_all_profiles(app)
            _print_batch_export_summary(exported, failures)
            continue
        if option == "2":
            archives = _environment_import_packages(app)
            if not archives:
                print(
                    f"No hay paquetes .7z en la bandeja {app.paths.envs}."
                )
                _pause_after_result()
                continue
            _start_page(
                "Inicio > Opciones avanzadas > Importar todos los profiles"
            )
            _print_panel(
                "Importar todos los profiles",
                [
                    (
                        "Paquetes",
                        [
                            *[archive.name for archive in archives],
                            "",
                            "Cada archivo se eliminará sólo después de una "
                            "importación correcta.",
                        ],
                    )
                ],
            )
            if not _confirm("¿Importar todos los profiles pendientes?"):
                continue
            imported, failures = _import_all_profiles(app, archives)
            _print_batch_import_summary(imported, failures)
            selected = app.environments.selected(
                app.settings.get("profile.default")
            )
            if selected is not None:
                current = selected
            continue
        print("Opción no válida.")
        _pause_after_result()


def _interactive_open_general_configuration(app: EapApplication) -> None:
    _start_page("Inicio > Opciones avanzadas > Configuración general")
    _open_component_information_path(
        app,
        "config.properties general",
        app.paths.config,
        "file",
    )


def _interactive_open_profile_configuration(
    app: EapApplication, current: str
) -> None:
    profiles = app.environments.list()
    _start_page("Inicio > Opciones avanzadas > Configuración de profile")
    if not profiles:
        print("No hay profiles configurados.")
        _pause_after_result()
        return
    rows = [
        f"[{index}] {profile}"
        f"{' (actual)' if profile == current else ''}"
        for index, profile in enumerate(profiles, start=1)
    ]
    rows.append("[Esc] Volver")
    _print_panel(
        "Abrir configuración de un profile",
        [("Profiles", rows)],
    )
    selected_index = _read_index(len(profiles))
    if selected_index is None:
        return
    selected = profiles[selected_index]
    _open_component_information_path(
        app,
        f"config.properties de {selected}",
        app.environments.ensure_config(selected),
        "file",
    )


def _interactive_host_integrations(
    app: EapApplication, environment_id: str
) -> None:
    while True:
        _start_page("Inicio > Opciones avanzadas > Integraciones con el Host")
        statuses = app.host_integration_statuses(environment_id)
        if not statuses:
            print("No hay integraciones con el host definidas.")
            _pause_after_result()
            return
        data_profile = statuses[0].data_profile
        profiles = app.host_integrations.profiles_using_data(data_profile)
        _print_panel(
            "Integraciones con el Host",
            [
                (
                    "Ámbito",
                    [
                        f"Datos compartidos: {data_profile}",
                        "Profiles afectados: "
                        + (", ".join(profiles) if profiles else "(ninguno)"),
                    ],
                ),
                (
                    "Integraciones",
                    [
                        f"[{index}] {status.display_name}: "
                        f"{'OK' if status.ok else 'KO'} · {status.detail}"
                        for index, status in enumerate(statuses, start=1)
                    ],
                ),
                ("", ["[Esc] Volver"]),
            ],
        )
        option = _read_input("> ").strip().lower()
        if _is_escape(option) or option in {"v", "volver", "q"}:
            return
        try:
            index = int(option) - 1
        except ValueError:
            print("Opción no válida.")
            _pause_after_result()
            continue
        if not 0 <= index < len(statuses):
            print("Opción no válida.")
            _pause_after_result()
            continue
        _interactive_host_integration(
            app, environment_id, statuses[index]
        )


def _interactive_host_integration(
    app: EapApplication,
    environment_id: str,
    status: Any,
) -> None:
    action = "Desactivar" if status.ok else "Activar"
    _start_page(
        "Inicio > Opciones avanzadas > Integraciones con el Host > "
        f"{status.display_name}"
    )
    link_rows: list[str] = []
    for link in status.links:
        link_rows.extend(
            [
                f"Origen host: {link.source}",
                f"Destino EAP: {link.destination}",
            ]
        )
    _print_panel(
        f"Integración con el Host · {status.display_name}",
        [
            (
                "Estado",
                [
                    f"{'OK' if status.ok else 'KO'} · {status.detail}",
                    status.description,
                ],
            ),
            ("Enlaces", link_rows),
            ("Acción", [f"[1] {action}", "[Esc] Volver"]),
        ],
    )
    option = _read_input("> ").strip().lower()
    if _is_escape(option) or option in {"v", "volver", "q"}:
        return
    if option != "1":
        print("Opción no válida.")
        _pause_after_result()
        return
    try:
        if status.ok:
            if not _confirm(
                "¿Desactivar la integración? Los datos del host "
                "se conservarán"
            ):
                return
            app.disable_host_integration(environment_id, status.id)
            print(
                "Integración desactivada. Se retiró el junction; "
                "los datos del host no se han eliminado."
            )
            _pause_after_result()
            return

        delete_existing = status.state == "inactive-with-data"
        if delete_existing:
            destinations = ", ".join(
                str(link.destination) for link in status.links
            )
            _print_panel(
                "Borrado necesario",
                [
                    (
                        "",
                        [
                            f"El directorio ya existe en {destinations}.",
                            "Su contenido se borrará permanentemente para "
                            "continuar.",
                        ],
                    )
                ],
            )
            if not _confirm("¿Desea borrarlo para continuar?"):
                return
        result = app.enable_host_integration(
            environment_id,
            status.id,
            delete_existing=delete_existing,
        )
        print(f"Integración {result.status.display_name} activada: OK.")
        if result.deleted_directories:
            for directory in result.deleted_directories:
                print(f"Directorio eliminado: {directory}")
            print(
                "Contenido eliminado: "
                f"{_format_bytes(result.deleted_bytes)} · "
                f"{result.deleted_files} archivo(s)."
            )
            print("El contenido eliminado no es recuperable desde EAP.")
        _pause_after_result()
    except EapError as exc:
        _print_error(str(exc), pause=True)


def _export_all_profiles(
    app: EapApplication,
) -> tuple[list[tuple[str, Any]], list[tuple[str, str]]]:
    exported: list[tuple[str, Any]] = []
    failures: list[tuple[str, str]] = []
    for profile_id in app.environments.list():
        try:
            result = app.export_environment(
                profile_id,
                profile_id,
                include_components=False,
                include_configuration=False,
                include_custom_commands=False,
            )
        except EapError as exc:
            failures.append((profile_id, str(exc)))
            continue
        exported.append((profile_id, result))
    return exported, failures


def _print_batch_export_summary(
    exported: list[tuple[str, Any]], failures: list[tuple[str, str]]
) -> None:
    for profile_id, result in exported:
        print(f"Exportado {profile_id}: {result.archive}")
    for profile_id, error in failures:
        _print_error(f"al exportar {profile_id}: {error}")
    print(
        f"Exportación masiva terminada: {len(exported)} correctos · "
        f"{len(failures)} errores."
    )
    _pause_after_result()


def _import_all_profiles(
    app: EapApplication,
    archives: list[Path] | None = None,
) -> tuple[list[tuple[Path, Any, bool]], list[tuple[str, str]]]:
    pending = (
        archives
        if archives is not None
        else _environment_import_packages(app)
    )
    imported: list[tuple[Path, Any, bool]] = []
    failures: list[tuple[str, str]] = []
    for archive in pending:
        try:
            result = app.import_environment(archive)
        except EapError as exc:
            failures.append((archive.name, str(exc)))
            continue
        removed = False
        try:
            _remove_imported_environment_package(app, archive)
            removed = True
        except (OSError, ValidationError) as exc:
            failures.append(
                (
                    archive.name,
                    "importado, pero no se pudo eliminar el paquete: "
                    + str(exc),
                )
            )
        imported.append((archive, result, removed))
    return imported, failures


def _print_batch_import_summary(
    imported: list[tuple[Path, Any, bool]],
    failures: list[tuple[str, str]],
) -> None:
    for archive, result, removed in imported:
        suffix = "paquete eliminado" if removed else "paquete conservado"
        print(
            f"Importado {result.environment_id} desde {archive.name} · "
            f"{suffix}."
        )
    for archive_name, error in failures:
        _print_error(f"con {archive_name}: {error}")
    print(
        f"Importación masiva terminada: {len(imported)} importados · "
        f"{len(failures)} errores."
    )
    _pause_after_result()


def _interactive_doctor(app: EapApplication) -> None:
    _start_page("Inicio > Opciones avanzadas > Diagnóstico")
    checks = app.doctor()
    _print_panel(
        "Diagnóstico",
        [
            (
                "",
                [
                    f"[{check['status'].upper()}] "
                    f"{check['name']}: {check['detail']}"
                    for check in checks
                ],
            ),
            ("Continuar", ["Pulse Intro o Esc para volver"]),
        ],
    )
    _read_input("> ")


def _interactive_clean_temporary_storage(app: EapApplication) -> None:
    _start_page("Inicio > Opciones avanzadas > Limpiar temporales")
    usage = app.temporary_storage_usage()
    _print_panel(
        "Limpiar temporales",
        [
            (
                "Almacenamiento",
                [
                    f"Ruta: {app.paths.temp}",
                    f"Tamaño: {_format_bytes(usage.bytes)}",
                    f"Archivos: {usage.files}",
                    "Se eliminarán descargas, staging, transacciones y logs.",
                ],
            )
        ],
    )
    if not _confirm("¿Eliminar todos los temporales de EAP?"):
        return
    result = app.clean_temporary_storage()
    print(
        f"Temporales eliminados: {_format_bytes(result.bytes_removed)} · "
        f"{result.files_removed} archivo(s)."
    )
    _pause_after_result()


def _interactive_update_eap(app: EapApplication) -> bool:
    _start_page("Inicio > Opciones avanzadas > Actualizar EAP")
    print("Comprobando la última release pública de EAP...")
    update = app.check_eap_update()
    if update.latest_version is None:
        print("Todavía no hay releases públicas de EAP.")
        _pause_after_result()
        return False
    if not update.update_available:
        print(f"EAP ya está actualizado: {update.current_version}")
        _pause_after_result()
        return False
    release = update.release
    _print_panel(
        "Actualizar EAP",
        [
            (
                "Release disponible",
                [
                    f"Versión actual: {update.current_version}",
                    f"Nueva versión: {update.latest_version}",
                    f"Publicada: {release.published_at if release else '-'}",
                    "Se conservarán profiles, datos, components, tools y "
                    "workspaces.",
                ],
            )
        ],
    )
    if not _confirm("¿Descargar e instalar la actualización de EAP?"):
        return False
    result = app.install_eap_update(update)
    print(f"EAP actualizado a {result.version}.")
    print("Cierre y vuelva a abrir EAP para aplicar el nuevo código.")
    _pause_after_result()
    return True


def _print_uninstall_result(display_name: str, result: Any) -> None:
    if result.payload_removed:
        print(f"{display_name} desinstalado; payload eliminado.")
    elif result.shared_profiles:
        print(
            f"{display_name} quitado del profile; payload conservado porque "
            "lo usan: " + ", ".join(result.shared_profiles)
        )
    elif result.residual_path is not None:
        print(
            f"AVISO: {display_name} se quitó del profile, pero quedó un "
            f"residuo temporal en {result.residual_path}."
        )
    else:
        print(
            f"{display_name} quitado del profile; el payload ya estaba ausente."
        )
    print("Los datos personales asociados se han conservado.")


def _format_bytes(size: int) -> str:
    value = float(max(size, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _interactive_restore_missing(
    app: EapApplication, environment_id: str
) -> None:
    missing = _missing_components(app, environment_id)
    if not missing:
        return
    _start_page(f"Inicio > Restaurar profile > {environment_id}")
    restorable = [
        item for item in missing if item.get("restorable", True)
    ]
    external = [
        item for item in missing if not item.get("restorable", True)
    ]
    rows = [
        f"• {app.catalog.component(str(item['id'])).display_name} · "
        f"{item['version']} · {item['reason']}"
        for item in missing
    ]
    rows.append("")
    if external:
        rows.append(
            "Los externos se vuelven a vincular desde el catálogo."
        )
    if restorable:
        rows.append("[1] Descargar y restaurar desde el lock")
        rows.append("[2] Continuar sin restaurar")
    rows.append("[Esc] Continuar sin restaurar")
    _print_panel(f"Profile incompleto: {environment_id}", [("", rows)])
    option = _read_input("> ").strip().lower()
    if option != "1" or not restorable:
        return
    restored = app.restore_missing_components(environment_id)
    print(f"Components restaurados: {len(restored)}")
    _print_activation_notice(environment_id)
    _pause_after_result()


def _print_activation_notice(environment_id: str) -> None:
    _print_panel(
        f"Profile {environment_id} actualizado",
        [
            (
                "Activación",
                [
                    "Los procesos y pestañas ya abiertos conservan sus "
                    "variables anteriores.",
                    "Abra una pestaña nueva con + para usar la nueva "
                    "selección.",
                    "Las aplicaciones que lance desde EAP ya reciben el "
                    "profile actualizado.",
                    "No es necesario reiniciar ni cerrar EAP.",
                ],
            )
        ],
    )


def _missing_components(
    app: EapApplication, environment_id: str
) -> list[dict[str, Any]]:
    resolver = getattr(app, "missing_components", None)
    if resolver is None:
        return []
    return list(resolver(environment_id))


def _read_index(length: int) -> int | None:
    while True:
        raw = _read_input("> ").strip()
        if _is_escape(raw) or raw.lower() in {"v", "volver", "q"}:
            return None
        if raw.isdigit() and 1 <= int(raw) <= length:
            return int(raw) - 1
        print("Selección no válida.")
        _pause_after_result()


def _require_environment(
    app: EapApplication, explicit: str | None
) -> str:
    if explicit:
        app.environments.read_desired(explicit)
        return explicit
    selected = app.environments.selected(app.settings.get("profile.default"))
    if selected is None:
        raise ValidationError(
            "No hay profile seleccionado; use eap.cmd profile create default"
        )
    return selected


def _component_selection(
    app: EapApplication,
    arguments: argparse.Namespace,
) -> tuple[str, int | str]:
    component = app.catalog.component(str(arguments.component))
    provider = arguments.provider or component.value.get("defaultProvider")
    if provider is None:
        provider = component.providers[0]["id"]
    component.provider(str(provider))
    track = arguments.track
    if track is None:
        track = component.value.get("defaultTrack")
    if track is None:
        track = component.tracks[0]["id"]
    return str(provider), component.validate_track(track)


def _find_locked_component(
    inventory: list[dict[str, Any]], component_id: str
) -> dict[str, Any]:
    for item in inventory:
        if item.get("id") == component_id:
            return item
    raise ValidationError(f"{component_id} no está instalado")


def _confirm_component_update(
    component: Any,
    update: UpdateInfo,
    *,
    assume_yes: bool = False,
    written_confirmation: str | None = None,
) -> bool:
    if not update.major_update:
        return assume_yes or _confirm("¿Actualizar este componente?")

    _print_panel(
        "¡Aviso importante! Versión mayor",
        [
            (
                "",
                [
                    f"{component.display_name}: "
                    f"{update.current_version} -> {update.latest.version}",
                    "Esta versión puede introducir cambios incompatibles "
                    "o requerir migraciones.",
                    "Lea las notas de la versión y las recomendaciones del "
                    "proveedor antes de continuar.",
                ],
            )
        ],
    )
    if not assume_yes and not _confirm(
        "¿Continuar con la actualización mayor?"
    ):
        return False

    confirmation = written_confirmation
    if confirmation is None:
        if assume_yes:
            raise ValidationError(
                "La actualización mayor requiere --confirm-major "
                f"{component.id}"
            )
        confirmation = _read_input(
            f"Escriba {component.id} y pulse Intro para confirmar: "
        )
    if confirmation.strip() != component.id:
        if assume_yes:
            raise ValidationError(
                "La confirmación de versión mayor no coincide; use "
                f"--confirm-major {component.id}"
            )
        print("El nombre no coincide. Actualización cancelada.")
        return False
    return True


def _confirm(message: str) -> bool:
    value = _read_input(f"{message} [s/N/Esc] ").strip().lower()
    if _is_escape(value):
        return False
    return value in {"s", "si", "sí", "y", "yes"}


def _read_input(prompt: str = "") -> str:
    if msvcrt is None or not sys.stdin.isatty():
        return input(prompt)
    print(prompt, end="", flush=True)
    characters: list[str] = []
    while True:
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            print()
            return "".join(characters)
        if character == _ESCAPE:
            print()
            return _ESCAPE
        if character == "\x03":
            print()
            raise KeyboardInterrupt
        if character in {"\x00", "\xe0"}:
            msvcrt.getwch()
            continue
        if character == "\b":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
            continue
        if character.isprintable():
            characters.append(character)
            print(character, end="", flush=True)


def _is_escape(value: str) -> bool:
    return value == _ESCAPE or value.strip().lower() in {
        "esc",
        "escape",
    }


def _print_resolution(artifact: Any) -> None:
    size = (
        f"{artifact.size / (1024 * 1024):.1f} MiB"
        if artifact.size is not None
        else "no publicado"
    )
    print(f"Componente: {artifact.family}")
    print(f"Proveedor: {artifact.provider_name}")
    print(f"Línea: {artifact.track}")
    print(f"Versión exacta: {artifact.version}")
    print(f"Archivo: {artifact.file_name}")
    print(f"Tamaño: {size}")
    print(
        f"{artifact.checksum_algorithm.upper()}: {artifact.checksum}"
    )


def _print_updates(updates: list[UpdateInfo]) -> None:
    if not updates:
        print("No hay actualizaciones disponibles.")
        return
    for item in updates:
        warning = " · VERSIÓN MAYOR" if item.major_update else ""
        print(
            f"{item.family}/{item.provider} línea {item.track}: "
            f"{item.current_version} -> {item.latest.version}{warning}"
        )


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
