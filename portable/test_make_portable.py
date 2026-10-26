import importlib.util
import platform
import sys
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
    def test_supported_targets_have_a_release_asset(self):
        for target in MODULE.TARGETS.values():
            self.assertTrue(target.asset_names)
            self.assertTrue(target.binary_name)

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

    def test_local_binary_bundle_keeps_data_inside_bundle(self):
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
            self.assertTrue(result.name.endswith(".tar.gz"))
            self.assertTrue(result.with_name(result.name + ".sha256").is_file())

    def test_package_directory_preserves_upstream_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            (package / "bin").mkdir(parents=True)
            (package / "codex-resources").mkdir()
            (package / "codex-path").mkdir()
            (package / "bin" / "codex").write_bytes(b"codex")
            (package / "bin" / "codex-code-mode-host").write_bytes(b"host")
            (package / "codex-resources" / "bwrap").write_bytes(b"bwrap")
            (package / "codex-path" / "rg").write_bytes(b"rg")
            (package / "codex-package.json").write_text("{}", encoding="utf-8")
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
            import tarfile

            with tarfile.open(result, "r:gz") as archive:
                names = archive.getnames()
            self.assertTrue(any(name.endswith("/codex-resources/bwrap") for name in names))
            self.assertTrue(any(name.endswith("/codex-path/rg") for name in names))

    def test_host_target_is_supported_on_known_ci_platforms(self):
        if platform.system().lower() in {"linux", "darwin", "windows"}:
            self.assertIn(MODULE.select_target().name, MODULE.TARGETS)


if __name__ == "__main__":
    unittest.main()
