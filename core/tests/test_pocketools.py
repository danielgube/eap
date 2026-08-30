from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eap.config import DEFAULTS, Settings
from eap.application import EapApplication
from eap.errors import IntegrityError, ValidationError
from eap.paths import EapPaths
from eap.pocketools import (
    PocketToolManager,
    semver_key,
    update_repository_property,
)
from eap.util import sha256_file


_DANIELGUBE_SETTINGS = {
    **DEFAULTS,
    "pocketools.repository.danielgube": (
        "https://github.com/danielgube/eap-pocketools"
    ),
}


class FakePocketToolClient:
    def __init__(self, catalog: dict, artifacts: dict[str, Path]):
        self.catalog = catalog
        self.artifacts = artifacts

    def get_json(self, url: str, maximum_bytes: int = 5 * 1024 * 1024):
        return self.catalog

    def download(
        self,
        url: str,
        destination: Path,
        progress=None,
        maximum_bytes: int | None = None,
    ) -> tuple[str, int]:
        source = self.artifacts[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return url, destination.stat().st_size


class FakeGitHubPocketToolClient:
    def __init__(self, manifest: dict):
        self.revision = "a" * 40
        self.files = {
            "pocketools/sessionkeep/pocketool.json": (
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            ),
            "pocketools/sessionkeep/README.md": b"# Session Keep\n",
            "pocketools/sessionkeep/src/sessionkeep.ps1": b"Write-Output 'ok'\n",
        }

    @staticmethod
    def object_id(content: bytes) -> str:
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(f"blob {len(content)}\0".encode("ascii"))
        digest.update(content)
        return digest.hexdigest()

    def get_json(self, url: str, maximum_bytes: int = 5 * 1024 * 1024):
        if url.endswith("/branches/main"):
            return {"commit": {"sha": self.revision}}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": path,
                        "type": "blob",
                        "mode": "100644",
                        "sha": self.object_id(content),
                        "size": len(content),
                    }
                    for path, content in self.files.items()
                ],
            }
        raise AssertionError(f"URL JSON inesperada: {url}")

    def get_text(
        self, url: str, maximum_bytes: int = 5 * 1024 * 1024
    ) -> str:
        path = url.split(f"/{self.revision}/", 1)[1]
        return self.files[path].decode("utf-8")

    def download(
        self,
        url: str,
        destination: Path,
        progress=None,
        maximum_bytes: int | None = None,
    ) -> tuple[str, int]:
        path = url.split(f"/{self.revision}/", 1)[1]
        content = self.files[path]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return url, len(content)


def pocketool_manifest(
    pocketool_id: str = "sessionkeep",
    version: str = "1.0.0",
    *,
    command: str | None = None,
    dependencies: list[dict] | None = None,
) -> dict:
    command_name = command or pocketool_id
    return {
        "schemaVersion": 1,
        "id": pocketool_id,
        "name": pocketool_id.title(),
        "version": version,
        "description": f"Utilidad {pocketool_id}",
        "license": "MIT",
        "platform": {"os": "windows", "architecture": "x64"},
        "help": {
            "summary": f"Ayuda de {pocketool_id}",
            "usage": f"{command_name} --help",
            "details": ["Detalle"],
        },
        "commands": [
            {
                "name": command_name,
                "type": "powershell",
                "entrypoint": f"src/{pocketool_id}.ps1",
                "arguments": [],
            }
        ],
        "requires": {
            "pocketools": dependencies or [],
            "components": [],
        },
        "install": {
            "requiredFiles": [f"src/{pocketool_id}.ps1"]
        },
    }


def build_artifact(root: Path, manifest: dict) -> tuple[Path, dict]:
    archive = root / f"{manifest['id']}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "pocketool.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        package.writestr("README.md", f"# {manifest['name']}\n")
        for required in manifest["install"]["requiredFiles"]:
            package.writestr(required, "Write-Output 'ok'\n")
    url = f"https://example.test/{archive.name}"
    entry = {
        **manifest,
        "artifact": {
            "url": url,
            "fileName": archive.name,
            "sha256": sha256_file(archive),
            "size": archive.stat().st_size,
        },
    }
    return archive, entry


class PocketToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = EapPaths.from_root(self.root)
        self.paths.ensure_layout()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(
        self, catalog: dict, artifacts: dict[str, Path] | None = None
    ) -> PocketToolManager:
        settings = Settings(
            {
                **DEFAULTS,
                "pocketools.repository.test": (
                    "https://example.test/pocketools.catalog.json"
                ),
            }
        )
        return PocketToolManager(
            self.paths,
            settings,
            FakePocketToolClient(catalog, artifacts or {}),
        )

    @staticmethod
    def catalog(entries: list[dict]) -> dict:
        return {
            "schemaVersion": 1,
            "repository": {"id": "test", "name": "Test Pocketools"},
            "pocketools": entries,
        }

    def test_github_repository_maps_to_main_branch_api(self) -> None:
        source = PocketToolManager(
            self.paths,
            Settings(_DANIELGUBE_SETTINGS),
            FakePocketToolClient({}, {}),
        ).source("danielgube")
        self.assertEqual(
            "https://api.github.com/repos/danielgube/"
            "eap-pocketools/branches/main",
            source.catalog_url,
        )
        self.assertEqual("github-tree", source.source_type)

    def test_github_tree_installs_directly_from_pinned_commit(self) -> None:
        manifest = pocketool_manifest()
        client = FakeGitHubPocketToolClient(manifest)
        manager = PocketToolManager(
            self.paths,
            Settings(_DANIELGUBE_SETTINGS),
            client,
        )

        available = manager.refresh("danielgube")
        self.assertEqual(1, len(available))
        self.assertEqual("github-tree", available[0].artifact["type"])
        self.assertEqual(client.revision, available[0].artifact["commit"])
        self.assertTrue(
            all(
                "raw.githubusercontent.com" in item["url"]
                and "/releases/" not in item["url"]
                for item in available[0].artifact["files"]
            )
        )
        result = manager.install_plan(
            manager.resolve_installation_plan(
                "danielgube/sessionkeep", refresh=False
            )
        )

        self.assertTrue(result[0].changed)
        self.assertTrue(
            (result[0].install_path / "src" / "sessionkeep.ps1").is_file()
        )
        marker = json.loads(
            (result[0].install_path / ".eap-pocketool.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(client.revision, marker["repositoryCommit"])

    def test_github_tree_rejects_a_changed_blob(self) -> None:
        client = FakeGitHubPocketToolClient(pocketool_manifest())
        manager = PocketToolManager(
            self.paths,
            Settings(_DANIELGUBE_SETTINGS),
            client,
        )
        manager.refresh("danielgube")
        client.files[
            "pocketools/sessionkeep/src/sessionkeep.ps1"
        ] = b"Write-Output 'NO'\n"

        with self.assertRaisesRegex(IntegrityError, "Objeto Git incorrecto"):
            manager.install_plan(
                manager.resolve_installation_plan(
                    "danielgube/sessionkeep", refresh=False
                )
            )

    def test_install_publishes_lock_payload_and_shim(self) -> None:
        manifest = pocketool_manifest()
        archive, entry = build_artifact(self.root, manifest)
        manager = self.manager(
            self.catalog([entry]),
            {entry["artifact"]["url"]: archive},
        )

        plan = manager.resolve_installation_plan("sessionkeep")
        result = manager.install_plan(plan)

        self.assertEqual(["test/sessionkeep"], [item.selector for item in result])
        self.assertTrue(result[0].changed)
        self.assertTrue((result[0].install_path / "src/sessionkeep.ps1").is_file())
        shim = self.paths.pocketools / "bin" / "sessionkeep.cmd"
        self.assertTrue(shim.is_file())
        self.assertIn("pocketool run", shim.read_text(encoding="utf-8"))
        self.assertEqual("1.0.0", manager.installed()[0]["version"])

    def test_dependency_plan_is_topological(self) -> None:
        base = pocketool_manifest("base")
        consumer = pocketool_manifest(
            "consumer",
            dependencies=[{"id": "base", "minimumVersion": "1.0.0"}],
        )
        _, base_entry = build_artifact(self.root, base)
        _, consumer_entry = build_artifact(self.root, consumer)
        manager = self.manager(self.catalog([consumer_entry, base_entry]))

        plan = manager.resolve_installation_plan("consumer")

        self.assertEqual(["base", "consumer"], [item.id for item in plan])

    def test_dependency_cycle_is_rejected(self) -> None:
        first = pocketool_manifest(
            "first",
            dependencies=[{"id": "second", "minimumVersion": "1.0.0"}],
        )
        second = pocketool_manifest(
            "second",
            dependencies=[{"id": "first", "minimumVersion": "1.0.0"}],
        )
        _, first_entry = build_artifact(self.root, first)
        _, second_entry = build_artifact(self.root, second)
        manager = self.manager(self.catalog([first_entry, second_entry]))

        with self.assertRaisesRegex(ValidationError, "Ciclo"):
            manager.resolve_installation_plan("first")

    def test_zip_traversal_is_rejected(self) -> None:
        manager = self.manager(self.catalog([]))
        archive = self.root / "traversal.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../outside.txt", "bad")

        with self.assertRaises(ValidationError):
            manager._extract_zip(archive, self.root / "output")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_command_collision_is_rejected(self) -> None:
        first = pocketool_manifest("first", command="same")
        second = pocketool_manifest("second", command="same")
        items = [
            {"repository": "test", "id": value["id"], "manifest": value}
            for value in (first, second)
        ]
        with self.assertRaisesRegex(ValidationError, "colisiona"):
            PocketToolManager._validate_command_collisions(items)

    def test_installed_dependency_blocks_uninstall(self) -> None:
        base = pocketool_manifest("base")
        consumer = pocketool_manifest(
            "consumer",
            dependencies=[{"id": "base", "minimumVersion": "1.0.0"}],
        )
        base_archive, base_entry = build_artifact(self.root, base)
        consumer_archive, consumer_entry = build_artifact(self.root, consumer)
        manager = self.manager(
            self.catalog([base_entry, consumer_entry]),
            {
                base_entry["artifact"]["url"]: base_archive,
                consumer_entry["artifact"]["url"]: consumer_archive,
            },
        )
        manager.install_plan(manager.resolve_installation_plan("consumer"))

        with self.assertRaisesRegex(ValidationError, "consumer"):
            manager.uninstall("base")
        manager.uninstall("consumer")
        result = manager.uninstall("base")
        self.assertTrue(result["payloadRemoved"])

    def test_component_requirements_use_active_profile_tracks(self) -> None:
        application = EapApplication.__new__(EapApplication)
        application.catalog = SimpleNamespace(
            component=lambda component_id: SimpleNamespace(
                value={
                    "capability": {
                        "id": "runtime.java",
                        "exclusive": True,
                    }
                }
            )
        )
        application.inventory = lambda environment_id: [
            {"id": "java", "track": 21}
        ]
        application._validate_pocketool_component_requirements(
            [{"capability": "runtime.java", "minimumTrack": 17}],
            "work",
            "test/tool",
        )
        with self.assertRaisesRegex(ValidationError, "línea >= 25"):
            application._validate_pocketool_component_requirements(
                [{"capability": "runtime.java", "minimumTrack": 25}],
                "work",
                "test/tool",
            )

    def test_windows_device_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reservado"):
            PocketToolManager._safe_relative_path(
                "src/CON.txt", "entrypoint"
            )

    def test_run_preserves_cwd_arguments_and_state(self) -> None:
        manifest = pocketool_manifest()
        archive, entry = build_artifact(self.root, manifest)
        manager = self.manager(
            self.catalog([entry]),
            {entry["artifact"]["url"]: archive},
        )
        manager.install_plan(manager.resolve_installation_plan("sessionkeep"))
        completed = SimpleNamespace(returncode=7)

        with patch("eap.pocketools.subprocess.run", return_value=completed) as run:
            code = manager.run(
                "sessionkeep",
                "sessionkeep",
                ["status", "value with spaces"],
                {"PATH": ""},
            )

        self.assertEqual(7, code)
        invocation = run.call_args.args[0]
        self.assertEqual(
            ["status", "value with spaces"], invocation[-2:]
        )
        self.assertEqual(Path.cwd(), run.call_args.kwargs["cwd"])
        child_environment = run.call_args.kwargs["env"]
        self.assertEqual("sessionkeep", child_environment["EAP_POCKETOOL_ID"])
        self.assertTrue(Path(child_environment["EAP_POCKETOOL_DATA"]).is_dir())

    def test_repository_property_preserves_other_configuration(self) -> None:
        config = self.root / "config.properties"
        config.write_text(
            "# general\nprofile.default=work\n",
            encoding="utf-8",
        )
        update_repository_property(
            config,
            "organization",
            "https://github.com/example/pocketools",
        )
        self.assertIn(
            "pocketools.repository.organization=",
            config.read_text(encoding="utf-8"),
        )
        update_repository_property(config, "organization", None)
        content = config.read_text(encoding="utf-8")
        self.assertIn("profile.default=work", content)
        self.assertNotIn("pocketools.repository.organization", content)

    def test_semver_is_strict(self) -> None:
        self.assertGreater(semver_key("2.0.0"), semver_key("1.99.99"))
        with self.assertRaises(ValidationError):
            semver_key("1.0")


if __name__ == "__main__":
    unittest.main()
