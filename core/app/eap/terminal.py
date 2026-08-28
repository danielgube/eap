from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .core_tools import CoreTools
from .environments import EnvironmentStore
from .errors import ValidationError
from .paths import EapPaths
from .util import atomic_write_json


_TERMINAL_PROFILE_NAMESPACE = uuid.UUID(
    "956c69ce-6698-44bc-86e9-72ed8235f1e7"
)


@dataclass(frozen=True)
class TerminalConfiguration:
    environment_id: str
    settings_path: Path
    executable: Path
    manager_profile: str
    process_environment: dict[str, str]


@dataclass(frozen=True)
class TerminalLaunch:
    environment_id: str
    settings_path: Path
    process_id: int


@dataclass(frozen=True)
class ManagedTerminal:
    paths: EapPaths
    environments: EnvironmentStore
    catalog: Catalog
    core_tools: CoreTools

    def prepare(self, environment_id: str) -> TerminalConfiguration:
        process_environment = self.environments.build_process_environment(
            environment_id,
            self.catalog,
            allow_missing=True,
        )
        process_environment["EAP_MANAGED_TERMINAL"] = "1"
        terminal = self.core_tools.tool("windows-terminal")
        executable = terminal.executable("WindowsTerminal.exe")
        workspace = self.environments.workspace_path(environment_id)
        settings_path = (
            Path(process_environment["LOCALAPPDATA"])
            / "Microsoft"
            / "Windows Terminal"
            / "settings.json"
        )
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        manager_guid = self._profile_guid("manager")
        cmd_guid = self._profile_guid("cmd")
        powershell_guid = self._profile_guid("powershell")
        manager_name = "EAP · Gestor"
        python = self.paths.core / "python-embed" / "python.exe"
        if not python.is_file():
            raise ValidationError(
                f"No se encuentra el runtime privado de EAP: {python}"
            )
        base_command = [
            str(python),
            "-B",
            "-I",
            "-X",
            "utf8",
            "-m",
            "eap",
        ]
        settings = self._settings(
            manager_guid=manager_guid,
            cmd_guid=cmd_guid,
            powershell_guid=powershell_guid,
            manager_name=manager_name,
            manager_command=subprocess.list2cmdline(
                [
                    *base_command,
                    "--inline",
                    "--shell-on-exit",
                ]
            ),
            cmd_command=subprocess.list2cmdline(
                [*base_command, "shell", "--type", "cmd"]
            ),
            powershell_command=subprocess.list2cmdline(
                [*base_command, "shell", "--type", "powershell"]
            ),
            workspace=workspace,
        )
        atomic_write_json(settings_path, settings)
        process_environment["EAP_TERMINAL_SETTINGS"] = str(settings_path)
        return TerminalConfiguration(
            environment_id=environment_id,
            settings_path=settings_path,
            executable=executable,
            manager_profile=manager_name,
            process_environment=process_environment,
        )

    def start(self, environment_id: str) -> TerminalLaunch:
        configuration = self.prepare(environment_id)
        window_id = "eap-" + hashlib.sha256(
            str(self.paths.root).casefold().encode("utf-8")
        ).hexdigest()[:12]
        command = [
            str(configuration.executable),
            "--maximized",
            "-w",
            window_id,
            "new-tab",
            "--profile",
            configuration.manager_profile,
            "--useApplicationTitle",
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.paths.root,
                env=configuration.process_environment,
                close_fds=True,
            )
        except OSError as exc:
            raise ValidationError(
                f"No se pudo iniciar Windows Terminal portable: {exc}"
            ) from exc
        return TerminalLaunch(
            environment_id=environment_id,
            settings_path=configuration.settings_path,
            process_id=process.pid,
        )

    @staticmethod
    def _profile_guid(profile_id: str) -> str:
        return "{" + str(
            uuid.uuid5(_TERMINAL_PROFILE_NAMESPACE, profile_id)
        ) + "}"

    @staticmethod
    def _settings(
        *,
        manager_guid: str,
        cmd_guid: str,
        powershell_guid: str,
        manager_name: str,
        manager_command: str,
        cmd_command: str,
        powershell_command: str,
        workspace: Path,
    ) -> dict[str, Any]:
        common = {
            "closeOnExit": "automatic",
            "colorScheme": "EAP Dark",
            "font": {"face": "Cascadia Mono", "size": 11.0},
            "opacity": 98,
            "padding": "10, 8, 10, 8",
            "startingDirectory": str(workspace),
            "suppressApplicationTitle": False,
            "useAcrylic": False,
        }
        return {
            "$help": "https://aka.ms/terminal-documentation",
            "$schema": "https://aka.ms/terminal-profiles-schema",
            "actions": [],
            "alwaysShowTabs": True,
            "copyFormatting": "none",
            "copyOnSelect": False,
            "defaultProfile": cmd_guid,
            "disabledProfileSources": [
                "Windows.Terminal.Azure",
                "Windows.Terminal.PowershellCore",
                "Windows.Terminal.SSH",
                "Windows.Terminal.VisualStudio",
                "Windows.Terminal.Wsl",
            ],
            "initialCols": 150,
            "initialRows": 42,
            "launchMode": "maximized",
            "newTabMenu": [
                {"profile": cmd_guid, "type": "profile"},
                {"profile": powershell_guid, "type": "profile"},
            ],
            "profiles": {
                "defaults": {},
                "list": [
                    {
                        **common,
                        "commandline": manager_command,
                        "guid": manager_guid,
                        "hidden": True,
                        "name": manager_name,
                        "tabColor": "#238636",
                        "tabTitle": "EAP",
                    },
                    {
                        **common,
                        "commandline": cmd_command,
                        "guid": cmd_guid,
                        "hidden": False,
                        "name": "EAP · CMD",
                        "tabColor": "#1F6FEB",
                        "tabTitle": "EAP · CMD",
                    },
                    {
                        **common,
                        "commandline": powershell_command,
                        "guid": powershell_guid,
                        "hidden": False,
                        "name": "EAP · PowerShell",
                        "tabColor": "#8957E5",
                        "tabTitle": "EAP · PowerShell",
                    },
                    {
                        "guid": "{0caa0dad-35be-5f56-a8ff-afceeeaa6101}",
                        "hidden": True,
                        "name": "Command Prompt",
                    },
                    {
                        "guid": "{61c54bbd-c2c6-5271-96e7-009a87ff44bf}",
                        "hidden": True,
                        "name": "Windows PowerShell",
                    },
                ],
            },
            "schemes": [
                {
                    "background": "#0D1117",
                    "black": "#484F58",
                    "blue": "#58A6FF",
                    "brightBlack": "#6E7681",
                    "brightBlue": "#79C0FF",
                    "brightCyan": "#56D4DD",
                    "brightGreen": "#7EE787",
                    "brightPurple": "#D2A8FF",
                    "brightRed": "#FFA198",
                    "brightWhite": "#FFFFFF",
                    "brightYellow": "#E3B341",
                    "cursorColor": "#C9D1D9",
                    "cyan": "#39C5CF",
                    "foreground": "#C9D1D9",
                    "green": "#3FB950",
                    "name": "EAP Dark",
                    "purple": "#BC8CFF",
                    "red": "#FF7B72",
                    "selectionBackground": "#264F78",
                    "white": "#B1BAC4",
                    "yellow": "#D29922",
                }
            ],
            "showTabsInTitlebar": True,
            "tabWidthMode": "equal",
            "themes": [],
            "useAcrylicInTabRow": True,
        }
