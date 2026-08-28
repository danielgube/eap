from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import TransactionError, ValidationError
from .paths import EapPaths


_POWERSHELL_CREATE_SHORTCUT = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::Desktop
)
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw 'Windows no ha devuelto la ruta del escritorio'
}
$shortcutPath = Join-Path $desktop $env:EAP_SHORTCUT_FILENAME
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $env:EAP_SHORTCUT_TARGET
$shortcut.Arguments = $env:EAP_SHORTCUT_ARGUMENTS
$shortcut.WorkingDirectory = $env:EAP_SHORTCUT_WORKING_DIRECTORY
$shortcut.IconLocation = $env:EAP_SHORTCUT_ICON
$shortcut.Description = $env:EAP_SHORTCUT_DESCRIPTION
$shortcut.WindowStyle = 1
$shortcut.Save()
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Write-Output $shortcutPath
""".strip()


@dataclass(frozen=True)
class ShortcutResult:
    path: Path
    target: Path
    arguments: str
    icon: Path

    def as_json(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "target": str(self.target),
            "arguments": self.arguments,
            "icon": str(self.icon),
        }


class WindowsShortcutManager:
    def __init__(self, paths: EapPaths):
        self.paths = paths

    def create_desktop_shortcut(
        self,
        environment_id: str,
        launcher_id: str,
        display_name: str,
        icon: Path,
    ) -> ShortcutResult:
        pythonw = self.paths.core / "python-embed" / "pythonw.exe"
        if not pythonw.is_file():
            raise ValidationError(
                f"No existe el lanzador gráfico privado: {pythonw}"
            )
        icon = icon.resolve()
        if not icon.is_file():
            raise ValidationError(f"No existe el icono del launcher: {icon}")
        try:
            from .icons import create_eap_shortcut_icon
        except ImportError as exc:
            raise ValidationError(
                "El runtime privado no contiene Pillow para generar "
                "el icono EAP"
            ) from exc
        shortcut_icon = create_eap_shortcut_icon(
            icon,
            self.paths.data / "shortcut-icons" / environment_id,
            launcher_id,
        )
        file_name = self._safe_file_name(
            f"{display_name} ({environment_id})"
        ) + ".lnk"
        arguments = subprocess.list2cmdline(
            [
                "-B",
                "-I",
                "-X",
                "utf8",
                "-m",
                "eap.shortcut_entry",
                launcher_id,
                environment_id,
            ]
        )
        process_environment = dict(os.environ)
        process_environment.update(
            {
                "EAP_SHORTCUT_FILENAME": file_name,
                "EAP_SHORTCUT_TARGET": str(pythonw),
                "EAP_SHORTCUT_ARGUMENTS": arguments,
                "EAP_SHORTCUT_WORKING_DIRECTORY": str(self.paths.root),
                "EAP_SHORTCUT_ICON": f"{shortcut_icon},0",
                "EAP_SHORTCUT_DESCRIPTION": (
                    f"EAP · {display_name} · profile {environment_id}"
                ),
            }
        )
        system_root = Path(process_environment.get("SystemRoot", r"C:\Windows"))
        powershell = (
            system_root
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            raise ValidationError(
                f"No se encuentra Windows PowerShell: {powershell}"
            )
        try:
            completed = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _POWERSHELL_CREATE_SHORTCUT,
                ],
                cwd=self.paths.root,
                env=process_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransactionError(
                f"No se pudo crear el acceso directo: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = "\n".join(
                value.strip()
                for value in (completed.stdout, completed.stderr)
                if value.strip()
            )
            raise TransactionError(
                "Windows no pudo crear el acceso directo"
                + (f": {detail}" if detail else "")
            )
        output_lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        if not output_lines:
            raise TransactionError(
                "Windows no devolvió la ruta del acceso directo"
            )
        shortcut = Path(output_lines[-1]).resolve()
        if shortcut.name.casefold() != file_name.casefold():
            raise TransactionError(
                f"Windows devolvió un acceso directo inesperado: {shortcut}"
            )
        if not shortcut.is_file():
            raise TransactionError(
                f"No se ha generado el acceso directo: {shortcut}"
            )
        return ShortcutResult(
            path=shortcut,
            target=pythonw.resolve(),
            arguments=arguments,
            icon=shortcut_icon,
        )

    @staticmethod
    def _safe_file_name(value: str) -> str:
        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
        sanitized = sanitized.rstrip(" .")
        if not sanitized:
            raise ValidationError("El nombre del acceso directo está vacío")
        return sanitized[:120].rstrip(" .")
