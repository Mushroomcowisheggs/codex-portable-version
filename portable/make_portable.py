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
from typing import Any, Iterable

UPSTREAM_REPOSITORY = os.environ.get("CODEX_PORTABLE_UPSTREAM", "openai/codex")
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class Target:
    name: str
    os_name: str
    architecture: str
    asset_names: tuple[str, ...]
    binary_name: str
    archive_suffix: str


TARGETS: dict[str, Target] = {
    "linux-x86_64": Target(
        "linux-x86_64",
        "linux",
        "x86_64",
        ("codex-package-x86_64-unknown-linux-musl.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "linux-aarch64": Target(
        "linux-aarch64",
        "linux",
        "aarch64",
        ("codex-package-aarch64-unknown-linux-musl.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "macos-x86_64": Target(
        "macos-x86_64",
        "macos",
        "x86_64",
        ("codex-package-x86_64-apple-darwin.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "macos-aarch64": Target(
        "macos-aarch64",
        "macos",
        "aarch64",
        ("codex-package-aarch64-apple-darwin.tar.gz",),
        "codex",
        ".tar.gz",
    ),
    "windows-x86_64": Target(
        "windows-x86_64",
        "windows",
        "x86_64",
        ("codex-package-x86_64-pc-windows-msvc.tar.gz",),
        "codex.exe",
        ".zip",
    ),
    "windows-aarch64": Target(
        "windows-aarch64",
        "windows",
        "aarch64",
        ("codex-package-aarch64-pc-windows-msvc.tar.gz",),
        "codex.exe",
        ".zip",
    ),
}

UNIX_LAUNCHER = """#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CODEX_HOME="$ROOT/data/codex"
mkdir -p "$CODEX_HOME"
export CODEX_HOME

exec "$ROOT/bin/codex" "$@"
"""

MACOS_LAUNCHER = """#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$ROOT/run.sh" "$@"
"""

WINDOWS_LAUNCHER = r'''@echo off
setlocal
set "ROOT=%~dp0"
set "CODEX_HOME=%ROOT%data\codex"
if not exist "%CODEX_HOME%" mkdir "%CODEX_HOME%"
"%ROOT%bin\codex.exe" %*
exit /b %ERRORLEVEL%
'''

POWERSHELL_LAUNCHER = r'''$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:CODEX_HOME = Join-Path $root "data\codex"
New-Item -ItemType Directory -Force -Path $env:CODEX_HOME | Out-Null
& (Join-Path $root "bin\codex.exe") @args
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
    with urllib.request.urlopen(request) as response:
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
    with urllib.request.urlopen(request) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


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
        "The official package layout is preserved, including the code-mode host, "
        "platform sandbox helpers, and bundled ripgrep."
        if package_layout == "official-codex-package"
        else "This local bundle contains only the supplied entrypoint and is intended for maker smoke tests."
    )
    return f"""# Codex Portable\n\nThis bundle contains the official Codex CLI binary for **{target.name}**.\n\n- Upstream: https://github.com/{repository}\n- Release: `{release.get('tag_name', 'local')}`\n- Source asset: `{asset.get('name', 'local binary')}`\n- Binary SHA-256: `{binary_digest}`\n\n## Run\n\nUse `run.sh` on Linux/macOS, `run.command` on macOS, `run.bat` on Windows, or\n`run.ps1` from PowerShell. The launcher sets `CODEX_HOME` to `data/codex`, so\nlogin data, configuration, history, and the local state database stay beside the\nbinary instead of being written to the host user's home directory.\n\nThe current working directory is preserved; launch the bundle from the project\nyou want Codex to work on. Arguments are passed through unchanged.\n\n{package_note} This is a packaging layer around upstream Codex; it does not patch\nor replace Codex source code. OS capabilities required by upstream Codex (for\nexample a usable terminal and any platform sandbox support) still apply.\n"""


def copy_legal_files(repository_root: Path, destination: Path) -> None:
    for name in ("LICENSE", "NOTICE"):
        source = repository_root / name
        if source.is_file():
            shutil.copy2(source, destination / name)


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
    safe_tag = "".join(character if character.isalnum() or character in ".-_" else "-" for character in tag)
    bundle_name = f"codex-portable-{safe_tag}-{target.name}"
    output_root.mkdir(parents=True, exist_ok=True)
    archive_path = output_root / f"{bundle_name}{target.archive_suffix}"

    with tempfile.TemporaryDirectory(prefix="codex-portable-") as temporary:
        stage = Path(temporary) / bundle_name
        (stage / "data" / "codex").mkdir(parents=True)
        if runtime_root is None:
            destination_binary = stage / "bin" / target.binary_name
            destination_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, destination_binary)
        else:
            _copy_runtime_tree(runtime_root, stage)
            destination_binary = _find_binary(stage, target.binary_name)
        if target.os_name != "windows":
            destination_binary.chmod(destination_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if target.os_name == "windows":
            write_text(stage / "run.bat", WINDOWS_LAUNCHER)
            write_text(stage / "run.ps1", POWERSHELL_LAUNCHER)
        else:
            write_text(stage / "run.sh", UNIX_LAUNCHER, executable=True)
            if target.os_name == "macos":
                write_text(stage / "run.command", MACOS_LAUNCHER, executable=True)

        binary_digest = sha256(destination_binary)
        manifest = {
            "format_version": 1,
            "upstream_repository": repository,
            "release_tag": release.get("tag_name"),
            "release_url": release.get("html_url"),
            "target": target.name,
            "package_layout": "official-codex-package" if runtime_root is not None else "entrypoint-only",
            "asset": asset.get("name"),
            "asset_url": asset.get("browser_download_url"),
            "asset_digest": asset.get("digest"),
            "binary_sha256": binary_digest,
            "portable_home": "data/codex",
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        write_text(stage / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        package_layout = "official-codex-package" if runtime_root is not None else "entrypoint-only"
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
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(stage, arcname=bundle_name)

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
    try:
        result = build(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"codex portable maker: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
