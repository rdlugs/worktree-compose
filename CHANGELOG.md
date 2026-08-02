# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Native support for macOS and Windows, including Windows workflows through
  WSL2, with CI coverage on macOS and native Windows.

### Changed

- Replaced Unix-only port-state locking with cross-platform native file locks.
- Store native Windows state under `%LOCALAPPDATA%` by default.

## [1.0.0-rc.1] - 2026-08-02

### Added

- First public release candidate.
- Worktree-aware Docker Compose execution from any directory in a Git worktree.
- Shared and isolated Compose project modes with stable project and container names.
- Persistent isolated-port allocation, inspection, conflict handling, and reallocation.
- `wco init` workspace initialization with Compose detection and safe overwrite behavior.
- Configurable Compose files, environment templates, and worktree validation.
- GitHub Actions workflows for the Python test matrix, package builds, and tag-based releases.
- MIT license, contribution guidelines, and a security reporting policy.

### Changed

- Organized the reusable CLI as a standalone `src`-layout Python project.

## [0.3.0] - 2026-08-02

### Added

- `wco init` for generating a workspace configuration from a detected Compose file.
- `--compose`, `--project`, and overwrite-protected `--force` initialization options.

### Changed

- Organized the reusable CLI as a standalone `src`-layout Python project.

## [0.2.0] - 2026-08-02

### Changed

- Renamed the command, package, configuration file, and state directory to `wco`.
- Added automatic migration of legacy isolated-port state.

## [0.1.0] - 2026-08-02

### Added

- Worktree-aware Docker Compose invocation from any nested worktree directory.
- Shared and isolated Compose project modes.
- Stable isolated project names and persistent host-port allocation.
- Configurable Compose files, environment templates, and worktree validation.
- Port inspection and explicit reallocation commands.

[Unreleased]: https://github.com/rdlugs/worktree-compose/compare/v1.0.0-rc.1...HEAD
[1.0.0-rc.1]: https://github.com/rdlugs/worktree-compose/releases/tag/v1.0.0-rc.1
[0.3.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.3.0
[0.2.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.2.0
[0.1.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.1.0
