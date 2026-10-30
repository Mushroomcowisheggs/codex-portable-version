#!/usr/bin/env python3
"""Build a self-contained Codex CLI bundle without modifying Codex itself.

The maker consumes an official Codex release asset by default. A local binary can
be supplied with --binary when testing a locally built upstream checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

UPSTREAM_REPOSITORY = os.environ.get("CODEX_PORTABLE_UPSTREAM", "openai/codex")
GITHUB_API = "https://api.github.com"
NETWORK_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Target:
    name: str
    os_name: str
    architecture: str
    upstream_target: str
    asset_names: tuple[str, ...]
    binary_name: str
    archive_suffix: str


TARGETS: dict[str, Target] = {
    "linux-x86_64": Target(
        "linux-x86_64",
        "linux",
        "x86_64",
        "x86_64-unknown-linux-musl",
        ("codex-package-x86_64-unknown-linux-musl.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "linux-aarch64": Target(
        "linux-aarch64",
        "linux",
        "aarch64",
        "aarch64-unknown-linux-musl",
        ("codex-package-aarch64-unknown-linux-musl.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "macos-x86_64": Target(
        "macos-x86_64",
        "macos",
        "x86_64",
        "x86_64-apple-darwin",
        ("codex-package-x86_64-apple-darwin.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "macos-aarch64": Target(
        "macos-aarch64",
        "macos",
        "aarch64",
        "aarch64-apple-darwin",
        ("codex-package-aarch64-apple-darwin.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "windows-x86_64": Target(
        "windows-x86_64",
        "windows",
        "x86_64",
        "x86_64-pc-windows-msvc",
        ("codex-package-x86_64-pc-windows-msvc.tar.gz",),
        "codex.exe",
        ".zip",
    ),
    "windows-aarch64": Target(
        "windows-aarch64",
        "windows",
        "aarch64",
        "aarch64-pc-windows-msvc",
        ("codex-package-aarch64-pc-windows-msvc.tar.gz",),
        "codex.exe",
        ".zip",
    ),
}

UNIX_LAUNCHER = """#!/usr/bin/env sh
set -eu

SCRIPT=$0
case "$SCRIPT" in
  /*) ;;
  *) SCRIPT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)/$(basename -- "$SCRIPT");;
esac
while [ -L "$SCRIPT" ]; do
  LINK=$(readlink "$SCRIPT")
  case "$LINK" in
    /*) SCRIPT=$LINK ;;
    *) SCRIPT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)/$LINK ;;
  esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)
CODEX_HOME="$ROOT/data/codex"
CODEX_SQLITE_HOME="$CODEX_HOME/state"
mkdir -p "$CODEX_HOME" "$CODEX_SQLITE_HOME"
export CODEX_HOME CODEX_SQLITE_HOME

exec "$ROOT/codex-package/bin/codex" "$@"
"""

MACOS_LAUNCHER = """#!/usr/bin/env sh
set -eu

SCRIPT=$0
case "$SCRIPT" in
  /*) ;;
  *) SCRIPT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)/$(basename -- "$SCRIPT");;
esac
while [ -L "$SCRIPT" ]; do
  LINK=$(readlink "$SCRIPT")
  case "$LINK" in
    /*) SCRIPT=$LINK ;;
    *) SCRIPT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)/$LINK ;;
  esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT")" && pwd)
exec "$ROOT/run.sh" "$@"
"""

WINDOWS_LAUNCHER = r'''@echo off
setlocal
set "ROOT=%~dp0"
set "CODEX_HOME=%ROOT%data\codex"
set "CODEX_SQLITE_HOME=%CODEX_HOME%\state"
if not exist "%CODEX_HOME%" mkdir "%CODEX_HOME%"
if not exist "%CODEX_SQLITE_HOME%" mkdir "%CODEX_SQLITE_HOME%"
"%ROOT%codex-package\bin\codex.exe" %*
exit /b %ERRORLEVEL%
'''

POWERSHELL_LAUNCHER = r'''$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:CODEX_HOME = Join-Path $root "data\codex"
$env:CODEX_SQLITE_HOME = Join-Path $env:CODEX_HOME "state"
New-Item -ItemType Directory -Force -Path $env:CODEX_HOME, $env:CODEX_SQLITE_HOME | Out-Null
& (Join-Path $root "codex-package\bin\codex.exe") @args
exit $LASTEXITCODE
'''


def github_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-portable-maker",
        },
    )
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        return json.load(response)


def release_url(repository: str, tag: str | None) -> str:
    encoded_repo = urllib.parse.quote(repository, safe="/")
    if tag:
        return f"{GITHUB_API}/repos/{encoded_repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    return f"{GITHUB_API}/repos/{encoded_repo}/releases/latest"


def select_target(value: str | None = None) -> Target:
    if value:
        normalized = value.lower().replace("_", "-")
        aliases = {
            "darwin-x64": "macos-x86_64",
            "darwin-arm64": "macos-aarch64",
            "macos-x64": "macos-x86_64",
            "macos-arm64": "macos-aarch64",
            "macos-x86-64": "macos-x86_64",
            "macos-aarch-64": "macos-aarch64",
            "win32-x64": "windows-x86_64",
            "win32-arm64": "windows-aarch64",
            "windows-x86-64": "windows-x86_64",
            "windows-aarch-64": "windows-aarch64",
            "linux-x64": "linux-x86_64",
            "linux-arm64": "linux-aarch64",
            "linux-x86-64": "linux-x86_64",
            "linux-aarch-64": "linux-aarch64",
        }
        normalized = aliases.get(normalized, normalized)
        try:
            return TARGETS[normalized]
        except KeyError as exc:
            choices = ", ".join(sorted(TARGETS))
            raise ValueError(f"Unknown target {value!r}. Choose one of: {choices}") from exc

    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(system)
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(machine)
    if not os_name or not architecture:
        raise ValueError(f"Cannot infer a supported target from {system}/{machine}")
    return TARGETS[f"{os_name}-{architecture}"]


def choose_asset(release: dict[str, Any], target: Target) -> dict[str, Any]:
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    for name in target.asset_names:
        if name in assets:
            return assets[name]

    # Keep following minor upstream asset naming changes without accepting the
    # Bazel variants or compressed formats this standard-library maker cannot
    # unpack. The exact preferred names above still win whenever available.
    preferred = target.asset_names[0]
    if preferred.endswith(".tar.gz"):
        prefix = preferred[: -len(".tar.gz")]
        suffix = ".tar.gz"
    else:
        prefix = preferred[: -len(".exe")]
        suffix = ".exe"
    compatible = [
        asset
        for name, asset in assets.items()
        if name.startswith(prefix)
        and name.endswith(suffix)
        and "-bazel" not in name
    ]
    if compatible:
        return sorted(compatible, key=lambda asset: asset["name"])[0]

    available = ", ".join(sorted(assets))
    raise RuntimeError(
        f"Release {release.get('tag_name', '<unknown>')} has no asset for {target.name}. "
        f"Expected one of {target.asset_names}; available assets: {available}"
    )

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-portable-maker"})
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)



def expected_package_entrypoint(target: Target) -> str:
    return f"bin/{target.binary_name}"


def required_package_files(target: Target) -> tuple[Path, ...]:
    files = [
        Path("bin") / target.binary_name,
        Path("bin") / ("codex-code-mode-host.exe" if target.os_name == "windows" else "codex-code-mode-host"),
        Path("codex-path") / ("rg.exe" if target.os_name == "windows" else "rg"),
    ]
    if target.os_name == "linux":
        files.append(Path("codex-resources") / "bwrap")
    if target.os_name == "windows":
        files.extend(
            [
                Path("codex-resources") / "codex-command-runner.exe",
                Path("codex-resources") / "codex-windows-sandbox-setup.exe",
            ]
        )
    return tuple(files)


def validate_package_root(root: Path, target: Target) -> None:
    metadata_path = root / "codex-package.json"
    if not metadata_path.is_file():
        raise RuntimeError(f"Missing package metadata: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid package metadata: {metadata_path}") from error
    expected_entrypoint = expected_package_entrypoint(target)
    if metadata.get("entrypoint") != expected_entrypoint:
        raise RuntimeError(
            f"Package entrypoint must be {expected_entrypoint!r}; "
            f"got {metadata.get('entrypoint')!r}"
        )
    package_target = metadata.get("target")
    if package_target is not None and package_target != target.upstream_target:
        raise RuntimeError(
            f"Package target must be {target.upstream_target!r}; got {package_target!r}"
        )
    for relative_path in required_package_files(target):
        if not (root / relative_path).is_file():
            raise RuntimeError(f"Missing package file: {relative_path}")


def _safe_destination(root: Path, member_name: str) -> Path:
    root = root.resolve()
    destination = (root / member_name).resolve()
    if destination != root and root not in destination.parents:
        raise RuntimeError(f"Archive contains an unsafe path: {member_name!r}")
    return destination


def _extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        for member in members:
            _safe_destination(destination, member.name)
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(f"Archive contains an unsupported link/device: {member.name!r}")
        if sys.version_info >= (3, 12):
            stream.extractall(destination, filter="data")
        else:
            stream.extractall(destination)


def _extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as stream:
        for member in stream.infolist():
            _safe_destination(destination, member.filename)
        stream.extractall(destination)


def _find_binary(root: Path, binary_name: str) -> Path:
    candidates = [path for path in root.rglob(binary_name) if path.is_file()]
    if not candidates:
        raise RuntimeError(f"Could not find {binary_name!r} in {root}")
    return min(
        candidates,
        key=lambda path: (0 if "release" in path.parts else 1, len(path.parts), str(path)),
    )


def _find_package_root(root: Path, binary_name: str) -> Path | None:
    metadata_roots = [
        path.parent
        for path in root.rglob("codex-package.json")
        if (path.parent / "bin" / binary_name).is_file()
    ]
    if metadata_roots:
        return min(metadata_roots, key=lambda path: (len(path.parts), str(path)))
    binary_roots = [
        path.parent.parent
        for path in root.rglob(binary_name)
        if path.is_file() and path.parent.name == "bin"
    ]
    if binary_roots:
        return min(binary_roots, key=lambda path: (len(path.parts), str(path)))
    return None


def _copy_runtime_tree(runtime_root: Path, stage: Path) -> None:
    for source in sorted(runtime_root.rglob("*")):
        relative = source.relative_to(runtime_root)
        destination = stage / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        else:
            raise RuntimeError(f"Package contains an unsupported filesystem entry: {relative}")


def resolve_local_binary(source: Path, target: Target) -> Path:
    source = source.expanduser().resolve()
    if source.is_file():
        return source
    if not source.is_dir():
        raise FileNotFoundError(source)
    direct = source / target.binary_name
    if direct.is_file():
        return direct
    candidates = [
        source / "target" / "release" / target.binary_name,
        source / "codex-rs" / "target" / "release" / target.binary_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return _find_binary(source, target.binary_name)


def write_text(path: Path, content: str, executable: bool = False) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def bundle_readme(
    release: dict[str, Any],
    target: Target,
    asset: dict[str, Any],
    binary_digest: str,
    repository: str,
    package_layout: str,
) -> str:
    package_note = (
        "The official package layout is preserved under 'codex-package/', including "
        "the code-mode host, platform sandbox helpers, and bundled ripgrep."
        if package_layout == "official-codex-package"
        else "This local bundle contains only the supplied entrypoint. Daemon auto-start is disabled "
        "because an entrypoint-only smoke bundle has no complete official package."
    )
    return f"""# Codex Portable

This bundle contains the official Codex CLI binary for **{target.name}**.

- Upstream: https://github.com/{repository}
- Release: '{release.get('tag_name', 'local')}'
- Source asset: '{asset.get('name', 'local binary')}'
- Binary SHA-256: '{binary_digest}'

## Run

Use 'run.sh' on Linux/macOS, 'run.command' on macOS, 'run.bat' on Windows, or
'run.ps1' from PowerShell. The launcher sets CODEX_HOME to 'data/codex' and
CODEX_SQLITE_HOME to 'data/codex/state'. The package is in 'codex-package/',
so the daemon can keep its managed installation beside the package rather than
inside it.

The current working directory is preserved; launch the bundle from the project
you want Codex to work on. Arguments are passed through unchanged.

{package_note}

This is a packaging layer around upstream Codex; it does not patch or replace
Codex source code. The bundle still uses host operating-system integrations:
OS keyrings, host git and shells, plugin/runtime caches, user skills, Windows
sandbox provisioning, and platform signing or notarization remain host concerns.
The Windows daemon must be started from a non-elevated terminal. Rebuild the
bundle with a new pinned release to upgrade it; no in-place updater is enabled.
"""




def copy_legal_files(repository_root: Path, destination: Path) -> None:
    for name in ("LICENSE", "NOTICE"):
        source = repository_root / name
        if source.is_file():
            shutil.copy2(source, destination / name)


def should_have_execute_bit(relative_path: str, target: Target) -> bool:
    if target.os_name == "windows":
        return False
    path = PurePosixPath(relative_path)
    if path in {PurePosixPath("run.sh"), PurePosixPath("run.command")}:
        return True
    if not path.parts or path.parts[0] != "codex-package":
        return False
    package_path = PurePosixPath(*path.parts[1:])
    if package_path.parts and package_path.parts[0] in {"bin", "codex-path"}:
        return True
    if package_path == PurePosixPath("codex-resources/bwrap"):
        return True
    return (
        len(package_path.parts) >= 3
        and package_path.parts[:3] == ("codex-resources", "voice", "bin")
    )


def add_tar_archive(stage: Path, archive_path: Path, bundle_name: str, target: Target) -> None:
    def archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
        prefix = f"{bundle_name}/"
        relative = member.name.removeprefix(prefix)
        if member.isfile() and should_have_execute_bit(relative, target):
            member.mode = 0o755
        return member

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(stage, arcname=bundle_name, filter=archive_filter)


def make_bundle(
    target: Target,
    release: dict[str, Any],
    asset: dict[str, Any],
    binary: Path,
    runtime_root: Path | None,
    output_root: Path,
    repository_root: Path,
    repository: str,
) -> Path:
    tag = str(release.get("tag_name") or "local")
    safe_tag = "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in tag
    )
    bundle_name = f"codex-portable-{safe_tag}-{target.name}"
    output_root.mkdir(parents=True, exist_ok=True)
    archive_path = output_root / f"{bundle_name}{target.archive_suffix}"

    with tempfile.TemporaryDirectory(prefix="codex-portable-") as temporary:
        stage = Path(temporary) / bundle_name
        package_stage = stage / "codex-package"
        (stage / "data" / "codex").mkdir(parents=True)
        package_stage.mkdir(parents=True)

        if runtime_root is None:
            destination_binary = package_stage / "bin" / target.binary_name
            destination_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, destination_binary)
            write_text(
                stage / "data" / "codex" / "config.toml",
                "[features]\ndaemon_auto_start = false\n",
            )
        else:
            _copy_runtime_tree(runtime_root, package_stage)
            validate_package_root(package_stage, target)
            destination_binary = _find_binary(package_stage, target.binary_name)

        if target.os_name != "windows":
            destination_binary.chmod(
                destination_binary.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )

        if target.os_name == "windows":
            write_text(stage / "run.bat", WINDOWS_LAUNCHER)
            write_text(stage / "run.ps1", POWERSHELL_LAUNCHER)
        else:
            write_text(stage / "run.sh", UNIX_LAUNCHER, executable=True)
            if target.os_name == "macos":
                write_text(stage / "run.command", MACOS_LAUNCHER, executable=True)

        binary_digest = sha256(destination_binary)
        package_layout = (
            "official-codex-package" if runtime_root is not None else "entrypoint-only"
        )
        manifest = {
            "format_version": 2,
            "upstream_repository": repository,
            "release_tag": release.get("tag_name"),
            "release_url": release.get("html_url"),
            "target": target.name,
            "upstream_target": target.upstream_target,
            "package_root": "codex-package",
            "package_layout": package_layout,
            "asset": asset.get("name"),
            "asset_url": asset.get("browser_download_url"),
            "asset_digest": asset.get("digest"),
            "binary_sha256": binary_digest,
            "portable_home": "data/codex",
            "sqlite_home": "data/codex/state",
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        write_text(stage / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        write_text(
            stage / "README.md",
            bundle_readme(release, target, asset, binary_digest, repository, package_layout),
        )
        copy_legal_files(repository_root, stage)

        if target.archive_suffix == ".zip":
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(stage.parent))
        else:
            add_tar_archive(stage, archive_path, bundle_name, target)

    checksum_path = output_root / f"{archive_path.name}.sha256"
    write_text(checksum_path, f"{sha256(archive_path)}  {archive_path.name}\n")
    return archive_path




def build(args: argparse.Namespace) -> Path:
    target = select_target(args.target)
    repository_root = Path(args.repository_root).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    local_inputs = [args.binary, args.source_dir, args.package_dir]

    if sum(value is not None for value in local_inputs) > 1:
        raise ValueError("Use only one of --binary, --source-dir, and --package-dir")

    if args.package_dir:
        runtime_root = Path(args.package_dir).expanduser().resolve()
        if not runtime_root.is_dir():
            raise FileNotFoundError(runtime_root)
        validate_package_root(runtime_root, target)
        binary = _find_binary(runtime_root, target.binary_name)
        release = {
            "tag_name": args.release_tag or "local",
            "html_url": None,
            "assets": [],
        }
        asset = {"name": runtime_root.name, "browser_download_url": None}
        return make_bundle(
            target,
            release,
            asset,
            binary,
            runtime_root,
            output_root,
            repository_root,
            args.upstream_repository,
        )

    if args.binary or args.source_dir:
        binary = resolve_local_binary(Path(args.binary or args.source_dir), target)
        release = {
            "tag_name": args.release_tag or "local",
            "html_url": None,
            "assets": [],
        }
        asset = {"name": binary.name, "browser_download_url": None}
        return make_bundle(
            target,
            release,
            asset,
            binary,
            None,
            output_root,
            repository_root,
            args.upstream_repository,
        )

    release = github_json(release_url(args.upstream_repository, args.release_tag))
    asset = choose_asset(release, target)
    with tempfile.TemporaryDirectory(prefix="codex-portable-download-") as temporary:
        downloaded = Path(temporary) / asset["name"]
        print(f"Downloading {asset['name']} from {release['tag_name']}...", file=sys.stderr)
        download(asset["browser_download_url"], downloaded)
        expected_digest = asset.get("digest")
        if isinstance(expected_digest, str) and expected_digest.startswith("sha256:"):
            actual_digest = sha256(downloaded)
            if actual_digest != expected_digest.removeprefix("sha256:"):
                raise RuntimeError(
                    f"Checksum mismatch for {asset['name']}: expected {expected_digest}, "
                    f"got sha256:{actual_digest}"
                )
        with tempfile.TemporaryDirectory(prefix="codex-portable-extract-") as extracted:
            extracted_root = Path(extracted)
            if asset["name"].endswith(".tar.gz"):
                _extract_tar(downloaded, extracted_root)
            elif asset["name"].endswith(".zip"):
                _extract_zip(downloaded, extracted_root)
            else:
                shutil.copy2(downloaded, extracted_root / target.binary_name)
            binary = _find_binary(extracted_root, target.binary_name)
            runtime_root = _find_package_root(extracted_root, target.binary_name)
            if asset["name"].startswith("codex-package-") and runtime_root is None:
                raise RuntimeError(
                    f"Official package asset {asset['name']} did not contain the expected package layout"
                )
            if runtime_root is not None:
                validate_package_root(runtime_root, target)
            return make_bundle(
                target,
                release,
                asset,
                binary,
                runtime_root,
                output_root,
                repository_root,
                args.upstream_repository,
            )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--target", help="Target platform, or omit to use the host platform")
    command.add_argument("--upstream-repository", default=UPSTREAM_REPOSITORY)
    command.add_argument("--release-tag", help="Exact upstream release tag; defaults to latest")
    command.add_argument("--binary", help="Path to a locally built codex/codex.exe")
    command.add_argument("--source-dir", help="Upstream checkout or build directory containing codex")
    command.add_argument(
        "--package-dir",
        help="Existing official codex-package directory to wrap without rebuilding it",
    )
    command.add_argument("--output", default="dist", help="Directory for the generated archive")
    command.add_argument("--repository-root", default=".", help="Checkout root used to copy LICENSE and NOTICE")
    return command


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if sys.version_info < (3, 10):
        print("codex portable maker requires Python 3.10 or newer", file=sys.stderr)
        return 2
    try:
        result = build(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"codex portable maker: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
