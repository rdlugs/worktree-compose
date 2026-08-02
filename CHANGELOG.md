# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- GitHub Actions workflows for the Python test matrix, package builds, and tag-based releases.
- MIT license, contribution guidelines, and a security reporting policy.

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

[Unreleased]: https://github.com/rdlugs/worktree-compose/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.3.0
[0.2.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.2.0
[0.1.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.1.0
