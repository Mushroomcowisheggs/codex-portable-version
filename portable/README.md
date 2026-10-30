# Codex portable maker

This directory is an additive packaging layer for the official 'openai/codex' fork. It does not patch Codex source files or replace upstream build targets. When the fork is updated from upstream, this layer can be reviewed independently.

## Build a bundle

The maker uses the latest official 'codex-package-*' release asset for the selected target. The generated bundle keeps the executable package and portable state as siblings:

~~~text
codex-portable-<release>-<target>/
  codex-package/       # upstream bin/, codex-resources/, codex-path/, metadata
  data/codex/          # CODEX_HOME and SQLite state
  run.sh / run.command / run.bat / run.ps1
  manifest.json
~~~

This layout is required by upstream's daemon installer: CODEX_HOME must be outside the source CLI package.

~~~bash
python3 portable/make_portable.py --target linux-x86_64 --output dist
python3 portable/make_portable.py --target macos-aarch64 --output dist
python3 portable/make_portable.py --target windows-x86_64 --output dist
~~~

Use --release-tag rust-v0.164.0 to pin an exact upstream release. The maker verifies the release asset digest when GitHub provides one and validates the official package metadata and required helper files before packaging.

To wrap an already assembled official package directory without downloading anything:

~~~bash
just assemble-codex-package --target x86_64-unknown-linux-musl --cargo-profile release \
  --package-dir /tmp/codex-package
python3 portable/make_portable.py \
  --target linux-x86_64 \
  --package-dir /tmp/codex-package \
  --output dist
~~~

--package-dir expects the canonical package layout containing codex-package.json, bin/, codex-resources/, and codex-path/. A WinGet-style root executable is not accepted by this mode; use --binary for an entrypoint-only smoke bundle.

The launcher sets CODEX_HOME to data/codex and CODEX_SQLITE_HOME to data/codex/state. It preserves the caller's working directory and command-line arguments. Unix launchers resolve symlinks before locating the bundle, so a run.sh or run.command link in PATH remains usable.

Supported targets are:

- linux-x86_64
- linux-aarch64
- macos-x86_64
- macos-aarch64
- windows-x86_64
- windows-aarch64

The maker requires Python 3.10 or newer. --binary and --source-dir create entrypoint-only smoke bundles; these include daemon_auto_start = false because they do not contain the complete official package required by the daemon installer. Use --package-dir for a functional local package with all upstream helpers.

## Keeping the fork current

The fork currently tracks openai/codex. Keep the upstream update separate from portable changes:

~~~bash
git remote add upstream https://github.com/openai/codex.git  # once
git fetch upstream main
git checkout main
git merge --ff-only upstream/main
~~~

Portable behavior is owned only by portable/ and the additive workflows. Rebuild with a new pinned upstream release to upgrade a bundle; the bundle deliberately does not run the upstream in-place updater.

## Portability boundary

The bundle carries the compiled Codex package and its upstream helper binaries, so users do not need Node.js or Rust. Some behavior remains an operating-system integration:

- credentials may use the host OS keyring, depending on Codex configuration;
- host git, shells, user skills, plugin/runtime caches, and PowerShell remain host-provided;
- Windows sandbox provisioning is a machine-level setup and may require administrator approval;
- the Windows daemon must be started from a non-elevated terminal;
- macOS notarization and Windows Authenticode signing are not performed by this maker.

The portable promise is package and Codex state relocation. It does not claim to replace the host operating system or its security services.
