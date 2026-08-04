# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.0] - 2026-08-04

### Added

- Make the command optional in `wco exec`. `wco exec 2.1` — or `wco exec php` —
  now opens an interactive shell instead of failing with a Compose usage error,
  picking the first shell that exists inside the container. The candidates
  default to `bash` then `sh` and are configurable per workspace with a new
  `[shell]` table in `.wco.toml`. Passing a command keeps the previous
  behaviour.

### Fixed

- Splice the service resolved from a container ID in after `exec`'s own options
  rather than directly after the command. Docker Compose stops reading options
  at the first positional argument, so `wco exec 2.1 -u root whoami` previously
  produced `exec php -u root whoami` and failed with `"-u": executable file not
  found in $PATH`. Other commands are unchanged.

## [1.3.0] - 2026-08-03

### Added

- Add persistent integer stack IDs and `<stack>.<container>` container IDs, shown
  in a new `ID` column in `wco ps` and `wco stacks` and in their JSON output. Stack
  1 is always the shared project; isolated worktrees are numbered from 2 and keep
  their ID, recorded in `ports.json`, until the worktree is removed.
- Accept an ID directly after a Docker Compose command to target another stack, or
  a single container inside it, without changing directory — `wco down 2`,
  `wco --isolated down 2`, `wco restart 2.1`, `wco logs 2.1 -f`, `wco exec 2.1 sh`.
  A container ID resolves to its Compose service name; `down` acts on whole stacks
  and rejects one.

### Changed

- Bump the `ports.json` state format to version 2, adding recorded stack IDs.
  Version 1 files are read and upgraded in place on the next write.

### Fixed

- Resolve missing worktree-relative build contexts from the central WCO workspace when the same
  path exists there, while preserving active-worktree resolution for bind mounts and build
  contexts that are present in the checkout.

## [1.2.1] - 2026-08-03

### Changed

- Publish tagged release distributions to PyPI using GitHub Actions trusted
  publishing.
- Document installation of the published package with `uv`.

## [1.2.0] - 2026-08-03

### Added

- Add terminal-aware, colored output for WCO context, initialization, port
  assignments, and responsive interactive `wco ps` output, including container
  state glyphs, a container state summary, grouped port assignments, and
  highlighted help. Output degrades to ASCII glyphs on terminals that cannot
  encode Unicode and drops decoration entirely when the stream is redirected.
- Add JSON output for `wco ports show` and `wco ports reallocate` through
  `--format json`.
- Add opt-in isolated container-name rewriting through generated WCO state
  overrides, allowing unchanged Compose files with fixed `container_name`
  values to run concurrently across worktrees.
- Add opt-in rotation of fixed published host ports through generated Compose
  overrides while preserving container ports, protocols, host IPs, and modes.
- Add `wco --isolated down --all`, tearing down every worktree's isolated stack
  in one command. Targets combine recorded port slots with isolated containers
  found in Docker, and remaining flags are forwarded to each `down`.
- Add `wco ports show --all`, listing every worktree that holds a port slot for
  the workspace with its branch, and flagging assignments whose variables no
  longer match `[isolation.ports]` as `outdated`.
- Add `wco stacks`, listing every container the workspace owns — the shared
  project and each worktree's isolated project — with its mode, worktree, and
  branch. Supports `--all` for stopped containers and `--format json`.
- Report the worktree that each listed container was created from in `wco ps`,
  reading the Compose `working_dir` label, together with that worktree's checked
  out branch. Paths are highlighted when they differ from the active worktree.

### Changed

- Generate leaner workspace configurations without a default `[environment]`
  table, and clarify that Docker Compose manages each worktree's `.env` file.
- Preserve native Docker Compose `ps` output for redirects and explicit output
  options while enriching the default interactive table with container details.
- Reduce the interactive `wco ps` table to `NAME`, `STATUS`, `WORKTREE`, and
  `BRANCH`, dropping the `IMAGE`, `SERVICE`, and `PORTS` columns.

## [1.1.1] - 2026-08-02

### Fixed

- Resolve relative Docker Compose paths from the active Git worktree instead
  of the directory containing the central Compose configuration.

## [1.1.0] - 2026-08-02

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

[Unreleased]: https://github.com/rdlugs/worktree-compose/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/rdlugs/worktree-compose/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/rdlugs/worktree-compose/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/rdlugs/worktree-compose/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/rdlugs/worktree-compose/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/rdlugs/worktree-compose/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/rdlugs/worktree-compose/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/rdlugs/worktree-compose/compare/v1.0.0-rc.1...v1.0.0
[1.0.0-rc.1]: https://github.com/rdlugs/worktree-compose/releases/tag/v1.0.0-rc.1
[0.3.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.3.0
[0.2.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.2.0
[0.1.0]: https://github.com/rdlugs/worktree-compose/releases/tag/v0.1.0
