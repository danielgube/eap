from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


@dataclass(frozen=True)
class EapPaths:
    root: Path
    core: Path
    components: Path
    pocketools: Path
    data: Path
    envs: Path
    temp: Path
    exports: Path
    workspaces: Path
    catalog: Path
    config: Path

    @classmethod
    def discover(cls) -> "EapPaths":
        root = Path(__file__).resolve().parents[3]
        return cls.from_root(root)

    @classmethod
    def from_root(cls, root: Path) -> "EapPaths":
        resolved = root.resolve()
        return cls(
            root=resolved,
            core=resolved / "core",
            components=resolved / "components",
            pocketools=resolved / "pocketools",
            data=resolved / "data",
            envs=resolved / "envs",
            temp=resolved / "temp",
            exports=resolved / "exports",
            workspaces=resolved / "workspaces",
            catalog=resolved / "core" / "catalog" / "catalog.json",
            config=resolved / "config.properties",
        )

    def ensure_layout(self) -> None:
        for directory in (
            self.components,
            self.pocketools,
            self.pocketools / "bin",
            self.pocketools / "packages",
            self.data,
            self.data / "pocketools",
            self.data / "pocketools" / "catalogs",
            self.data / "pocketools" / "state",
            self.envs,
            self.temp,
            self.exports,
            self.workspaces,
            self.temp / "downloads",
            self.temp / "downloads" / "pocketools",
            self.temp / "staging",
            self.temp / "staging" / "pocketools",
            self.temp / "transactions",
            self.temp / "locks",
            self.temp / "logs",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def require_within_root(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValidationError(
                f"La ruta queda fuera de EAP: {resolved}"
            ) from exc
        return resolved
