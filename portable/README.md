# Codex portable maker

This directory is an additive packaging layer for the official `openai/codex` fork. It deliberately does not patch Codex source files, add a second runtime, or modify upstream build targets. When the fork is updated from upstream, the portable layer remains a small, isolated directory that can be reviewed independently.

## Build a bundle

The maker uses the latest official `codex-package-*` release asset for the selected target. These archives contain the upstream entrypoint together with the code-mode host, ripgrep, and platform sandbox helpers:

```bash
python3 portable/make_portable.py --target linux-x86_64 --output dist
python3 portable/make_portable.py --target macos-aarch64 --output dist
python3 portable/make_portable.py --target windows-x86_64 --output dist
```

Use `--release-tag rust-v0.164.0` to pin an exact upstream release. To wrap an already assembled official package directory without downloading anything:

```bash
just assemble-codex-package --target x86_64-unknown-linux-musl --cargo-profile release \
  --package-dir /tmp/codex-package
python3 portable/make_portable.py \
  --target linux-x86_64 \
  --package-dir /tmp/codex-package \
  --output dist
```

The generated archive preserves the official package layout, then adds a launcher, `manifest.json`, and the upstream license files when present. The launcher creates `data/codex/` and sets `CODEX_HOME` to it, so authentication, configuration, history, skills, and state stay with the bundle. The caller's working directory and command-line arguments are preserved.

Supported targets are:

- `linux-x86_64`
- `linux-aarch64`
- `macos-x86_64`
- `macos-aarch64`
- `windows-x86_64`
- `windows-aarch64`

The official release assets are preferred over source compilation because they follow the upstream release process and avoid adding portable-specific changes to the Codex build. The workflow in `.github/workflows/portable.yml` builds all six bundles from one pinned upstream release and publishes them as workflow artifacts. `--binary` remains available for a small entrypoint-only smoke bundle; use `--package-dir` for a functional local package with all upstream helpers.

## Keeping the fork current

The fork currently tracks `openai/codex`. Keep the upstream update separate from portable changes:

```bash
git remote add upstream https://github.com/openai/codex.git  # once
git fetch upstream main
git checkout main
git merge --ff-only upstream/main
```

Portable behavior is owned only by `portable/` and the additive workflow. This makes an upstream fast-forward easy to review and keeps conflicts out of Codex source files.

## Scope and limitations

The bundle removes the Node.js/Rust requirement for users because it carries a compiled Codex binary. It still relies on the operating-system capabilities required by that upstream binary, including a usable terminal and any platform sandbox support. No device binding, system-directory symlink, or host configuration migration is performed.
