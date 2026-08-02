from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import string
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from . import __version__


CONFIG_NAME = ".wco.toml"
LEGACY_CONFIG_NAME = ".bcompose.toml"
LEGACY_STATE_DIRECTORY = "bcompose"
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STARTUP_COMMANDS = ("up", "create", "start", "restart", "run", "watch")
COMPOSE_COMMANDS = {
    "attach",
    "build",
    "commit",
    "config",
    "cp",
    "create",
    "down",
    "events",
    "exec",
    "export",
    "images",
    "kill",
    "logs",
    "ls",
    "pause",
    "port",
    "ps",
    "publish",
    "pull",
    "push",
    "restart",
    "rm",
    "run",
    "scale",
    "start",
    "stats",
    "stop",
    "top",
    "unpause",
    "up",
    "version",
    "volumes",
    "wait",
    "watch",
}
COMPOSE_OPTIONS_WITH_VALUES = {
    "--ansi",
    "--env-file",
    "-f",
    "--file",
    "--parallel",
    "--profile",
    "--progress",
    "--project-directory",
}
RESERVED_PROJECT_OPTIONS = {"-p", "--project-name"}
ALLOWED_TEMPLATE_FIELDS = {"worktree", "workspace", "project", "instance"}
COMPOSE_FILE_CANDIDATES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


class WcoError(Exception):
    """An expected command-line or configuration error."""


@dataclass(frozen=True)
class IsolationConfig:
    port_step: int
    max_slots: int
    ports: dict[str, int]


@dataclass(frozen=True)
class ValidationConfig:
    required: tuple[str, ...]
    startup_required: tuple[str, ...]
    startup_commands: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    workspace: Path
    compose_files: tuple[Path, ...]
    project_name: str
    instance_name: str
    environment: dict[str, str]
    validation: ValidationConfig
    isolation: IsolationConfig


@dataclass(frozen=True)
class Invocation:
    worktree: Path
    config: WorkspaceConfig
    isolated: bool
    compose_args: tuple[str, ...]
    compose_command: str | None
    project_name: str
    instance_name: str
    slot: int | None
    ports: dict[str, int]
    environment: dict[str, str]


def _table(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise WcoError(f"'{name}' must be a TOML table.")
    return value


def _only_keys(table: Mapping[str, object], allowed: set[str], location: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise WcoError(
            f"Unknown key(s) in {location}: {', '.join(unknown)}."
        )


def _string_list(value: object, location: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WcoError(f"'{location}' must be an array of strings.")
    return tuple(value)


def _safe_workspace_path(workspace: Path, raw: str, location: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        raise WcoError(f"'{location}' must be relative to the workspace.")
    resolved = (workspace / candidate).resolve()
    if not resolved.is_relative_to(workspace):
        raise WcoError(f"'{location}' escapes the workspace: {raw}")
    return resolved


def _validate_relative_checks(values: Sequence[str], location: str) -> None:
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise WcoError(
                f"'{location}' entries must be safe paths relative to the worktree: {value}"
            )


def _validate_template(template: str, location: str) -> None:
    try:
        parsed = string.Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in ALLOWED_TEMPLATE_FIELDS or format_spec or conversion:
                raise WcoError(
                    f"'{location}' uses an unsupported template expression: {field_name}"
                )
    except ValueError as exc:
        raise WcoError(f"Invalid template in '{location}': {exc}") from exc


def load_config(path: Path) -> WorkspaceConfig:
    path = path.resolve()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WcoError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise WcoError(f"{path} must contain a TOML table.")
    _only_keys(data, {"version", "compose", "environment", "validation", "isolation"}, "root")
    if data.get("version") != 1:
        raise WcoError(f"{path} must declare 'version = 1'.")

    compose = _table(data, "compose")
    _only_keys(compose, {"files", "project_name", "instance_name"}, "[compose]")
    files = _string_list(compose.get("files"), "compose.files")
    if not files:
        raise WcoError("'compose.files' must contain at least one Compose file.")

    project_name = compose.get("project_name")
    if not isinstance(project_name, str) or not PROJECT_NAME_RE.fullmatch(project_name):
        raise WcoError(
            "'compose.project_name' must start with a lowercase letter or digit and "
            "contain only lowercase letters, digits, '-' and '_'."
        )
    instance_name = compose.get("instance_name", project_name)
    if not isinstance(instance_name, str) or not PROJECT_NAME_RE.fullmatch(instance_name):
        raise WcoError(
            "'compose.instance_name' must use lowercase letters, digits, '-' or '_'."
        )

    workspace = path.parent
    compose_files = tuple(
        _safe_workspace_path(workspace, raw, f"compose.files entry '{raw}'") for raw in files
    )
    missing_compose = [str(item) for item in compose_files if not item.is_file()]
    if missing_compose:
        raise WcoError(f"Compose file(s) do not exist: {', '.join(missing_compose)}")

    environment_table = _table(data, "environment")
    environment: dict[str, str] = {}
    for name, value in environment_table.items():
        if not isinstance(name, str) or not ENVIRONMENT_NAME_RE.fullmatch(name):
            raise WcoError(f"Invalid environment variable name: {name!r}")
        if not isinstance(value, str):
            raise WcoError(f"Environment value '{name}' must be a string.")
        _validate_template(value, f"environment.{name}")
        environment[name] = value

    validation_table = _table(data, "validation")
    _only_keys(
        validation_table,
        {"required", "startup_required", "startup_commands"},
        "[validation]",
    )
    required = _string_list(validation_table.get("required"), "validation.required")
    startup_required = _string_list(
        validation_table.get("startup_required"), "validation.startup_required"
    )
    startup_commands = _string_list(
        validation_table.get("startup_commands"),
        "validation.startup_commands",
        STARTUP_COMMANDS,
    )
    _validate_relative_checks(required, "validation.required")
    _validate_relative_checks(startup_required, "validation.startup_required")

    isolation_table = _table(data, "isolation")
    _only_keys(isolation_table, {"port_step", "max_slots", "ports"}, "[isolation]")
    port_step = isolation_table.get("port_step", 100)
    max_slots = isolation_table.get("max_slots", 500)
    if not isinstance(port_step, int) or isinstance(port_step, bool) or port_step <= 0:
        raise WcoError("'isolation.port_step' must be a positive integer.")
    if not isinstance(max_slots, int) or isinstance(max_slots, bool) or max_slots <= 0:
        raise WcoError("'isolation.max_slots' must be a positive integer.")
    ports_table = isolation_table.get("ports", {})
    if not isinstance(ports_table, dict):
        raise WcoError("'isolation.ports' must be a TOML table.")
    ports: dict[str, int] = {}
    for name, value in ports_table.items():
        if name in environment:
            raise WcoError(
                f"'{name}' cannot appear in both [environment] and [isolation.ports]."
            )
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
            raise WcoError(f"Isolation port '{name}' must be an integer from 1 to 65535.")
        ports[name] = value

    return WorkspaceConfig(
        path=path,
        workspace=workspace,
        compose_files=compose_files,
        project_name=project_name,
        instance_name=instance_name,
        environment=environment,
        validation=ValidationConfig(required, startup_required, startup_commands),
        isolation=IsolationConfig(port_step, max_slots, ports),
    )


def resolve_worktree(cwd: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WcoError(f"Cannot run Git: {exc}") from exc
    if result.returncode != 0:
        raise WcoError("The current directory is not inside a Git worktree.")
    return Path(result.stdout.strip()).resolve()


def find_config(worktree: Path) -> Path:
    for directory in (worktree, *worktree.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate.resolve()
    raise WcoError(
        f"No {CONFIG_NAME} was found in '{worktree}' or any parent directory."
    )


def compose_command(arguments: Sequence[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            return arguments[index] if index < len(arguments) else None
        if argument in COMPOSE_COMMANDS:
            return argument
        if argument in COMPOSE_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in COMPOSE_OPTIONS_WITH_VALUES if option.startswith("--")):
            index += 1
            continue
        if argument.startswith("-f") and argument != "-f":
            index += 1
            continue
        if not argument.startswith("-"):
            return argument
        index += 1
    return None


def reject_reserved_arguments(arguments: Sequence[str]) -> None:
    for index, argument in enumerate(arguments):
        if argument in RESERVED_PROJECT_OPTIONS:
            raise WcoError(
                f"'{argument}' is managed by wco and cannot be passed to Docker Compose."
            )
        if argument.startswith("--project-name="):
            raise WcoError(
                "'--project-name' is managed by wco and cannot be overridden."
            )
        if index and arguments[index - 1] in RESERVED_PROJECT_OPTIONS:
            raise WcoError("The Compose project name is managed by wco.")


def validate_worktree(config: WorkspaceConfig, worktree: Path, command: str | None) -> None:
    required = list(config.validation.required)
    if command in config.validation.startup_commands:
        required.extend(config.validation.startup_required)
    missing = [item for item in required if not (worktree / item).exists()]
    if missing:
        raise WcoError(
            f"'{worktree}' is missing required path(s): {', '.join(missing)}"
        )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "worktree"


def _isolated_name(base: str, worktree: Path, max_length: int = 63) -> str:
    digest = hashlib.sha256(str(worktree).encode()).hexdigest()[:8]
    suffix = f"{_slug(worktree.name)[:40].rstrip('-')}-{digest}"
    available = max_length - len(suffix) - 1
    prefix = base[:available].rstrip("-_") or "project"
    return f"{prefix}-{suffix}"


def _parse_init_arguments(arguments: Sequence[str]) -> tuple[str | None, str | None, bool]:
    compose_file: str | None = None
    project_name: str | None = None
    force = False
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--force":
            force = True
            index += 1
            continue
        if argument in {"--compose", "--project"}:
            if index + 1 >= len(arguments):
                raise WcoError(f"'{argument}' requires a value.")
            value = arguments[index + 1]
            index += 2
        elif argument.startswith("--compose="):
            argument, value = "--compose", argument.split("=", 1)[1]
            index += 1
        elif argument.startswith("--project="):
            argument, value = "--project", argument.split("=", 1)[1]
            index += 1
        else:
            raise WcoError(f"Unknown wco init option: {argument}")
        if not value:
            raise WcoError(f"'{argument}' requires a non-empty value.")
        if argument == "--compose":
            if compose_file is not None:
                raise WcoError("'--compose' may only be specified once.")
            compose_file = value
        else:
            if project_name is not None:
                raise WcoError("'--project' may only be specified once.")
            project_name = value
    return compose_file, project_name, force


def _init_compose_file(directory: Path, requested: str | None) -> str:
    if requested is None:
        for filename in COMPOSE_FILE_CANDIDATES:
            if (directory / filename).is_file():
                return filename
        candidates = ", ".join(COMPOSE_FILE_CANDIDATES)
        raise WcoError(
            f"No Compose file was found in '{directory}'. Expected one of: {candidates}. "
            "Use '--compose PATH' to select another file."
        )

    resolved = _safe_workspace_path(directory, requested, "--compose")
    if not resolved.is_file():
        raise WcoError(f"Compose file does not exist: {resolved}")
    return resolved.relative_to(directory).as_posix()


def _initial_config(compose_file: str, project_name: str) -> str:
    compose_value = json.dumps(compose_file)
    project_value = json.dumps(project_name)
    return f"""version = 1

[compose]
files = [{compose_value}]
project_name = {project_value}
instance_name = {project_value}

[environment]
SOURCE_PATH = "{{worktree}}"

[validation]
required = []
startup_required = []
startup_commands = ["up", "create", "start", "restart", "run", "watch"]

[isolation]
port_step = 100
max_slots = 500

[isolation.ports]
"""


def _write_initial_config(path: Path, content: str, force: bool) -> None:
    temporary_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=".wco.", suffix=".tmp", delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        if force:
            os.replace(temporary_name, path)
            temporary_name = None
        else:
            os.link(temporary_name, path)
    except FileExistsError as exc:
        raise WcoError(
            f"'{path}' already exists. Use 'wco init --force' to replace it."
        ) from exc
    except OSError as exc:
        raise WcoError(f"Cannot create '{path}': {exc}") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _init_command(arguments: Sequence[str], cwd: Path | None, stdout: TextIO) -> int:
    if any(argument in {"-h", "--help"} for argument in arguments[1:]):
        if len(arguments) != 2:
            raise WcoError("'--help' cannot be combined with other wco init options.")
        print(INIT_HELP, file=stdout, end="")
        return 0

    requested_compose, requested_project, force = _parse_init_arguments(arguments)
    directory = (cwd or Path.cwd()).resolve()
    config_path = directory / CONFIG_NAME
    if config_path.exists() and not force:
        raise WcoError(
            f"'{config_path}' already exists. Use 'wco init --force' to replace it."
        )
    compose_file = _init_compose_file(directory, requested_compose)
    project_name = requested_project or _slug(directory.name)
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise WcoError(
            "'--project' must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, '-' and '_'."
        )

    _write_initial_config(
        config_path,
        _initial_config(compose_file, project_name),
        force,
    )
    print(f"Created {config_path}", file=stdout)
    print(f"Compose file: {compose_file}", file=stdout)
    print(f"Project: {project_name}", file=stdout)
    print(
        "Next: update your Compose bind mounts to use ${SOURCE_PATH} and add any "
        "published port variables to [isolation.ports].",
        file=stdout,
    )
    return 0


def state_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get("XDG_STATE_HOME")
    if root:
        return Path(root).expanduser() / "wco" / "ports.json"
    return Path.home() / ".local" / "state" / "wco" / "ports.json"


def legacy_state_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get("XDG_STATE_HOME")
    if root:
        return Path(root).expanduser() / LEGACY_STATE_DIRECTORY / "ports.json"
    return (
        Path.home()
        / ".local"
        / "state"
        / LEGACY_STATE_DIRECTORY
        / "ports.json"
    )


def port_is_available(port: int) -> bool:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("0.0.0.0", port))
        return True
    except PermissionError as exc:
        raise WcoError(
            f"Cannot probe host port {port}: permission was denied by the operating environment."
        ) from exc
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()


class PortStore:
    def __init__(
        self,
        path: Path,
        availability: Callable[[int], bool] = port_is_available,
    ) -> None:
        self.path = path
        self.availability = availability

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": 1, "assignments": []}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WcoError(f"Cannot read port state '{self.path}': {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("assignments"), list)
        ):
            raise WcoError(f"Port state '{self.path}' has an unsupported format.")
        return data

    def _write(self, data: Mapping[str, object]) -> None:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, prefix="ports.", suffix=".tmp", delete=False
            ) as handle:
                temporary_name = handle.name
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(".lock")
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                handle.close()
                raise
            return handle
        except OSError as exc:
            raise WcoError(f"Cannot lock port state '{self.path}': {exc}") from exc

    @staticmethod
    def _upgrade_config_paths(data: dict[str, object]) -> bool:
        assignments = data["assignments"]
        assert isinstance(assignments, list)
        changed = False
        for item in assignments:
            if not isinstance(item, dict) or not isinstance(item.get("config"), str):
                continue
            config_path = Path(item["config"])
            if config_path.name != LEGACY_CONFIG_NAME:
                continue
            replacement = config_path.with_name(CONFIG_NAME)
            if replacement.is_file():
                item["config"] = str(replacement.resolve())
                changed = True
        return changed

    @staticmethod
    def _prune(data: dict[str, object]) -> bool:
        assignments = data["assignments"]
        assert isinstance(assignments, list)
        kept = [
            item
            for item in assignments
            if isinstance(item, dict)
            and isinstance(item.get("config"), str)
            and isinstance(item.get("worktree"), str)
            and Path(item["config"]).is_file()
            and Path(item["worktree"]).is_dir()
        ]
        changed = len(kept) != len(assignments)
        data["assignments"] = kept
        return changed

    @staticmethod
    def _find(data: Mapping[str, object], config: WorkspaceConfig, worktree: Path) -> dict[str, object] | None:
        assignments = data["assignments"]
        assert isinstance(assignments, list)
        for item in assignments:
            if (
                isinstance(item, dict)
                and item.get("config") == str(config.path)
                and item.get("worktree") == str(worktree)
            ):
                return item
        return None

    def _allocate(
        self,
        data: dict[str, object],
        config: WorkspaceConfig,
        worktree: Path,
        excluded_slot: int | None = None,
    ) -> dict[str, object]:
        assignments = data["assignments"]
        assert isinstance(assignments, list)
        reserved = {
            int(port)
            for item in assignments
            if isinstance(item, dict) and isinstance(item.get("ports"), dict)
            for port in item["ports"].values()
            if isinstance(port, int)
        }
        for slot in range(1, config.isolation.max_slots + 1):
            if slot == excluded_slot:
                continue
            ports = {
                name: base + slot * config.isolation.port_step
                for name, base in config.isolation.ports.items()
            }
            if any(port > 65535 for port in ports.values()):
                continue
            if any(port in reserved or not self.availability(port) for port in ports.values()):
                continue
            assignment: dict[str, object] = {
                "config": str(config.path),
                "worktree": str(worktree),
                "slot": slot,
                "ports": ports,
            }
            assignments.append(assignment)
            return assignment
        raise WcoError(
            f"No free isolated port slot is available within slots 1-{config.isolation.max_slots}."
        )

    def get_or_allocate(self, config: WorkspaceConfig, worktree: Path) -> tuple[int, dict[str, int]]:
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            assignment = self._find(data, config, worktree)
            if assignment is None:
                assignment = self._allocate(data, config, worktree)
                changed = True
            if changed:
                self._write(data)
            return int(assignment["slot"]), {
                str(name): int(value)
                for name, value in dict(assignment["ports"]).items()
            }

    def get(self, config: WorkspaceConfig, worktree: Path) -> tuple[int, dict[str, int]] | None:
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            if changed:
                self._write(data)
            assignment = self._find(data, config, worktree)
            if assignment is None:
                return None
            return int(assignment["slot"]), {
                str(name): int(value)
                for name, value in dict(assignment["ports"]).items()
            }

    def reallocate(self, config: WorkspaceConfig, worktree: Path) -> tuple[int, dict[str, int]]:
        with self._locked():
            data = self._read()
            self._upgrade_config_paths(data)
            self._prune(data)
            previous = self._find(data, config, worktree)
            excluded_slot = int(previous["slot"]) if previous is not None else None
            if previous is not None:
                assignments = data["assignments"]
                assert isinstance(assignments, list)
                assignments.remove(previous)
            assignment = self._allocate(data, config, worktree, excluded_slot)
            self._write(data)
            return int(assignment["slot"]), {
                str(name): int(value)
                for name, value in dict(assignment["ports"]).items()
            }


def migrate_legacy_state(environ: Mapping[str, str] | None = None) -> Path:
    destination = state_file(environ)
    legacy = legacy_state_file(environ)
    if not legacy.exists():
        return destination
    if destination.exists():
        raise WcoError(
            "Both legacy and current port state files exist. Resolve them manually before "
            f"continuing: '{legacy}' and '{destination}'."
        )

    legacy_lock = legacy.with_suffix(".lock")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_lock = destination.with_suffix(".lock")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        with destination_lock.open("a+") as destination_handle:
            fcntl.flock(destination_handle.fileno(), fcntl.LOCK_EX)
            with legacy_lock.open("a+") as legacy_handle:
                fcntl.flock(legacy_handle.fileno(), fcntl.LOCK_EX)
                if destination.exists():
                    raise WcoError(
                        "The current port state appeared during legacy migration; "
                        "no files were changed."
                    )
                if not legacy.exists():
                    return destination
                legacy_store = PortStore(legacy)
                data = legacy_store._read()
                PortStore._upgrade_config_paths(data)
                PortStore(destination)._write(data)
                legacy.unlink()
    except OSError as exc:
        raise WcoError(
            f"Cannot migrate legacy port state from '{legacy}' to '{destination}': {exc}"
        ) from exc

    try:
        legacy_lock.unlink(missing_ok=True)
        legacy.parent.rmdir()
    except OSError:
        pass
    return destination


def default_port_store() -> PortStore:
    return PortStore(migrate_legacy_state())


def build_compose_prefix(config: WorkspaceConfig, project_name: str) -> list[str]:
    command = ["docker", "compose", "--project-name", project_name]
    for compose_file in config.compose_files:
        command.extend(["--file", str(compose_file)])
    command.extend(["--project-directory", str(config.workspace)])
    return command


def _render_environment(
    config: WorkspaceConfig,
    worktree: Path,
    project_name: str,
    instance_name: str,
    ports: Mapping[str, int],
) -> dict[str, str]:
    values = {
        "worktree": str(worktree),
        "workspace": str(config.workspace),
        "project": project_name,
        "instance": instance_name,
    }
    environment = dict(os.environ)
    for name, template in config.environment.items():
        environment[name] = template.format_map(values)
    for name, port in ports.items():
        environment[name] = str(port)
    environment["COMPOSE_PROJECT_NAME"] = project_name
    return environment


def _docker_container_ids(project_name: str) -> list[str]:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project_name}",
                "--format",
                "{{.ID}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WcoError(f"Cannot run Docker: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker returned an error."
        raise WcoError(f"Cannot inspect Compose project '{project_name}': {detail}")
    return [line for line in result.stdout.splitlines() if line]


def _published_ports(project_name: str) -> set[int]:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project_name}",
                "--format",
                "{{.Ports}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WcoError(f"Cannot run Docker: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker returned an error."
        raise WcoError(f"Cannot inspect Compose project '{project_name}': {detail}")
    return {
        int(match)
        for match in re.findall(r":(\d+)->", result.stdout)
    }


def _validate_isolated_start(invocation: Invocation) -> None:
    own_ports = _published_ports(invocation.project_name)
    conflicts = [
        port
        for port in invocation.ports.values()
        if port not in own_ports and not port_is_available(port)
    ]
    if conflicts:
        raise WcoError(
            "Remembered isolated port(s) are occupied by another application: "
            f"{', '.join(map(str, sorted(conflicts)))}. Stop the conflicting application or run "
            "'wco ports reallocate'."
        )

    prefix = build_compose_prefix(invocation.config, invocation.project_name)
    result = subprocess.run(
        [*prefix, "config", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
        env=invocation.environment,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker Compose could not render the configuration."
        raise WcoError(detail)
    try:
        rendered = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WcoError(f"Docker Compose returned invalid JSON during isolation validation: {exc}") from exc
    services = rendered.get("services", {}) if isinstance(rendered, dict) else {}
    collisions: list[str] = []
    unconfigured_ports: set[int] = set()
    configured_ports = set(invocation.ports.values())
    if isinstance(services, dict):
        for service, definition in services.items():
            if not isinstance(definition, dict):
                continue
            container_name = definition.get("container_name")
            if (
                isinstance(container_name, str)
                and invocation.project_name not in container_name
                and invocation.instance_name not in container_name
            ):
                collisions.append(f"{service}={container_name}")
            published_ports = definition.get("ports", [])
            if isinstance(published_ports, list):
                for published_port in published_ports:
                    if not isinstance(published_port, dict):
                        continue
                    published = published_port.get("published")
                    try:
                        numeric_port = int(published)
                    except (TypeError, ValueError):
                        continue
                    if numeric_port and numeric_port not in configured_ports:
                        unconfigured_ports.add(numeric_port)
    if collisions:
        raise WcoError(
            "Isolated mode found fixed container name(s): "
            f"{', '.join(collisions)}. Remove 'container_name' or parameterize it with "
            "an [environment] value containing '{instance}'."
        )
    if unconfigured_ports:
        raise WcoError(
            "Isolated mode found published host port(s) that are not declared in "
            f"[isolation.ports]: {', '.join(map(str, sorted(unconfigured_ports)))}. "
            "Parameterize and register every fixed host port before using --isolated."
        )


def prepare_invocation(
    compose_args: Sequence[str],
    isolated: bool,
    cwd: Path | None = None,
    store: PortStore | None = None,
) -> Invocation:
    cwd = (cwd or Path.cwd()).resolve()
    worktree = resolve_worktree(cwd)
    config = load_config(find_config(worktree))
    reject_reserved_arguments(compose_args)
    command = compose_command(compose_args)
    validate_worktree(config, worktree, command)

    if isolated:
        project_name = _isolated_name(config.project_name, worktree)
        instance_name = _isolated_name(config.instance_name, worktree)
        if config.isolation.ports:
            store = store or default_port_store()
            slot, ports = store.get_or_allocate(config, worktree)
        else:
            slot, ports = None, {}
    else:
        project_name = config.project_name
        instance_name = config.instance_name
        slot = None
        ports = dict(config.isolation.ports)

    environment = _render_environment(
        config, worktree, project_name, instance_name, ports
    )
    return Invocation(
        worktree=worktree,
        config=config,
        isolated=isolated,
        compose_args=tuple(compose_args),
        compose_command=command,
        project_name=project_name,
        instance_name=instance_name,
        slot=slot,
        ports=ports,
        environment=environment,
    )


def _print_ports(
    stream: TextIO,
    heading: str,
    project_name: str,
    slot: int | None,
    ports: Mapping[str, int],
) -> None:
    print(heading, file=stream)
    print(f"  project: {project_name}", file=stream)
    if slot is not None:
        print(f"  slot: {slot}", file=stream)
    if ports:
        for name, port in sorted(ports.items()):
            print(f"  {name}: {port}", file=stream)
    else:
        print("  ports: none configured", file=stream)


def _port_command(arguments: Sequence[str], cwd: Path | None, stdout: TextIO) -> int:
    if len(arguments) != 2 or arguments[1] not in {"show", "reallocate"}:
        raise WcoError("Usage: wco ports <show|reallocate>")
    worktree = resolve_worktree((cwd or Path.cwd()).resolve())
    config = load_config(find_config(worktree))
    validate_worktree(config, worktree, None)
    store = default_port_store()
    isolated_project = _isolated_name(config.project_name, worktree)

    if arguments[1] == "show":
        _print_ports(
            stdout,
            "Shared:",
            config.project_name,
            None,
            config.isolation.ports,
        )
        assignment = store.get(config, worktree) if config.isolation.ports else None
        if assignment is None:
            _print_ports(stdout, "Isolated (not allocated):", isolated_project, None, {})
        else:
            slot, ports = assignment
            _print_ports(stdout, "Isolated:", isolated_project, slot, ports)
        return 0

    if not config.isolation.ports:
        raise WcoError("This workspace does not configure isolated host ports.")
    if _docker_container_ids(isolated_project):
        raise WcoError(
            "The isolated project still has containers. Run 'wco --isolated down' "
            "before reallocating its ports."
        )
    slot, ports = store.reallocate(config, worktree)
    _print_ports(stdout, "Reallocated isolated ports:", isolated_project, slot, ports)
    return 0


INIT_HELP = """Usage: wco init [OPTIONS]

Create .wco.toml in the current directory.

Options:
  --compose PATH   Use this Compose file instead of automatic detection.
  --project NAME   Use this Compose project name instead of the directory name.
  --force          Replace an existing .wco.toml.
  -h, --help       Show this help.
"""


HELP = """Usage: wco [--isolated] <docker compose arguments>
       wco init [--compose PATH] [--project NAME] [--force]
       wco ports <show|reallocate>

Run a workspace's central Docker Compose configuration against the current
Git worktree. The nearest .wco.toml at or above the worktree defines the
Compose files, environment, validation rules, and isolated ports.

Options:
  --isolated   Use a worktree-specific project name and persistent port slot.
  -h, --help   Show this help.
  --version    Show the installed wco version.

Examples:
  wco init
  wco up -d --build --force-recreate
  wco --isolated up -d
  wco --isolated down
  wco ports show
"""


def run(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    exec_fn: Callable[[str, Sequence[str], Mapping[str, str]], object] = os.execvpe,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if not arguments:
            print(HELP, file=stderr, end="")
            return 2
        if arguments in (["-h"], ["--help"]):
            print(HELP, file=stdout, end="")
            return 0
        if arguments == ["--version"]:
            print(f"wco {__version__}", file=stdout)
            return 0
        if arguments[0] == "init":
            return _init_command(arguments, cwd, stdout)
        if arguments[0] == "ports":
            return _port_command(arguments, cwd, stdout)

        isolated = False
        if arguments and arguments[0] == "--isolated":
            isolated = True
            arguments.pop(0)
        if not arguments:
            raise WcoError("Docker Compose arguments are required.")

        invocation = prepare_invocation(arguments, isolated, cwd)
        if isolated and invocation.compose_command in invocation.config.validation.startup_commands:
            _validate_isolated_start(invocation)
        command = [
            *build_compose_prefix(invocation.config, invocation.project_name),
            *invocation.compose_args,
        ]
        print(f"Using worktree: {invocation.worktree}", file=stderr)
        print(f"Compose project: {invocation.project_name}", file=stderr)
        if invocation.slot is not None:
            print(f"Isolated port slot: {invocation.slot}", file=stderr)
        exec_fn(command[0], command, invocation.environment)
        return 0
    except WcoError as exc:
        print(f"wco: error: {exc}", file=stderr)
        return 1


def main() -> None:
    raise SystemExit(run())
