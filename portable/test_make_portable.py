import importlib.util
import json
import platform
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("make_portable.py")
SPEC = importlib.util.spec_from_file_location("make_portable", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PortableMakerTests(unittest.TestCase):
    def test_supported_targets_have_release_assets_and_triples(self):
        for target in MODULE.TARGETS.values():
            self.assertTrue(target.asset_names)
            self.assertTrue(target.binary_name)
            self.assertTrue(target.upstream_target)

    def test_target_aliases(self):
        self.assertEqual(MODULE.select_target("linux-x64").name, "linux-x86_64")
        self.assertEqual(MODULE.select_target("linux-x86_64").name, "linux-x86_64")
        self.assertEqual(MODULE.select_target("darwin-arm64").name, "macos-aarch64")
        self.assertEqual(MODULE.select_target("win32-x64").name, "windows-x86_64")

    def test_asset_selection_prefers_non_bazel_release_asset(self):
        release = {
            "tag_name": "rust-v0.164.0",
            "assets": [
                {"name": "codex-package-x86_64-unknown-linux-musl-bazel.tar.gz"},
                {"name": "codex-package-x86_64-unknown-linux-musl.tar.gz"},
            ],
        }
        asset = MODULE.choose_asset(release, MODULE.TARGETS["linux-x86_64"])
        self.assertEqual(asset["name"], "codex-package-x86_64-unknown-linux-musl.tar.gz")

    def test_entrypoint_only_bundle_isolated_and_daemon_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "codex"
            binary.write_bytes(b"portable-test-binary")
            output = root / "dist"
            result = MODULE.build(
                MODULE.parser().parse_args(
                    [
                        "--target",
                        "linux-x86_64",
                        "--binary",
                        str(binary),
                        "--output",
                        str(output),
                        "--repository-root",
                        str(root),
                    ]
                )
            )
            self.assertTrue(result.is_file())
            with tarfile.open(result, "r:gz") as archive:
                root_name = result.name.removesuffix(".tar.gz")
                names = set(archive.getnames())
                self.assertIn(f"{root_name}/codex-package/bin/codex", names)
                self.assertNotIn(f"{root_name}/bin/codex", names)
                config = archive.extractfile(
                    f"{root_name}/data/codex/config.toml"
                ).read().decode("utf-8")
                self.assertEqual(config, "[features]\ndaemon_auto_start = false\n")
                self.assertEqual(
                    archive.getmember(f"{root_name}/run.sh").mode & 0o111,
                    0o111,
                )
                self.assertEqual(
                    archive.getmember(f"{root_name}/codex-package/bin/codex").mode & 0o111,
                    0o111,
                )
            self.assertTrue(result.with_name(result.name + ".sha256").is_file())

    def _make_package(self, root: Path) -> Path:
        package = root / "package"
        (package / "bin").mkdir(parents=True)
        (package / "codex-resources").mkdir()
        (package / "codex-path").mkdir()
        (package / "bin" / "codex").write_bytes(b"codex")
        (package / "bin" / "codex-code-mode-host").write_bytes(b"host")
        (package / "codex-resources" / "bwrap").write_bytes(b"bwrap")
        (package / "codex-path" / "rg").write_bytes(b"rg")
        (package / "codex-package.json").write_text(
            json.dumps(
                {
                    "layoutVersion": 1,
                    "version": "0.164.0",
                    "target": "x86_64-unknown-linux-musl",
                    "variant": "codex",
                    "entrypoint": "bin/codex",
                    "resourcesDir": "codex-resources",
                    "pathDir": "codex-path",
                }
            ),
            encoding="utf-8",
        )
        return package

    def test_package_directory_is_nested_and_preserves_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._make_package(root)
            output = root / "dist"
            result = MODULE.build(
                MODULE.parser().parse_args(
                    [
                        "--target",
                        "linux-x86_64",
                        "--package-dir",
                        str(package),
                        "--output",
                        str(output),
                        "--repository-root",
                        str(root),
                    ]
                )
            )
            with tarfile.open(result, "r:gz") as archive:
                names = archive.getnames()
            self.assertTrue(any(name.endswith("/codex-package/codex-resources/bwrap") for name in names))
            self.assertTrue(any(name.endswith("/codex-package/codex-path/rg") for name in names))
            self.assertTrue(any(name.endswith("/codex-package/bin/codex-code-mode-host") for name in names))

    def test_package_directory_rejects_winget_style_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            (package / "codex-x86_64-pc-windows-msvc.exe").write_bytes(b"stub")
            (package / "codex-package.json").write_text(
                json.dumps(
                    {
                        "layoutVersion": 1,
                        "target": "x86_64-pc-windows-msvc",
                        "entrypoint": "codex-x86_64-pc-windows-msvc.exe",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                MODULE.build(
                    MODULE.parser().parse_args(
                        [
                            "--target",
                            "windows-x86_64",
                            "--package-dir",
                            str(package),
                            "--output",
                            str(root / "dist"),
                            "--repository-root",
                            str(root),
                        ]
                    )
                )

    def test_tar_permissions_are_restored_for_unix_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._make_package(root)
            for path in package.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            result = MODULE.build(
                MODULE.parser().parse_args(
                    [
                        "--target",
                        "linux-x86_64",
                        "--package-dir",
                        str(package),
                        "--output",
                        str(root / "dist"),
                        "--repository-root",
                        str(root),
                    ]
                )
            )
            with tarfile.open(result, "r:gz") as archive:
                executable_names = [
                    name
                    for name in archive.getnames()
                    if name.endswith("/run.sh")
                    or name.endswith("/codex-package/bin/codex")
                    or name.endswith("/codex-package/bin/codex-code-mode-host")
                    or name.endswith("/codex-package/codex-path/rg")
                    or name.endswith("/codex-package/codex-resources/bwrap")
                ]
                self.assertTrue(executable_names)
                for name in executable_names:
                    self.assertEqual(archive.getmember(name).mode & 0o111, 0o111)

    def test_host_target_is_supported_on_known_ci_platforms(self):
        if platform.system().lower() in {"linux", "darwin", "windows"}:
            self.assertIn(MODULE.select_target().name, MODULE.TARGETS)


if __name__ == "__main__":
    unittest.main()
