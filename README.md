# wco

`wco` runs a central Docker Compose configuration against whichever Git
worktree contains the current directory. It is project-agnostic: every Docker
workspace defines its own Compose files, environment variables, validation
rules, and isolated port set in `.wco.toml`.

## Supported platforms

`wco` supports Linux, macOS, native Windows through PowerShell or Command
Prompt, and Windows through WSL2. Python 3.11 or newer, Git, Docker, and Docker
Compose v2 are required. Use Docker Desktop on macOS and Windows; enable its
WSL integration when running `wco` inside WSL2.

## Install

Install a self-contained copy with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install /path/to/wco-source
```

While developing `wco`, install it in editable mode instead:

```bash
uv tool install --editable /path/to/wco-source
```

Run `uv tool update-shell` if the installed command is not on `PATH`. Reinstall
a non-editable installation after updating the source package.

## Initialize a workspace

Run `init` in the directory that contains the central Compose file:

```bash
wco init
```

`wco` detects `compose.yaml`, `compose.yml`, `docker-compose.yaml`, or
`docker-compose.yml`, derives the project name from the directory, and creates
`.wco.toml`. Override those defaults when needed:

```bash
wco init --compose deploy/compose.yml --project example
wco init --force
```

Initialization never changes the Compose file. Afterward, update its worktree
bind mounts to use `${SOURCE_PATH}` and declare parameterized published ports
under `[isolation.ports]`. Existing `.wco.toml` files are not replaced unless
`--force` is supplied.

## Use

From a worktree root or any directory below it:

```bash
wco up -d --build --force-recreate
wco ps
wco logs -f php
wco down
```

The default mode reuses the configured Compose project. Switching worktrees
changes the rendered bind-mount source, so `up` recreates affected services
when Compose detects the configuration change. `--force-recreate` can be used
when an unconditional switch is preferred.

Run worktrees concurrently with isolated project names and ports:

```bash
wco --isolated up -d --build
wco --isolated ps
wco --isolated down
```

`--isolated` must be included on every command that targets the isolated
stack. Inspect or replace the current worktree's persistent port assignment:

```bash
wco ports show
wco ports reallocate
```

Run `wco --isolated down` before reallocating ports.

## Configure a workspace

Place `.wco.toml` beside the central Compose file. The configuration may
also live in a normal repository root; `wco` searches from the detected
Git worktree toward the filesystem root and uses the nearest configuration.

```toml
version = 1

[compose]
files = ["docker-compose.yml"]
project_name = "example"
instance_name = "example"

[environment]
SOURCE_PATH = "{worktree}"
CONTAINER_PREFIX = "{instance}"

[validation]
required = ["package.json"]
startup_required = [".env"]
startup_commands = ["up", "create", "start", "restart", "run", "watch"]

[isolation]
port_step = 100
max_slots = 500

[isolation.ports]
HTTP_PORT = 8080
DEV_PORT = 5173
```

Compose then consumes those variables:

```yaml
services:
  web:
    container_name: "${CONTAINER_PREFIX:-example}-web"
    volumes:
      - "${SOURCE_PATH:?Run through wco}:/app"
    ports:
      - "${HTTP_PORT:-8080}:80"
      - "${DEV_PORT:-5173}:5173"
```

Supported environment templates are `{worktree}`, `{workspace}`, `{project}`
and `{instance}`. `instance_name` defaults to `project_name`. All validation
lists are optional and may be empty.

For isolated mode, every fixed published host port must be replaced with an
environment variable and listed in `[isolation.ports]`. Explicit
`container_name` values must contain an environment value rendered from
`{instance}`; alternatively, omit `container_name` and let Compose generate
project-scoped names.

The first isolated worktree receives each base port plus one `port_step`, the
next receives the lowest available slot, and assignments remain stable under
`$XDG_STATE_HOME/wco/ports.json` when configured. The defaults are
`~/.local/state/wco/ports.json` on Linux, macOS, and WSL2, and
`%LOCALAPPDATA%\wco\ports.json` on native Windows.

`wco` controls Docker Compose's project name, so `-p` and
`--project-name` are intentionally rejected. Other Docker Compose arguments
are forwarded unchanged.
