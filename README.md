<p align="center">
  <img src="docs/assets/wco-logo.png" alt="WCO logo">
</p>

<h1 align="center">wco</h1>

<p align="center">
  <strong>Run a central Docker Compose stack against whichever Git worktree you are standing in.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20WSL2-lightgrey" alt="Platforms">
  <img src="https://img.shields.io/badge/compose-v2-2496ED" alt="Docker Compose v2">
</p>

---

`wco` runs one central Docker Compose configuration against whichever Git worktree contains the
current directory. It is project-agnostic: every Docker workspace defines its own Compose files,
optional WCO-rendered variables, validation rules, and isolated port set in `.wco.toml`.

```bash
cd ~/code/myapp-feature-branch   # any worktree, any subdirectory
wco up -d --build                # the central stack, pointed here
```

**Contents**

- [Requirements](#requirements)
- [Install](#install)
- [Initialize a workspace](#initialize-a-workspace)
- [Everyday use](#everyday-use)
- [Isolated mode](#isolated-mode)
- [Configuration reference](#configuration-reference)
  - [Fixed ports in isolated mode](#fixed-ports-in-isolated-mode)
  - [Environment templates](#environment-templates)
  - [Container names in isolated mode](#container-names-in-isolated-mode)
  - [Port assignments and state](#port-assignments-and-state)

## Requirements

| Requirement | Notes |
| --- | --- |
| Python 3.11+ | |
| Git | Worktree detection |
| Docker + Compose v2 | Use Docker Desktop on macOS and Windows |
| Platforms | Linux, macOS, native Windows (PowerShell or Command Prompt), and WSL2 |

> [!TIP]
> When running `wco` inside WSL2, enable Docker Desktop's WSL integration.

## Install

Install a self-contained copy with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install /path/to/wco-source
```

While developing `wco`, install it in editable mode instead:

```bash
uv tool install --editable /path/to/wco-source
```

> [!NOTE]
> Run `uv tool update-shell` if the installed command is not on `PATH`. Reinstall a non-editable
> installation after updating the source package.

## Initialize a workspace

Run `init` in the directory that contains the central Compose file:

```bash
wco init
```

`wco` detects `compose.yaml`, `compose.yml`, `docker-compose.yaml`, or `docker-compose.yml`,
derives the project name from the directory, and creates `.wco.toml`. Override those defaults
when needed:

```bash
wco init --compose deploy/compose.yml --project example
wco init --force
```

Things worth knowing:

- Initialization never changes the Compose file.
- Relative Compose paths such as `./` resolve from the **active worktree**, and Docker Compose
  loads `.env` from that worktree for interpolation.
- Declare parameterized published ports under `[isolation.ports]`.
- Existing `.wco.toml` files are not replaced unless `--force` is supplied.

## Everyday use

From a worktree root or any directory below it:

```bash
wco up -d --build --force-recreate
wco ps
wco logs -f php
wco down
```

**Output.** Interactive commands use colored, terminal-aware output. `wco ps` presents a
responsive `NAME`, `STATUS`, `WORKTREE`, and `BRANCH` table with status highlighting, and
supports Compose filters, service arguments, `--all`, `--status`, `--orphans`, and `--no-trunc`.
When output is redirected — or when `--format`, `--quiet`, or `--services` is supplied — WCO
delegates the output to Docker Compose unchanged. Set [`NO_COLOR`](https://no-color.org/) to
disable color styling.

**Switching worktrees.** The default mode reuses the configured Compose project. Switching
worktrees changes the rendered bind-mount source, so `up` recreates affected services when Compose
detects the configuration change. Use `--force-recreate` when an unconditional switch is preferred.

**Every stack at once.** `wco ps` is scoped to one project — the shared one, or with `--isolated`
this worktree's. To see every container the workspace owns, across all worktrees:

```bash
wco stacks             # running containers, shared and isolated
wco stacks --all       # include stopped containers
wco stacks --format json
```

`wco stacks` scans Docker for Compose containers and keeps the ones whose project is this
workspace's shared project or the isolated project derived from their recorded worktree, so
stacks belonging to other projects are never listed.

**Where containers run.** The `WORKTREE` column reports the worktree each listed container was
actually created from — not the one you are standing in — and is highlighted when the two differ.
`BRANCH` shows that worktree's checked-out branch, or the short revision in parentheses when the
worktree is detached. Both modes use the same columns, so `ps` omits the `Worktree` row from the
`WCO context` block.

## Isolated mode

Run worktrees concurrently with isolated project names and ports:

```bash
wco --isolated up -d --build
wco --isolated ps
wco --isolated down
```

> [!IMPORTANT]
> `--isolated` must be included on **every** command that targets the isolated stack.

To stop every worktree's isolated stack at once, from any worktree of the workspace:

```bash
wco --isolated down --all
wco --isolated down --all --volumes    # extra flags are forwarded to each down
```

Targets are the union of the worktrees holding a port slot and those with isolated containers
(running or stopped), so a stack still gets torn down after its slot was reallocated. Each worktree
runs its own `docker compose down` with that worktree's project name and environment; a failure in
one does not stop the others, and the first non-zero exit status is returned. Worktrees that no
longer exist on disk are reported rather than skipped silently — Compose needs the worktree to
resolve its files, so remove those with `docker compose -p <project> down`.

Inspect or replace the current worktree's persistent port assignment:

```bash
wco ports show
wco ports reallocate
```

`wco ports show --all` widens that to every worktree that holds a slot for this workspace, adding
`WORKTREE` and `BRANCH` columns. It reads the recorded state only — no Docker call — so it also
lists slots reserved for worktrees whose stacks are currently down, and reports `outdated` for an
assignment whose variables no longer match `[isolation.ports]` (run `wco ports reallocate` in that
worktree). Assignments whose worktree directory no longer exists are pruned from the state file on
access, so they never appear.

Port assignments are shown as a table by default. For automation, request stable, unstyled JSON
instead:

```bash
wco ports show --format json
wco ports reallocate --format=json
```

> [!WARNING]
> Run `wco --isolated down` before reallocating ports.

## Configuration reference

Place `.wco.toml` beside the central Compose file. The configuration may also live in a normal
repository root; `wco` searches from the detected Git worktree toward the filesystem root and uses
the nearest configuration.

```toml
version = 1

[compose]
files = ["docker-compose.yml"]
project_name = "example"
instance_name = "example"

[validation]
required = ["package.json"]
startup_required = [".env"]
startup_commands = ["up", "create", "start", "restart", "run", "watch"]

[isolation]
port_step = 100
max_slots = 500
# Opt in when the Compose files contain fixed names or published ports.
rewrite_container_names = true
rewrite_ports = true

[isolation.ports]
HTTP_PORT = 8080
DEV_PORT = 5173
REDIS_PORT = 6379
```

`instance_name` defaults to `project_name`. All validation lists are optional and may be empty.

Compose can use worktree-relative paths and the configured port variables:

```yaml
services:
  web:
    volumes:
      - "./:/app"
    ports:
      - "${HTTP_PORT:-8080}:80"
      - "${DEV_PORT:-5173}:5173"
```

Docker Compose automatically loads `.env` from the active worktree because `wco` uses that worktree
as the Compose project directory. These values are available for Compose interpolation; pass them
into a container with the Compose file's `environment` or `env_file` service attributes when needed.

Every published host port used by isolated mode must be listed in `[isolation.ports]`. By default,
the Compose file must reference the corresponding variable as shown above.

### Fixed ports in isolated mode

If changing the project's Compose file is not practical, WCO can rotate fixed published ports
through the generated isolation override:

```toml
[isolation]
rewrite_ports = true

[isolation.ports]
VITE_PORT = 5173
REDIS_PORT = 6379
HTTP_PORT = 8084
```

WCO matches each rendered host port to the base value in `[isolation.ports]`, replaces it with the
current worktree's allocated port, and preserves its container port, protocol, host IP, and mode.
For example, slot 1 with the default `port_step = 100` rotates `5173`, `6379`, and `8084` to `5273`,
`6479`, and `8184`. Fixed ports not listed in `[isolation.ports]` remain an error.

Port-list replacement uses Docker Compose's
[`!override` merge tag](https://docs.docker.com/reference/compose-file/merge/#replace-value) and
therefore requires Docker Compose 2.24.4 or newer. The original Compose files are never changed.

### Environment templates

Use the optional `[environment]` table only for values that `wco` must render from its current
context:

```toml
[environment]
SOURCE_PATH = "{worktree}"
CONTAINER_PREFIX = "{instance}"
```

| Template | Renders to |
| --- | --- |
| `{worktree}` | The active Git worktree |
| `{workspace}` | The Docker workspace directory |
| `{project}` | The configured `project_name` |
| `{instance}` | The configured `instance_name` (isolated-aware) |

Configured values replace same-named host variables before Compose starts and therefore take
precedence over `.env`.

### Container names in isolated mode

By default, explicit `container_name` values must contain an environment value rendered from
`{instance}`; alternatively, omit `container_name` and let Compose generate project-scoped names.
If changing the project's Compose file is not practical, opt in to generated names instead:

```toml
[isolation]
rewrite_container_names = true
```

WCO then renders the effective configuration, including profile-only services, and writes a small
generated override that replaces each fixed name with `<isolated-instance>-<original-name>`. The
override is passed as the final Compose file for every isolated command. The project's Compose
files are never copied or modified. Long or duplicate results receive a stable hash suffix.

Generated overrides are stored below the WCO state directory in `overrides/`. They are recreated as
needed and remain available for later commands such as `wco --isolated down`.

> [!WARNING]
> This mode cannot be combined with a Compose file read from standard input (`--file -`). Services
> or tools that refer to an exact container name — such as a Promtail target configured with a
> hard-coded name — must use the generated name or another stable discovery mechanism.

### Port assignments and state

The first isolated worktree receives each base port plus one `port_step`, the next receives the
lowest available slot, and assignments remain stable in `ports.json`:

| Platform | State file |
| --- | --- |
| Linux, macOS, WSL2 | `~/.local/state/wco/ports.json` (or `$XDG_STATE_HOME/wco/ports.json`) |
| Native Windows | `%LOCALAPPDATA%\wco\ports.json` |

Generated isolation overrides for container names and fixed ports use the adjacent `overrides/`
directory.

> [!NOTE]
> `wco` controls Docker Compose's project name, so `-p` and `--project-name` are intentionally
> rejected. Other Docker Compose arguments are forwarded unchanged.
