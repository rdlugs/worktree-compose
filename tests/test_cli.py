from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wco.cli import (
    WcoError,
    PortStore,
    _validate_isolated_start,
    compose_command,
    legacy_state_file,
    load_config,
    migrate_legacy_state,
    prepare_invocation,
    run,
    state_file,
)


CONFIG = """\
version = 1

[compose]
files = ["docker-compose.yml"]
project_name = "example"
instance_name = "example-container"

[environment]
SOURCE_PATH = "{worktree}"
PREFIX = "{instance}"

[validation]
required = ["project.marker"]
startup_required = [".env"]

[isolation]
port_step = 100
max_slots = 5

[isolation.ports]
HTTP_PORT = 8000
DEV_PORT = 5000
"""


class WorkspaceFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".wco.toml").write_text(CONFIG)
        (self.workspace / "docker-compose.yml").write_text("services: {}\n")
        self.main = self.create_repo("main")

    def create_repo(self, name: str) -> Path:
        repository = self.workspace / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "project.marker").write_text("ok\n")
        return repository

    def close(self) -> None:
        self.temporary.cleanup()


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "My Workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = run(arguments, cwd=self.workspace, stdout=stdout, stderr=stderr)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_init_detects_compose_file_and_derives_project_name(self) -> None:
        (self.workspace / "docker-compose.yml").write_text("services: {}\n")

        result, stdout, stderr = self.invoke(["init"])

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Project: my-workspace", stdout)
        config = load_config(self.workspace / ".wco.toml")
        self.assertEqual(config.project_name, "my-workspace")
        self.assertEqual(config.compose_files, (self.workspace / "docker-compose.yml",))
        self.assertEqual(config.environment, {"SOURCE_PATH": "{worktree}"})

    def test_init_prefers_modern_compose_filename(self) -> None:
        (self.workspace / "compose.yaml").write_text("services: {}\n")
        (self.workspace / "docker-compose.yml").write_text("services: {}\n")

        result, _, _ = self.invoke(["init"])

        self.assertEqual(result, 0)
        config = load_config(self.workspace / ".wco.toml")
        self.assertEqual(config.compose_files, (self.workspace / "compose.yaml",))

    def test_init_accepts_custom_compose_file_and_project(self) -> None:
        deploy = self.workspace / "deploy"
        deploy.mkdir()
        (deploy / "stack.yml").write_text("services: {}\n")

        result, _, _ = self.invoke(
            ["init", "--compose", "deploy/stack.yml", "--project", "custom_app"]
        )

        self.assertEqual(result, 0)
        config = load_config(self.workspace / ".wco.toml")
        self.assertEqual(config.project_name, "custom_app")
        self.assertEqual(config.compose_files, (deploy / "stack.yml",))

    def test_init_requires_force_to_replace_configuration(self) -> None:
        (self.workspace / "docker-compose.yml").write_text("services: {}\n")
        first_result, _, _ = self.invoke(["init"])
        original = (self.workspace / ".wco.toml").read_text()

        second_result, _, second_error = self.invoke(["init", "--project", "replacement"])
        self.assertEqual(first_result, 0)
        self.assertEqual(second_result, 1)
        self.assertIn("--force", second_error)
        self.assertEqual((self.workspace / ".wco.toml").read_text(), original)

        forced_result, _, _ = self.invoke(
            ["init", "--project", "replacement", "--force"]
        )
        self.assertEqual(forced_result, 0)
        self.assertEqual(
            load_config(self.workspace / ".wco.toml").project_name,
            "replacement",
        )

    def test_init_fails_cleanly_without_compose_file(self) -> None:
        result, _, stderr = self.invoke(["init"])
        self.assertEqual(result, 1)
        self.assertIn("No Compose file was found", stderr)
        self.assertFalse((self.workspace / ".wco.toml").exists())

    def test_init_help_does_not_create_configuration(self) -> None:
        result, stdout, stderr = self.invoke(["init", "--help"])
        self.assertEqual(result, 0)
        self.assertIn("Usage: wco init", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse((self.workspace / ".wco.toml").exists())


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_loads_generic_configuration(self) -> None:
        config = load_config(self.fixture.workspace / ".wco.toml")
        self.assertEqual(config.project_name, "example")
        self.assertEqual(config.environment["SOURCE_PATH"], "{worktree}")
        self.assertEqual(config.isolation.ports["HTTP_PORT"], 8000)

    def test_rejects_compose_path_outside_workspace(self) -> None:
        config_path = self.fixture.workspace / ".wco.toml"
        config_path.write_text(CONFIG.replace('["docker-compose.yml"]', '["../outside.yml"]'))
        with self.assertRaisesRegex(WcoError, "escapes the workspace"):
            load_config(config_path)

    def test_discovers_worktree_from_nested_directory(self) -> None:
        nested = self.fixture.main / "a" / "b"
        nested.mkdir(parents=True)
        invocation = prepare_invocation(["config"], False, nested)
        self.assertEqual(invocation.worktree, self.fixture.main.resolve())
        self.assertEqual(invocation.environment["SOURCE_PATH"], str(self.fixture.main.resolve()))
        self.assertEqual(invocation.ports["HTTP_PORT"], 8000)

    def test_startup_requires_env_but_config_does_not(self) -> None:
        prepare_invocation(["config"], False, self.fixture.main)
        with self.assertRaisesRegex(WcoError, r"\.env"):
            prepare_invocation(["up", "-d"], False, self.fixture.main)

    def test_project_name_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(WcoError, "managed by wco"):
            prepare_invocation(["--project-name", "wrong", "config"], False, self.fixture.main)

    def test_legacy_configuration_name_is_not_discovered(self) -> None:
        current = self.fixture.workspace / ".wco.toml"
        current.rename(self.fixture.workspace / ".bcompose.toml")
        with self.assertRaisesRegex(WcoError, r"No \.wco\.toml"):
            prepare_invocation(["config"], False, self.fixture.main)


class PortStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.state = self.fixture.root / "state" / "ports.json"
        self.store = PortStore(self.state, availability=lambda _port: True)
        self.config = load_config(self.fixture.workspace / ".wco.toml")

    def tearDown(self) -> None:
        self.fixture.close()

    def test_assignments_are_stable_and_distinct(self) -> None:
        first = self.store.get_or_allocate(self.config, self.fixture.main)
        repeated = self.store.get_or_allocate(self.config, self.fixture.main)
        feature = self.fixture.create_repo("feature")
        second = self.store.get_or_allocate(self.config, feature)

        self.assertEqual(first, repeated)
        self.assertEqual(first, (1, {"HTTP_PORT": 8100, "DEV_PORT": 5100}))
        self.assertEqual(second, (2, {"HTTP_PORT": 8200, "DEV_PORT": 5200}))

    def test_reallocate_moves_to_a_new_slot(self) -> None:
        self.store.get_or_allocate(self.config, self.fixture.main)
        replacement = self.store.reallocate(self.config, self.fixture.main)
        self.assertEqual(replacement, (2, {"HTTP_PORT": 8200, "DEV_PORT": 5200}))


class StateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.environment = {"XDG_STATE_HOME": str(self.fixture.root / "xdg-state")}
        self.legacy = legacy_state_file(self.environment)
        self.current = state_file(self.environment)

    def tearDown(self) -> None:
        self.fixture.close()

    def _write_state(self, path: Path, config_path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "assignments": [
                        {
                            "config": str(config_path),
                            "worktree": str(self.fixture.main.resolve()),
                            "slot": 1,
                            "ports": {"HTTP_PORT": 8100, "DEV_PORT": 5100},
                        }
                    ],
                }
            )
        )

    def test_moves_legacy_state_and_preserves_assignment(self) -> None:
        old_config = self.fixture.workspace / ".bcompose.toml"
        self._write_state(self.legacy, old_config)

        destination = migrate_legacy_state(self.environment)

        self.assertEqual(destination, self.current)
        self.assertFalse(self.legacy.exists())
        self.assertFalse(self.legacy.parent.exists())
        migrated = json.loads(self.current.read_text())
        self.assertEqual(
            migrated["assignments"][0]["config"],
            str((self.fixture.workspace / ".wco.toml").resolve()),
        )
        assignment = PortStore(self.current, lambda _port: True).get(
            load_config(self.fixture.workspace / ".wco.toml"),
            self.fixture.main.resolve(),
        )
        self.assertEqual(assignment, (1, {"HTTP_PORT": 8100, "DEV_PORT": 5100}))

    def test_refuses_to_overwrite_existing_current_state(self) -> None:
        self._write_state(self.legacy, self.fixture.workspace / ".bcompose.toml")
        self._write_state(self.current, self.fixture.workspace / ".wco.toml")
        legacy_before = self.legacy.read_text()
        current_before = self.current.read_text()

        with self.assertRaisesRegex(WcoError, "Both legacy and current"):
            migrate_legacy_state(self.environment)

        self.assertEqual(self.legacy.read_text(), legacy_before)
        self.assertEqual(self.current.read_text(), current_before)


class InvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_compose_command_skips_global_option_values(self) -> None:
        self.assertEqual(compose_command(["--env-file", "up", "config"]), "config")
        self.assertEqual(compose_command(["--ansi=never", "up", "-d"]), "up")

    def test_run_forwards_arguments_and_environment(self) -> None:
        captured: dict[str, object] = {}

        def capture(file: str, args: object, environment: object) -> None:
            captured.update(file=file, args=args, environment=environment)

        stdout = io.StringIO()
        stderr = io.StringIO()
        result = run(
            ["config", "--services"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=stderr,
            exec_fn=capture,
        )

        self.assertEqual(result, 0)
        self.assertEqual(captured["file"], "docker")
        self.assertEqual(captured["args"][-2:], ["config", "--services"])
        environment = captured["environment"]
        self.assertEqual(environment["SOURCE_PATH"], str(self.fixture.main.resolve()))
        self.assertIn("Using worktree:", stderr.getvalue())

    def test_isolated_invocations_use_stable_unique_names_and_ports(self) -> None:
        state = self.fixture.root / "state" / "ports.json"
        store = PortStore(state, availability=lambda _port: True)
        first = prepare_invocation(["config"], True, self.fixture.main, store)
        repeated = prepare_invocation(["config"], True, self.fixture.main, store)

        self.assertEqual(first.project_name, repeated.project_name)
        self.assertEqual(first.ports, repeated.ports)
        self.assertIn("main-", first.project_name)
        self.assertEqual(first.environment["PREFIX"], first.instance_name)

    def test_isolated_start_rejects_fixed_container_names(self) -> None:
        (self.fixture.main / ".env").write_text("APP_ENV=test\n")
        store = PortStore(self.fixture.root / "state" / "ports.json", lambda _port: True)
        invocation = prepare_invocation(["up", "-d"], True, self.fixture.main, store)
        rendered = '{"services":{"web":{"container_name":"fixed-web","ports":[]}}}'
        completed = subprocess.CompletedProcess([], 0, rendered, "")

        with (
            patch("wco.cli._published_ports", return_value=set()),
            patch("wco.cli.port_is_available", return_value=True),
            patch("wco.cli.subprocess.run", return_value=completed),
            self.assertRaisesRegex(WcoError, "fixed container name"),
        ):
            _validate_isolated_start(invocation)

    def test_isolated_start_rejects_unregistered_published_ports(self) -> None:
        (self.fixture.main / ".env").write_text("APP_ENV=test\n")
        store = PortStore(self.fixture.root / "state" / "ports.json", lambda _port: True)
        invocation = prepare_invocation(["up", "-d"], True, self.fixture.main, store)
        rendered = (
            '{"services":{"web":{"container_name":"'
            + invocation.instance_name
            + '-web","ports":[{"published":"9999","target":80}]}}}'
        )
        completed = subprocess.CompletedProcess([], 0, rendered, "")

        with (
            patch("wco.cli._published_ports", return_value=set()),
            patch("wco.cli.port_is_available", return_value=True),
            patch("wco.cli.subprocess.run", return_value=completed),
            self.assertRaisesRegex(WcoError, r"not declared in \[isolation\.ports\]"),
        ):
            _validate_isolated_start(invocation)


if __name__ == "__main__":
    unittest.main()
