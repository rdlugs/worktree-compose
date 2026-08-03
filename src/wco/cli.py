from __future__ import annotations

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
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Callable, Iterator, Mapping, Sequence, TextIO

from filelock import FileLock

from . import __version__
from .presentation import Output, PortRow, PsRow, StackRow


CONFIG_NAME = ".wco.toml"
LEGACY_CONFIG_NAME = ".bcompose.toml"
LEGACY_STATE_DIRECTORY = "bcompose"
STATE_VERSION = 2
SHARED_STACK_ID = 1
TARGET_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STARTUP_COMMANDS = ("up", "create", "start", "restart", "run", "watch")
BUILD_CONTEXT_COMMANDS = {"build", "up", "create", "run", "watch"}
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


@contextmanager
def _exclusive_lock(path: Path, subject: str) -> Iterator[None]:
    lock = FileLock(str(path), timeout=-1)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock.acquire()
    except OSError as exc:
        raise WcoError(f"Cannot lock {subject} '{path}': {exc}") from exc
    try:
        yield
    finally:
        lock.release()


@dataclass(frozen=True)
class IsolationConfig:
    port_step: int
    max_slots: int
    ports: dict[str, int]
    rewrite_container_names: bool
    rewrite_ports: bool


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
    stack_id: int
    slot: int | None
    ports: dict[str, int]
    environment: dict[str, str]


@dataclass(frozen=True)
class Target:
    """A wco ID naming a stack, and optionally one container inside it."""

    stack: int
    container: int | None

    def __str__(self) -> str:
        return f"{self.stack}" if self.container is None else f"{self.stack}.{self.container}"


@dataclass(frozen=True)
class IsolationOverride:
    path: Path
    names: dict[str, str]
    ports: dict[str, tuple[dict[str, object], ...]]
    rewritten_ports: int
    build_contexts: dict[str, str]


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
    _only_keys(
        isolation_table,
        {
            "port_step",
            "max_slots",
            "ports",
            "rewrite_container_names",
            "rewrite_ports",
        },
        "[isolation]",
    )
    port_step = isolation_table.get("port_step", 100)
    max_slots = isolation_table.get("max_slots", 500)
    rewrite_container_names = isolation_table.get("rewrite_container_names", False)
    rewrite_ports = isolation_table.get("rewrite_ports", False)
    if not isinstance(port_step, int) or isinstance(port_step, bool) or port_step <= 0:
        raise WcoError("'isolation.port_step' must be a positive integer.")
    if not isinstance(max_slots, int) or isinstance(max_slots, bool) or max_slots <= 0:
        raise WcoError("'isolation.max_slots' must be a positive integer.")
    if not isinstance(rewrite_container_names, bool):
        raise WcoError("'isolation.rewrite_container_names' must be a boolean.")
    if not isinstance(rewrite_ports, bool):
        raise WcoError("'isolation.rewrite_ports' must be a boolean.")
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
    duplicate_ports = sorted(
        port for port in set(ports.values()) if list(ports.values()).count(port) > 1
    )
    if duplicate_ports:
        raise WcoError(
            "Each [isolation.ports] base port must be unique; duplicate value(s): "
            f"{', '.join(map(str, duplicate_ports))}."
        )

    return WorkspaceConfig(
        path=path,
        workspace=workspace,
        compose_files=compose_files,
        project_name=project_name,
        instance_name=instance_name,
        environment=environment,
        validation=ValidationConfig(required, startup_required, startup_commands),
        isolation=IsolationConfig(
            port_step,
            max_slots,
            ports,
            rewrite_container_names,
            rewrite_ports,
        ),
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


def _compose_command_index(arguments: Sequence[str]) -> int | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            return index if index < len(arguments) else None
        if argument in COMPOSE_COMMANDS:
            return index
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
            return index
        index += 1
    return None


def compose_command(arguments: Sequence[str]) -> str | None:
    index = _compose_command_index(arguments)
    return arguments[index] if index is not None else None


def split_target(arguments: Sequence[str]) -> tuple[list[str], Target | None]:
    """Strip a '<stack>[.<container>]' wco ID from just after the Compose command.

    Only that one position is examined, so numeric option values and service
    names elsewhere are passed through untouched.
    """
    command_index = _compose_command_index(arguments)
    if command_index is None:
        return list(arguments), None
    position = command_index + 1
    if position >= len(arguments):
        return list(arguments), None
    token = arguments[position]
    match = TARGET_RE.match(token)
    if match is None:
        return list(arguments), None
    stack = int(match.group(1))
    container = None if match.group(2) is None else int(match.group(2))
    if stack < 1 or container == 0:
        raise WcoError(f"'{token}' is not a valid wco ID; IDs start at 1.")
    remaining = [*arguments[:position], *arguments[position + 1 :]]
    return remaining, Target(stack=stack, container=container)


def _insert_after_command(arguments: Sequence[str], extra: Sequence[str]) -> list[str]:
    if not extra:
        return list(arguments)
    command_index = _compose_command_index(arguments)
    if command_index is None:
        raise WcoError("Cannot locate the Docker Compose command.")
    return [
        *arguments[: command_index + 1],
        *extra,
        *arguments[command_index + 1 :],
    ]


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


def _init_command(arguments: Sequence[str], cwd: Path | None, output: Output) -> int:
    if any(argument in {"-h", "--help"} for argument in arguments[1:]):
        if len(arguments) != 2:
            raise WcoError("'--help' cannot be combined with other wco init options.")
        output.help(INIT_HELP)
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
    output.initialized(config_path, compose_file, project_name)
    return 0


def state_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get("XDG_STATE_HOME")
    if root:
        return Path(root).expanduser() / "wco" / "ports.json"
    if sys.platform == "win32":
        root = environ.get("LOCALAPPDATA")
        if root:
            return Path(root).expanduser() / "wco" / "ports.json"
    return Path.home() / ".local" / "state" / "wco" / "ports.json"


def override_directory(environ: Mapping[str, str] | None = None) -> Path:
    return state_file(environ).parent / "overrides"


def legacy_state_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get("XDG_STATE_HOME")
    if root:
        return Path(root).expanduser() / LEGACY_STATE_DIRECTORY / "ports.json"
    if sys.platform == "win32":
        root = environ.get("LOCALAPPDATA")
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
            return {"version": STATE_VERSION, "assignments": [], "stacks": []}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WcoError(f"Cannot read port state '{self.path}': {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") not in {1, STATE_VERSION}
            or not isinstance(data.get("assignments"), list)
        ):
            raise WcoError(f"Port state '{self.path}' has an unsupported format.")
        if data.get("version") == 1:
            # Version 1 predates stack IDs; the rest of the file is unchanged.
            data["version"] = STATE_VERSION
            data["stacks"] = []
        elif not isinstance(data.get("stacks"), list):
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

    def _locked(self) -> AbstractContextManager[None]:
        return _exclusive_lock(self.path.with_suffix(".lock"), "port state")

    @staticmethod
    def _upgrade_config_paths(data: dict[str, object]) -> bool:
        changed = False
        for key in ("assignments", "stacks"):
            entries = data[key]
            assert isinstance(entries, list)
            for item in entries:
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
    def _live(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("config"), str)
            and isinstance(item.get("worktree"), str)
            and Path(item["config"]).is_file()
            and Path(item["worktree"]).is_dir()
        )

    @classmethod
    def _prune(cls, data: dict[str, object]) -> bool:
        changed = False
        for key in ("assignments", "stacks"):
            entries = data[key]
            assert isinstance(entries, list)
            kept = [item for item in entries if cls._live(item)]
            changed = changed or len(kept) != len(entries)
            data[key] = kept
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

    def _refresh_assignment(
        self,
        data: Mapping[str, object],
        assignment: dict[str, object],
        config: WorkspaceConfig,
    ) -> bool:
        slot = int(assignment["slot"])
        expected = {
            name: base + slot * config.isolation.port_step
            for name, base in config.isolation.ports.items()
        }
        current = {
            str(name): int(value)
            for name, value in dict(assignment.get("ports", {})).items()
        }
        if current == expected:
            return False
        if slot < 1 or slot > config.isolation.max_slots or any(
            port > 65535 for port in expected.values()
        ):
            raise WcoError(
                "The isolated port configuration changed and the remembered slot is no "
                "longer valid. Run 'wco --isolated down', then 'wco ports reallocate'."
            )
        assignments = data["assignments"]
        assert isinstance(assignments, list)
        reserved = {
            int(port)
            for item in assignments
            if item is not assignment
            and isinstance(item, dict)
            and isinstance(item.get("ports"), dict)
            for port in item["ports"].values()
            if isinstance(port, int)
        }
        own_ports = set(current.values())
        unavailable = sorted(
            port
            for port in expected.values()
            if port in reserved
            or (port not in own_ports and not self.availability(port))
        )
        if unavailable:
            raise WcoError(
                "The isolated port configuration changed, but the remembered slot's new "
                f"port(s) are unavailable: {', '.join(map(str, unavailable))}. Run "
                "'wco --isolated down', then 'wco ports reallocate'."
            )
        assignment["ports"] = expected
        return True

    def get_or_allocate(self, config: WorkspaceConfig, worktree: Path) -> tuple[int, dict[str, int]]:
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            assignment = self._find(data, config, worktree)
            if assignment is None:
                assignment = self._allocate(data, config, worktree)
                changed = True
            else:
                changed = self._refresh_assignment(data, assignment, config) or changed
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
            assignment = self._find(data, config, worktree)
            if assignment is None:
                if changed:
                    self._write(data)
                return None
            changed = self._refresh_assignment(data, assignment, config) or changed
            if changed:
                self._write(data)
            return int(assignment["slot"]), {
                str(name): int(value)
                for name, value in dict(assignment["ports"]).items()
            }

    def list_assignments(
        self, config: WorkspaceConfig
    ) -> list[tuple[Path, int, dict[str, int]]]:
        """Every recorded assignment for this workspace, as stored.

        Unlike get(), stored ports are reported verbatim: listing another
        worktree's assignment must never rewrite it.
        """
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            if changed:
                self._write(data)
            assignments = data["assignments"]
            assert isinstance(assignments, list)
            listed = [
                (
                    Path(str(item["worktree"])),
                    int(item["slot"]),
                    {
                        str(name): int(value)
                        for name, value in dict(item["ports"]).items()
                    },
                )
                for item in assignments
                if isinstance(item, dict) and item.get("config") == str(config.path)
            ]
            return sorted(listed, key=lambda entry: str(entry[0]))

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

    @staticmethod
    def _stack_entries(
        data: Mapping[str, object], config: WorkspaceConfig
    ) -> list[dict[str, object]]:
        stacks = data["stacks"]
        assert isinstance(stacks, list)
        return [
            item
            for item in stacks
            if isinstance(item, dict)
            and item.get("config") == str(config.path)
            and isinstance(item.get("id"), int)
        ]

    def stack_id(self, config: WorkspaceConfig, worktree: Path) -> int:
        """The persistent integer ID of this worktree's isolated stack.

        IDs start at SHARED_STACK_ID + 1: the shared project is always ID 1 and
        is never recorded. The lowest free ID is reused once _prune drops a
        worktree that no longer exists.
        """
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            entries = self._stack_entries(data, config)
            for item in entries:
                if item.get("worktree") == str(worktree):
                    if changed:
                        self._write(data)
                    return int(item["id"])
            taken = {int(item["id"]) for item in entries}
            identifier = SHARED_STACK_ID + 1
            while identifier in taken:
                identifier += 1
            stacks = data["stacks"]
            assert isinstance(stacks, list)
            stacks.append(
                {
                    "config": str(config.path),
                    "worktree": str(worktree),
                    "id": identifier,
                }
            )
            self._write(data)
            return identifier

    def list_stack_ids(self, config: WorkspaceConfig) -> dict[Path, int]:
        """Every recorded stack ID for this workspace, as stored."""
        with self._locked():
            data = self._read()
            changed = self._upgrade_config_paths(data)
            changed = self._prune(data) or changed
            listed = {
                Path(str(item["worktree"])): int(item["id"])
                for item in self._stack_entries(data, config)
            }
            if changed:
                self._write(data)
            return listed

    def worktree_for_stack_id(
        self, config: WorkspaceConfig, stack_id: int
    ) -> Path | None:
        for worktree, identifier in self.list_stack_ids(config).items():
            if identifier == stack_id:
                return worktree
        return None


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
        destination_lock = destination.with_suffix(".lock")
        with _exclusive_lock(
            destination_lock, "destination port state"
        ):
            with _exclusive_lock(legacy_lock, "legacy port state"):
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


def _compose_global_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    command_index = _compose_command_index(arguments)
    if command_index is None:
        raise WcoError("Docker Compose arguments are required.")
    return tuple(arguments[:command_index])


def _uses_stdin_compose_file(arguments: Sequence[str]) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-f", "--file"}:
            if index + 1 < len(arguments) and arguments[index + 1] == "-":
                return True
            index += 2
            continue
        if argument == "-f-" or argument == "--file=-":
            return True
        index += 1
    return False


def _container_override_path(
    invocation: Invocation,
    directory: Path,
) -> Path:
    context = json.dumps(
        {
            "config": str(invocation.config.path),
            "worktree": str(invocation.worktree),
            "files": [str(path) for path in invocation.config.compose_files],
            "global_arguments": _compose_global_arguments(invocation.compose_args),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(context.encode()).hexdigest()[:16]
    return directory / f"{digest}.compose.yaml"


def _generated_container_name(
    instance_name: str,
    original_name: str,
    service: str,
    used: set[str],
    max_length: int = 63,
) -> str:
    raw = f"{instance_name}-{original_name}"
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    if not normalized or not normalized[0].isalnum():
        normalized = f"container-{normalized}".rstrip("-._")
    candidate = normalized
    if len(candidate) > max_length or candidate in used:
        digest = hashlib.sha256(
            f"{service}\0{raw}".encode()
        ).hexdigest()[:8]
        readable = normalized[: max_length - len(digest) - 1].rstrip("-._")
        candidate = f"{readable or 'container'}-{digest}"
    if not CONTAINER_NAME_RE.fullmatch(candidate):
        raise WcoError(
            f"Cannot generate a valid isolated container name for service '{service}'."
        )
    used.add(candidate)
    return candidate


def _container_name_mappings(
    invocation: Invocation,
    rendered: Mapping[str, object],
) -> dict[str, str]:
    services = rendered.get("services", {})
    if not isinstance(services, dict):
        raise WcoError("Docker Compose rendered an invalid services definition.")
    used = {
        value
        for definition in services.values()
        if isinstance(definition, dict)
        for value in [definition.get("container_name")]
        if isinstance(value, str)
        and (
            invocation.project_name in value
            or invocation.instance_name in value
        )
    }
    mappings: dict[str, str] = {}
    for service in sorted(services):
        definition = services[service]
        if not isinstance(service, str) or not isinstance(definition, dict):
            continue
        container_name = definition.get("container_name")
        if not isinstance(container_name, str):
            continue
        if (
            invocation.project_name in container_name
            or invocation.instance_name in container_name
        ):
            continue
        mappings[service] = _generated_container_name(
            invocation.instance_name,
            container_name,
            service,
            used,
        )
    return mappings


def _port_mappings(
    invocation: Invocation,
    rendered: Mapping[str, object],
) -> tuple[dict[str, tuple[dict[str, object], ...]], int]:
    if not invocation.config.isolation.rewrite_ports:
        return {}, 0
    services = rendered.get("services", {})
    if not isinstance(services, dict):
        raise WcoError("Docker Compose rendered an invalid services definition.")
    allocated_by_base = {
        base: invocation.ports[name]
        for name, base in invocation.config.isolation.ports.items()
        if name in invocation.ports
    }
    mappings: dict[str, tuple[dict[str, object], ...]] = {}
    rewritten = 0
    for service in sorted(services):
        definition = services[service]
        if not isinstance(service, str) or not isinstance(definition, dict):
            continue
        ports = definition.get("ports", [])
        if not isinstance(ports, list):
            continue
        replacements: list[dict[str, object]] = []
        service_rewritten = 0
        for port in ports:
            if not isinstance(port, dict):
                continue
            replacement = dict(port)
            try:
                published = int(port.get("published"))
            except (TypeError, ValueError):
                replacements.append(replacement)
                continue
            allocated = allocated_by_base.get(published)
            if allocated is not None and allocated != published:
                replacement["published"] = str(allocated)
                service_rewritten += 1
            replacements.append(replacement)
        if service_rewritten:
            mappings[service] = tuple(replacements)
            rewritten += service_rewritten
    return mappings, rewritten


def _write_isolation_override(path: Path, override: IsolationOverride) -> None:
    content = ["services:"]
    services = sorted(
        set(override.names) | set(override.ports) | set(override.build_contexts)
    )
    for service in services:
        content.append(f"  {json.dumps(service)}:")
        build_context = override.build_contexts.get(service)
        if build_context is not None:
            content.append("    build:")
            content.append(f"      context: {json.dumps(build_context)}")
        container_name = override.names.get(service)
        if container_name is not None:
            content.append(f"    container_name: {json.dumps(container_name)}")
        ports = override.ports.get(service)
        if ports is not None:
            content.append("    ports: !override")
            for port in ports:
                content.append(f"      - {json.dumps(port, sort_keys=True)}")
    document = "\n".join(content) + "\n"
    temporary_name: str | None = None
    with _exclusive_lock(path.with_suffix(".lock"), "isolation override"):
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                prefix="isolation.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o644)
            os.replace(temporary_name, path)
            temporary_name = None
        except OSError as exc:
            raise WcoError(
                f"Cannot write isolation override '{path}': {exc}"
            ) from exc
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)


def build_compose_prefix(
    config: WorkspaceConfig, project_name: str, project_directory: Path
) -> list[str]:
    command = ["docker", "compose", "--project-name", project_name]
    for compose_file in config.compose_files:
        command.extend(["--file", str(compose_file)])
    command.extend(["--project-directory", str(project_directory)])
    return command


def build_invocation_command(
    invocation: Invocation,
    override: IsolationOverride | None = None,
    compose_args: Sequence[str] | None = None,
) -> list[str]:
    arguments = list(invocation.compose_args if compose_args is None else compose_args)
    command_index = _compose_command_index(arguments)
    if command_index is None:
        raise WcoError("Docker Compose arguments are required.")
    command = build_compose_prefix(
        invocation.config,
        invocation.project_name,
        invocation.worktree,
    )
    command.extend(arguments[:command_index])
    if override is not None:
        command.extend(["--file", str(override.path)])
    command.extend(arguments[command_index:])
    return command


def _render_compose_config(
    invocation: Invocation,
    override: IsolationOverride | None,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    *,
    all_profiles: bool,
) -> dict[str, object]:
    arguments = list(_compose_global_arguments(invocation.compose_args))
    if all_profiles:
        arguments.extend(["--profile", "*"])
    arguments.extend(["config", "--format", "json"])
    command = build_invocation_command(invocation, override, arguments)
    try:
        result = run_process(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=invocation.environment,
        )
    except OSError as exc:
        raise WcoError(f"Cannot run Docker Compose: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker Compose could not render the configuration."
        raise WcoError(detail)
    try:
        rendered = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WcoError(
            f"Docker Compose returned invalid JSON while preparing overrides: {exc}"
        ) from exc
    if not isinstance(rendered, dict):
        raise WcoError("Docker Compose returned an invalid configuration model.")
    return rendered


def _workspace_build_context_mappings(
    invocation: Invocation,
    rendered: Mapping[str, object],
) -> dict[str, str]:
    """Find relative build contexts that only exist in the central workspace.

    Compose resolves every relative path from the project directory. WCO uses the
    active worktree as that directory so relative bind mounts keep targeting the
    selected checkout. Central Docker build assets are the exception: when the
    worktree-relative context is absent but the same path exists beside the WCO
    configuration, override just that context with its absolute workspace path.
    """
    if invocation.worktree == invocation.config.workspace:
        return {}
    services = rendered.get("services", {})
    if not isinstance(services, dict):
        return {}
    mappings: dict[str, str] = {}
    for service, definition in services.items():
        if not isinstance(service, str) or not isinstance(definition, dict):
            continue
        build = definition.get("build")
        if not isinstance(build, dict):
            continue
        context = build.get("context")
        if not isinstance(context, str):
            continue
        rendered_path = Path(context)
        if not rendered_path.is_absolute() or rendered_path.exists():
            continue
        try:
            relative = rendered_path.relative_to(invocation.worktree)
        except ValueError:
            continue
        workspace_path = invocation.config.workspace / relative
        if workspace_path.exists():
            mappings[service] = str(workspace_path.resolve())
    return mappings


def prepare_isolation_override(
    invocation: Invocation,
    run_process: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    directory: Path | None = None,
) -> IsolationOverride | None:
    rewrites_isolation = invocation.isolated and (
        invocation.config.isolation.rewrite_container_names
        or invocation.config.isolation.rewrite_ports
    )
    checks_build_contexts = invocation.compose_command in BUILD_CONTEXT_COMMANDS
    if not rewrites_isolation and not checks_build_contexts:
        return None
    run_process = run_process or subprocess.run
    global_arguments = _compose_global_arguments(invocation.compose_args)
    if _uses_stdin_compose_file(global_arguments):
        if not rewrites_isolation:
            return None
        raise WcoError(
            "Generated overrides cannot be used with '--file -'. "
            "Use a named Compose file instead."
        )
    rendered = _render_compose_config(
        invocation,
        None,
        run_process,
        all_profiles=True,
    )
    build_contexts = _workspace_build_context_mappings(invocation, rendered)
    names = (
        _container_name_mappings(invocation, rendered)
        if invocation.isolated
        and invocation.config.isolation.rewrite_container_names
        else {}
    )
    ports, rewritten_ports = (
        _port_mappings(invocation, rendered)
        if invocation.isolated and invocation.config.isolation.rewrite_ports
        else ({}, 0)
    )
    if not names and not ports and not build_contexts:
        return None
    path = _container_override_path(
        invocation,
        (directory or override_directory()).resolve(),
    )
    override = IsolationOverride(
        path, names, ports, rewritten_ports, build_contexts
    )
    _write_isolation_override(path, override)
    return override


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


def _docker_container_ids(
    project_name: str,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    try:
        result = run_process(
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


def _validate_isolated_start(
    invocation: Invocation,
    override: IsolationOverride | None = None,
    run_process: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    run_process = run_process or subprocess.run
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

    rendered = _render_compose_config(
        invocation,
        override,
        run_process,
        all_profiles=False,
    )
    services = rendered.get("services", {})
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
        if invocation.config.isolation.rewrite_container_names:
            raise WcoError(
                "Generated isolation override did not resolve fixed container name(s): "
                f"{', '.join(collisions)}."
            )
        raise WcoError(
            "Isolated mode found fixed container name(s): "
            f"{', '.join(collisions)}. Remove 'container_name' or parameterize it with "
            "an [environment] value containing '{instance}'."
        )
    if unconfigured_ports:
        guidance = (
            "Add every fixed host port as a base value in [isolation.ports], or "
            "parameterize it in the Compose file."
            if invocation.config.isolation.rewrite_ports
            else "Parameterize and register every fixed host port before using --isolated."
        )
        raise WcoError(
            "Isolated mode found published host port(s) that are not declared in "
            f"[isolation.ports]: {', '.join(map(str, sorted(unconfigured_ports)))}. "
            f"{guidance}"
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
        # Stack IDs are allocated for every isolated worktree, whether or not
        # the workspace declares isolated ports.
        store = store or default_port_store()
        stack_id = store.stack_id(config, worktree)
        if config.isolation.ports:
            slot, ports = store.get_or_allocate(config, worktree)
        else:
            slot, ports = None, {}
    else:
        project_name = config.project_name
        instance_name = config.instance_name
        stack_id = SHARED_STACK_ID
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
        stack_id=stack_id,
        slot=slot,
        ports=ports,
        environment=environment,
    )


def _parse_port_arguments(arguments: Sequence[str]) -> tuple[str, str, bool]:
    if len(arguments) < 2 or arguments[1] not in {"show", "reallocate"}:
        raise WcoError(
            "Usage: wco ports <show|reallocate> [--all] [--format table|json]"
        )
    action = arguments[1]
    output_format = "table"
    format_seen = False
    all_worktrees = False
    index = 2
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-a", "--all"}:
            if action != "show":
                raise WcoError("'--all' is only valid for 'wco ports show'.")
            all_worktrees = True
            index += 1
            continue
        if argument == "--format":
            if index + 1 >= len(arguments):
                raise WcoError("'--format' requires a value.")
            value = arguments[index + 1]
            index += 2
        elif argument.startswith("--format="):
            value = argument.split("=", 1)[1]
            index += 1
        else:
            raise WcoError(f"Unknown wco ports option: {argument}")
        if format_seen:
            raise WcoError("'--format' may only be specified once.")
        if value not in {"table", "json"}:
            raise WcoError("'--format' must be either 'table' or 'json'.")
        output_format = value
        format_seen = True
    return action, output_format, all_worktrees


def _port_rows(
    mode: str,
    project: str,
    slot: int | None,
    ports: Mapping[str, int],
    status: str,
    worktree: str = "-",
    branch: str = "-",
) -> list[PortRow]:
    slot_value = str(slot) if slot is not None else "-"
    if not ports:
        return [
            PortRow(mode, project, slot_value, "-", "-", status, worktree, branch)
        ]
    return [
        PortRow(
            mode, project, slot_value, name, str(port), status, worktree, branch
        )
        for name, port in sorted(ports.items())
    ]


def _write_json(stream: TextIO, value: Mapping[str, object]) -> None:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _show_all_ports(
    config: WorkspaceConfig,
    worktree: Path,
    store: PortStore,
    output: Output,
    stdout: TextIO,
    output_format: str,
    run_process: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    run_process = run_process or subprocess.run
    assignments = store.list_assignments(config)
    configured = set(config.isolation.ports)
    entries = [
        {
            "worktree": str(assigned_worktree),
            "branch": _worktree_branch(str(assigned_worktree), run_process),
            "project": _isolated_name(config.project_name, assigned_worktree),
            "slot": slot,
            "ports": dict(sorted(ports.items())),
            "status": "allocated" if set(ports) == configured else "outdated",
        }
        for assigned_worktree, slot, ports in assignments
    ]
    if output_format == "json":
        _write_json(
            stdout,
            {
                "worktree": str(worktree),
                "shared": {
                    "project": config.project_name,
                    "slot": None,
                    "ports": dict(sorted(config.isolation.ports.items())),
                },
                "isolated": entries,
            },
        )
        return 0

    rows = _port_rows(
        "shared",
        config.project_name,
        None,
        config.isolation.ports,
        "configured" if config.isolation.ports else "not configured",
    )
    for entry in entries:
        rows.extend(
            _port_rows(
                "isolated",
                str(entry["project"]),
                int(str(entry["slot"])),
                entry["ports"],  # type: ignore[arg-type]
                str(entry["status"]),
                str(entry["worktree"]),
                str(entry["branch"]),
            )
        )
    output.ports(
        "Port assignments  all worktrees",
        rows,
        show_worktree=True,
        current_worktree=worktree,
    )
    if not entries:
        output.hint(
            "No worktree has an isolated port slot yet. Run 'wco --isolated up -d' "
            "in a worktree to allocate one."
        )
    return 0


def _port_command(
    arguments: Sequence[str],
    cwd: Path | None,
    output: Output,
    stdout: TextIO,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    action, output_format, all_worktrees = _parse_port_arguments(arguments)
    worktree = resolve_worktree((cwd or Path.cwd()).resolve())
    config = load_config(find_config(worktree))
    validate_worktree(config, worktree, None)
    store = default_port_store()
    isolated_project = _isolated_name(config.project_name, worktree)

    if action == "show" and all_worktrees:
        return _show_all_ports(
            config, worktree, store, output, stdout, output_format, run_process
        )

    if action == "show":
        assignment = store.get(config, worktree) if config.isolation.ports else None
        if assignment is None:
            isolated_slot, isolated_ports, allocated = None, {}, False
        else:
            isolated_slot, isolated_ports = assignment
            allocated = True
        if output_format == "json":
            _write_json(
                stdout,
                {
                    "worktree": str(worktree),
                    "shared": {
                        "project": config.project_name,
                        "slot": None,
                        "ports": dict(sorted(config.isolation.ports.items())),
                    },
                    "isolated": {
                        "project": isolated_project,
                        "allocated": allocated,
                        "slot": isolated_slot,
                        "ports": dict(sorted(isolated_ports.items())),
                    },
                },
            )
            return 0
        shared_status = "configured" if config.isolation.ports else "not configured"
        isolated_status = "allocated" if allocated else "not allocated"
        rows = _port_rows(
            "shared",
            config.project_name,
            None,
            config.isolation.ports,
            shared_status,
        )
        rows.extend(
            _port_rows(
                "isolated",
                isolated_project,
                isolated_slot,
                isolated_ports,
                isolated_status,
            )
        )
        output.ports("Port assignments", rows)
        return 0

    if not config.isolation.ports:
        raise WcoError("This workspace does not configure isolated host ports.")
    if _docker_container_ids(isolated_project):
        raise WcoError(
            "The isolated project still has containers. Run 'wco --isolated down' "
            "before reallocating its ports."
        )
    slot, ports = store.reallocate(config, worktree)
    if output_format == "json":
        _write_json(
            stdout,
            {
                "worktree": str(worktree),
                "isolated": {
                    "project": isolated_project,
                    "allocated": True,
                    "slot": slot,
                    "ports": dict(sorted(ports.items())),
                },
            },
        )
    else:
        output.ports(
            "Reallocated isolated ports",
            _port_rows("isolated", isolated_project, slot, ports, "allocated"),
        )
    return 0


def _parse_stacks_arguments(arguments: Sequence[str]) -> tuple[bool, str]:
    include_stopped = False
    output_format = "table"
    format_seen = False
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-a", "--all"}:
            include_stopped = True
            index += 1
            continue
        if argument == "--format":
            if index + 1 >= len(arguments):
                raise WcoError("'--format' requires a value.")
            value = arguments[index + 1]
            index += 2
        elif argument.startswith("--format="):
            value = argument.split("=", 1)[1]
            index += 1
        else:
            raise WcoError(f"Unknown wco stacks option: {argument}")
        if format_seen:
            raise WcoError("'--format' may only be specified once.")
        if value not in {"table", "json"}:
            raise WcoError("'--format' must be either 'table' or 'json'.")
        output_format = value
        format_seen = True
    return include_stopped, output_format


def _compose_container_ids(
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    *,
    include_stopped: bool,
) -> list[str]:
    command = ["docker", "ps"]
    if include_stopped:
        command.append("-a")
    command.extend(
        [
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            "{{.ID}}",
        ]
    )
    try:
        result = run_process(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise WcoError(f"Cannot run Docker: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker returned an error."
        raise WcoError(f"Cannot list Docker Compose containers: {detail}")
    return [line for line in result.stdout.splitlines() if line]


def _container_label(inspection: Mapping[str, object], label: str) -> str:
    config = inspection.get("Config", {})
    if not isinstance(config, dict):
        return ""
    labels = config.get("Labels", {})
    if not isinstance(labels, dict):
        return ""
    value = labels.get(label)
    return value if isinstance(value, str) else ""


def _container_number(inspection: Mapping[str, object]) -> int:
    try:
        return int(_container_label(inspection, "com.docker.compose.container-number"))
    except ValueError:
        return 0


def _container_sort_key(service: str, number: int, name: str) -> tuple[str, int, str]:
    """The ordering that assigns container sub-IDs, used for listing and lookup."""
    return (service, number, name)


def _stack_containers(
    project_name: str,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    output: Output,
) -> list[str]:
    """Service names in one Compose project, in container sub-ID order."""
    ids = _docker_container_ids(project_name, run_process)
    inspections = _inspect_containers(ids, run_process, output)
    entries: list[tuple[tuple[str, int, str], str]] = []
    for container_id in ids:
        inspection = inspections.get(container_id)
        if inspection is None:
            continue
        service = _container_label(inspection, "com.docker.compose.service")
        name = str(inspection.get("Name") or container_id[:12]).lstrip("/")
        entries.append(
            (_container_sort_key(service, _container_number(inspection), name), service)
        )
    entries.sort(key=lambda entry: entry[0])
    return [service for _key, service in entries]


def _resolve_target(
    target: Target,
    command: str | None,
    isolated: bool,
    cwd: Path | None,
    output: Output,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[Path, bool, list[str]]:
    """Map a wco ID to the worktree, mode, and extra Compose arguments it names."""
    worktree = resolve_worktree((cwd or Path.cwd()).resolve())
    config = load_config(find_config(worktree))

    if target.stack == SHARED_STACK_ID:
        if isolated:
            raise WcoError(
                f"Stack {SHARED_STACK_ID} is this workspace's shared stack; "
                "drop '--isolated'."
            )
        target_worktree, target_isolated = worktree, False
        project_name = config.project_name
    else:
        found = default_port_store().worktree_for_stack_id(config, target.stack)
        if found is None:
            raise WcoError(
                f"No stack has ID {target.stack} in this workspace. "
                "Run 'wco stacks' to list them."
            )
        if not found.is_dir():
            raise WcoError(
                f"Stack {target.stack} refers to '{found}', which no longer exists. "
                "Remove its containers with 'docker compose -p "
                f"{_isolated_name(config.project_name, found)} down'."
            )
        target_worktree, target_isolated = found, True
        project_name = _isolated_name(config.project_name, found)

    if target.container is None:
        return target_worktree, target_isolated, []

    if command == "down":
        raise WcoError(
            f"'down' targets a whole stack. Use 'wco stop {target}' to stop one "
            f"container, or 'wco down {target.stack}' to bring the stack down."
        )
    services = _stack_containers(project_name, run_process, output)
    if not services:
        raise WcoError(
            f"Stack {target.stack} has no containers, so '{target}' cannot be "
            "resolved. Start it with 'wco up -d' first."
        )
    if target.container > len(services):
        raise WcoError(
            f"Stack {target.stack} has {len(services)} container(s); "
            f"'{target}' is out of range (valid: {target.stack}.1-"
            f"{target.stack}.{len(services)})."
        )
    return target_worktree, target_isolated, [services[target.container - 1]]


def _stack_mode(config: WorkspaceConfig, project: str, worktree: str) -> str | None:
    if project == config.project_name:
        return "shared"
    if worktree != "-" and project == _isolated_name(
        config.project_name, Path(worktree)
    ):
        return "isolated"
    return None


def _stack_rows(
    config: WorkspaceConfig,
    inspections: Mapping[str, Mapping[str, object]],
    ids: Sequence[str],
    now: datetime,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    stack_ids: Mapping[str, int] | None = None,
) -> list[StackRow]:
    stack_ids = {} if stack_ids is None else stack_ids
    ordered: list[tuple[tuple[int, str, int, str], StackRow]] = []
    branches: dict[str, str] = {}
    for container_id in ids:
        inspection = inspections.get(container_id)
        if inspection is None:
            continue
        worktree = _container_worktree(inspection)
        project = _container_label(inspection, "com.docker.compose.project")
        mode = _stack_mode(config, project, worktree)
        if mode is None:
            continue
        status, state, health = _container_status({}, inspection, now)
        name = str(inspection.get("Name") or container_id[:12] or "-").lstrip("/")
        if worktree not in branches:
            branches[worktree] = _worktree_branch(worktree, run_process)
        stack_id = (
            SHARED_STACK_ID if mode == "shared" else stack_ids.get(worktree, 0)
        )
        service = _container_label(inspection, "com.docker.compose.service")
        ordered.append(
            (
                (stack_id, *_container_sort_key(
                    service, _container_number(inspection), name
                )),
                StackRow(
                    name=name,
                    status=status,
                    state=state,
                    health=health,
                    worktree=worktree,
                    branch=branches[worktree],
                    mode=mode,
                    project=project,
                    id=str(stack_id),
                ),
            )
        )
    ordered.sort(key=lambda entry: entry[0])

    # Sub-IDs are per stack, numbered in the order the rows are displayed.
    rows: list[StackRow] = []
    position = 0
    previous: str | None = None
    for _key, row in ordered:
        position = position + 1 if row.id == previous else 1
        previous = row.id
        rows.append(replace(row, id="-" if row.id == "0" else f"{row.id}.{position}"))
    return rows


def _assign_stack_ids(
    config: WorkspaceConfig,
    inspections: Mapping[str, Mapping[str, object]],
    ids: Sequence[str],
) -> dict[str, int]:
    """Stack IDs keyed by worktree, allocating one for each isolated stack seen.

    'wco stacks' is where users learn an ID, so a stack that is running but has
    never been through prepare_invocation still gets one here.
    """
    store = default_port_store()
    assigned = {
        str(worktree): identifier
        for worktree, identifier in store.list_stack_ids(config).items()
    }
    for container_id in ids:
        inspection = inspections.get(container_id)
        if inspection is None:
            continue
        worktree = _container_worktree(inspection)
        project = _container_label(inspection, "com.docker.compose.project")
        if _stack_mode(config, project, worktree) != "isolated":
            continue
        if worktree in assigned or not Path(worktree).is_dir():
            continue
        assigned[worktree] = store.stack_id(config, Path(worktree))
    return assigned


def _is_down_all(arguments: Sequence[str]) -> bool:
    """True for 'wco --isolated down --all', which Compose itself has no flag for."""
    command_index = _compose_command_index(arguments)
    if command_index is None or arguments[command_index] != "down":
        return False
    return "--all" in arguments[command_index + 1 :]


def _isolated_worktrees(
    config: WorkspaceConfig,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    output: Output,
) -> list[Path]:
    """Every worktree with a stack ID, an isolated slot, or an isolated container."""
    store = default_port_store()
    worktrees: list[Path] = list(store.list_stack_ids(config))
    for worktree, _slot, _ports in store.list_assignments(config):
        if worktree not in worktrees:
            worktrees.append(worktree)
    ids = _compose_container_ids(run_process, include_stopped=True)
    inspections = _inspect_containers(ids, run_process, output)
    for container_id in ids:
        inspection = inspections.get(container_id)
        if inspection is None:
            continue
        worktree = _container_worktree(inspection)
        project = _container_label(inspection, "com.docker.compose.project")
        if _stack_mode(config, project, worktree) != "isolated":
            continue
        candidate = Path(worktree)
        if candidate not in worktrees:
            worktrees.append(candidate)
    return sorted(worktrees, key=str)


def _isolated_down_all(
    arguments: Sequence[str],
    cwd: Path | None,
    output: Output,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    compose_args = [argument for argument in arguments if argument != "--all"]
    worktree = resolve_worktree((cwd or Path.cwd()).resolve())
    config = load_config(find_config(worktree))
    targets = _isolated_worktrees(config, run_process, output)
    if not targets:
        output.hint("No worktree has an isolated stack for this workspace.")
        return 0

    status = 0
    for target in targets:
        if not target.is_dir():
            output.warning(
                f"Skipping '{target}': the worktree no longer exists. Remove its "
                "containers with 'docker compose -p <project> down'."
            )
            status = status or 1
            continue
        invocation = prepare_invocation(compose_args, True, target)
        if invocation.config.path != config.path:
            output.warning(
                f"Skipping '{target}': it belongs to workspace "
                f"'{invocation.config.path}'."
            )
            continue
        override = prepare_isolation_override(invocation, run_process)
        _print_context(output, invocation, override, target)
        result = run_process(
            build_invocation_command(invocation, override),
            check=False,
            env=invocation.environment,
        )
        if result.returncode != 0:
            status = status or result.returncode
    return status


def _stacks_command(
    arguments: Sequence[str],
    cwd: Path | None,
    output: Output,
    stdout: TextIO,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    now: datetime,
) -> int:
    if len(arguments) > 1 and arguments[1] in {"-h", "--help"}:
        output.help(STACKS_HELP)
        return 0
    include_stopped, output_format = _parse_stacks_arguments(arguments)
    worktree = resolve_worktree((cwd or Path.cwd()).resolve())
    config = load_config(find_config(worktree))
    ids = _compose_container_ids(run_process, include_stopped=include_stopped)
    inspections = _inspect_containers(ids, run_process, output)
    stack_ids = _assign_stack_ids(config, inspections, ids)
    rows = _stack_rows(config, inspections, ids, now, run_process, stack_ids)
    if output_format == "json":
        _write_json(
            stdout,
            {
                "worktree": str(worktree),
                "project": config.project_name,
                "containers": [
                    {
                        "id": None if row.id == "-" else row.id,
                        "stack_id": None if row.id == "-" else int(row.id.split(".")[0]),
                        "mode": row.mode,
                        "project": row.project,
                        "name": row.name,
                        "status": row.status,
                        "state": row.state,
                        "health": row.health,
                        "worktree": None if row.worktree == "-" else row.worktree,
                        "branch": None if row.branch == "-" else row.branch,
                    }
                    for row in rows
                ],
            },
        )
        return 0
    output.stacks(rows, current_worktree=worktree)
    return 0


def _ps_uses_native_output(arguments: Sequence[str], output: Output) -> bool:
    if not output.stdout.is_terminal:
        return True
    command_index = _compose_command_index(arguments)
    if command_index is None or arguments[command_index] != "ps":
        return True
    output_options = {"-q", "--quiet", "--services", "-h", "--help", "--dry-run"}
    for argument in arguments[command_index + 1 :]:
        if argument in output_options or argument == "--format":
            return True
        if argument.startswith("--format="):
            return True
    return False


def _ps_json_arguments(arguments: Sequence[str]) -> list[str]:
    command_index = _compose_command_index(arguments)
    if command_index is None or arguments[command_index] != "ps":
        raise WcoError("Cannot locate the Docker Compose ps command.")
    return [
        *arguments[: command_index + 1],
        "--format",
        "json",
        *arguments[command_index + 1 :],
    ]


def _parse_ps_json(content: str) -> list[dict[str, object]]:
    content = content.strip()
    if not content:
        return []
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        return [decoded]
    if isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
        return list(decoded)

    rows: list[dict[str, object]] = []
    try:
        for line in content.splitlines():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("a row is not a JSON object")
            rows.append(item)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WcoError(f"Docker Compose returned invalid ps JSON: {exc}") from exc
    return rows


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration(value: datetime | None, now: datetime) -> str:
    if value is None:
        return ""
    seconds = max(0, int((now - value).total_seconds()))
    units = (
        (365 * 24 * 60 * 60, "year"),
        (30 * 24 * 60 * 60, "month"),
        (7 * 24 * 60 * 60, "week"),
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
        (1, "second"),
    )
    for size, name in units:
        if seconds >= size:
            count = seconds // size
            suffix = "" if count == 1 else "s"
            return f"{count} {name}{suffix}"
    return "less than a second"


def _container_status(
    item: Mapping[str, object],
    inspection: Mapping[str, object],
    now: datetime,
) -> tuple[str, str, str]:
    inspect_state = inspection.get("State", {})
    if not isinstance(inspect_state, dict):
        inspect_state = {}
    state = str(item.get("State") or inspect_state.get("Status") or "unknown").lower()
    inspect_health = inspect_state.get("Health", {})
    if not isinstance(inspect_health, dict):
        inspect_health = {}
    health = str(item.get("Health") or inspect_health.get("Status") or "").lower()
    try:
        exit_code = int(item.get("ExitCode", inspect_state.get("ExitCode", 0)))
    except (TypeError, ValueError):
        exit_code = 0

    if state == "running":
        age = _duration(_parse_datetime(inspect_state.get("StartedAt")), now)
        status = f"Up {age}" if age else "Up"
        if health:
            status += f" ({health})"
    elif state == "exited":
        age = _duration(_parse_datetime(inspect_state.get("FinishedAt")), now)
        status = f"Exited ({exit_code})"
        if age:
            status += f" {age} ago"
    else:
        status = state.title()
        if health:
            status += f" ({health})"
    return status, state, health


def _container_worktree(inspection: Mapping[str, object]) -> str:
    working_dir = _container_label(
        inspection, "com.docker.compose.project.working_dir"
    )
    if working_dir:
        return working_dir
    config_files = _container_label(
        inspection, "com.docker.compose.project.config_files"
    )
    first = config_files.split(",")[0].strip()
    if first:
        parent = str(PurePath(first).parent)
        if parent not in {"", "."}:
            return parent
    return "-"


def _worktree_branch(
    worktree: str,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    if worktree == "-" or not Path(worktree).is_dir():
        return "-"
    try:
        result = run_process(
            ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "-"
    if result.returncode != 0:
        return "-"
    branch = result.stdout.strip()
    if branch and branch != "HEAD":
        return branch
    try:
        detached = run_process(
            ["git", "-C", worktree, "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "-"
    revision = detached.stdout.strip() if detached.returncode == 0 else ""
    return f"({revision})" if revision else "-"


def _worktree_branches(
    rows: Sequence[PsRow],
    run_process: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, str]:
    branches: dict[str, str] = {}
    for row in rows:
        if row.worktree not in branches:
            branches[row.worktree] = _worktree_branch(row.worktree, run_process)
    return branches


def _ps_rows(
    items: Sequence[Mapping[str, object]],
    inspections: Mapping[str, Mapping[str, object]],
    now: datetime,
    stack_id: int | None = None,
) -> list[PsRow]:
    ordered: list[tuple[tuple[str, int, str], PsRow]] = []
    for item in items:
        container_id = str(item.get("ID") or "")
        inspection = inspections.get(container_id, {})
        status, state, health = _container_status(item, inspection, now)
        name = str(item.get("Name") or container_id[:12] or "-")
        service = str(item.get("Service") or "") or _container_label(
            inspection, "com.docker.compose.service"
        )
        row = PsRow(
            name=name,
            status=status,
            state=state,
            health=health,
            worktree=_container_worktree(inspection),
        )
        ordered.append(
            (_container_sort_key(service, _container_number(inspection), name), row)
        )
    ordered.sort(key=lambda entry: entry[0])
    return [
        replace(row, id="-" if stack_id is None else f"{stack_id}.{position}")
        for position, (_key, row) in enumerate(ordered, start=1)
    ]


def _inspect_containers(
    ids: Sequence[str],
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    output: Output,
) -> dict[str, Mapping[str, object]]:
    if not ids:
        return {}
    result = run_process(
        ["docker", "inspect", "--type", "container", *ids],
        check=False,
        capture_output=True,
        text=True,
    )
    inspections: list[object] = []
    if result.stdout.strip():
        try:
            decoded = json.loads(result.stdout)
            if isinstance(decoded, list):
                inspections = decoded
        except json.JSONDecodeError:
            pass
    if result.returncode != 0:
        detail = result.stderr.strip() or "one or more containers disappeared"
        output.warning(f"Could not enrich every container: {detail}")
    indexed: dict[str, Mapping[str, object]] = {}
    for item in inspections:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), str):
            continue
        full_id = str(item["Id"])
        indexed[full_id] = item
        for requested_id in ids:
            if full_id.startswith(requested_id):
                indexed[requested_id] = item
    return indexed


def _print_context(
    output: Output,
    invocation: Invocation,
    override: IsolationOverride | None,
    worktree: Path | None,
) -> None:
    output.context(
        worktree,
        invocation.project_name,
        invocation.isolated,
        invocation.slot,
        len(override.names) if override is not None else 0,
        override.rewritten_ports if override is not None else 0,
        stack_id=invocation.stack_id,
        current_worktree=invocation.worktree,
    )


def _run_pretty_ps(
    invocation: Invocation,
    override: IsolationOverride | None,
    output: Output,
    stdout: TextIO,
    stderr: TextIO,
    run_process: Callable[..., subprocess.CompletedProcess[str]],
    now: datetime,
) -> int:
    json_arguments = _ps_json_arguments(invocation.compose_args)
    json_command = build_invocation_command(
        invocation,
        override,
        json_arguments,
    )
    result = run_process(
        json_command,
        check=False,
        capture_output=True,
        text=True,
        env=invocation.environment,
    )
    if result.returncode != 0:
        _print_context(output, invocation, override, invocation.worktree)
        if result.stderr:
            stderr.write(result.stderr)
            stderr.flush()
        if result.stdout:
            stdout.write(result.stdout)
            stdout.flush()
        return result.returncode

    items = _parse_ps_json(result.stdout)
    ids = [str(item.get("ID")) for item in items if item.get("ID")]
    inspections = _inspect_containers(ids, run_process, output)
    rows = _ps_rows(items, inspections, now, invocation.stack_id)
    branches = _worktree_branches(rows, run_process)
    rows = [replace(row, branch=branches[row.worktree]) for row in rows]
    _print_context(output, invocation, override, None)
    if result.stderr:
        stderr.write(result.stderr)
        stderr.flush()
    output.ps(
        rows,
        no_trunc="--no-trunc" in invocation.compose_args,
        current_worktree=invocation.worktree,
    )
    return 0


INIT_HELP = """Usage: wco init [OPTIONS]

Create .wco.toml in the current directory.

Options:
  --compose PATH   Use this Compose file instead of automatic detection.
  --project NAME   Use this Compose project name instead of the directory name.
  --force          Replace an existing .wco.toml.
  -h, --help       Show this help.
"""


STACKS_HELP = """Usage: wco stacks [-a|--all] [--format table|json]

List every container this workspace owns, in the shared project and in each
worktree's isolated project, with the worktree and branch it runs from.

The ID column shows each container's '<stack>.<container>' address. Stack 1 is
always the shared project; isolated worktrees are numbered from 2 and keep
their ID until the worktree is removed. Pass an ID to a Compose command to
target that stack or container, e.g. 'wco down 2' or 'wco restart 2.1'.

Options:
  -a, --all        Include stopped containers.
  --format FORMAT  Select table or JSON output.
  -h, --help       Show this help.
"""


HELP = """Usage: wco [--isolated] <command> [ID] [docker compose arguments]
       wco init [--compose PATH] [--project NAME] [--force]
       wco ports <show|reallocate> [--all] [--format table|json]
       wco stacks [--all] [--format table|json]

Run a workspace's central Docker Compose configuration against the current
Git worktree. The nearest .wco.toml at or above the worktree defines the
Compose files, environment, validation rules, and isolated ports.

An optional ID directly after the Compose command targets another stack
without changing directory. 'wco stacks' lists the IDs: stack 1 is the shared
project, isolated worktrees are numbered from 2, and '2.1' addresses a single
container inside stack 2. Because only that one position is read as an ID,
put it before any flags: 'wco logs 2.1 -f', not 'wco logs -f 2.1'.

Options:
  --isolated   Use a worktree-specific project name and persistent port slot.
  --format     Select table or JSON output for wco ports and wco stacks.
  --all        Bring down every worktree's isolated stack
               (wco --isolated down --all), list every worktree's port slot
               (wco ports show), or include stopped containers (wco stacks).
  -h, --help   Show this help.
  --version    Show the installed wco version.

Examples:
  wco init
  wco up -d --build --force-recreate
  wco --isolated up -d
  wco --isolated down
  wco --isolated down --all
  wco down 2
  wco restart 2.1
  wco logs 2.1 -f
  wco ports show
  wco ports show --all
  wco stacks --all
"""


def run(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    exec_fn: Callable[[str, Sequence[str], Mapping[str, str]], object] = os.execvpe,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = Output(stdout, stderr)
    try:
        if not arguments:
            output.help(HELP, error=True)
            return 2
        if arguments in (["-h"], ["--help"]):
            output.help(HELP)
            return 0
        if arguments == ["--version"]:
            output.version(__version__)
            return 0
        if arguments[0] == "init":
            return _init_command(arguments, cwd, output)
        if arguments[0] == "ports":
            return _port_command(arguments, cwd, output, stdout, run_process)
        if arguments[0] == "stacks":
            return _stacks_command(
                arguments,
                cwd,
                output,
                stdout,
                run_process,
                now_fn() if now_fn is not None else datetime.now(timezone.utc),
            )

        isolated = False
        if arguments and arguments[0] == "--isolated":
            isolated = True
            arguments.pop(0)
        if not arguments:
            raise WcoError("Docker Compose arguments are required.")
        if isolated and _is_down_all(arguments):
            return _isolated_down_all(arguments, cwd, output, run_process)

        arguments, target = split_target(arguments)
        if target is not None:
            worktree, isolated, extra = _resolve_target(
                target,
                compose_command(arguments),
                isolated,
                cwd,
                output,
                run_process,
            )
            cwd = worktree
            arguments = _insert_after_command(arguments, extra)

        invocation = prepare_invocation(arguments, isolated, cwd)
        override = prepare_isolation_override(invocation, run_process)
        is_startup = (
            isolated
            and invocation.compose_command
            in invocation.config.validation.startup_commands
        )
        if is_startup:
            _validate_isolated_start(invocation, override, run_process)
        command = build_invocation_command(invocation, override)
        uses_pretty_ps = not _ps_uses_native_output(invocation.compose_args, output)
        if not uses_pretty_ps:
            _print_context(output, invocation, override, invocation.worktree)
        if is_startup and override is not None:
            if override.names:
                output.warning(
                    f"Rewriting {len(override.names)} fixed container name(s) for "
                    "isolated mode; exact-name-dependent tooling may need adjustment."
                )
            if override.rewritten_ports:
                output.warning(
                    f"Rotating {override.rewritten_ports} fixed published host "
                    "port(s) for isolated mode."
                )
        if uses_pretty_ps:
            current_time = (
                now_fn() if now_fn is not None else datetime.now(timezone.utc)
            )
            return _run_pretty_ps(
                invocation,
                override,
                output,
                stdout,
                stderr,
                run_process,
                current_time,
            )
        exec_fn(command[0], command, invocation.environment)
        return 0
    except WcoError as exc:
        output.error(str(exc))
        return 1


def main() -> None:
    raise SystemExit(run())
