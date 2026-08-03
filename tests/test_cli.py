from __future__ import annotations

import io
import json
import multiprocessing
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wco.cli import (
    SHARED_STACK_ID,
    STATE_VERSION,
    Target,
    WcoError,
    PortStore,
    _isolated_name,
    _validate_isolated_start,
    build_invocation_command,
    compose_command,
    legacy_state_file,
    load_config,
    migrate_legacy_state,
    override_directory,
    prepare_isolation_override,
    prepare_invocation,
    run,
    split_target,
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


class TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _allocate_port_worker(
    state_path: str,
    config_path: str,
    worktree: str,
    start_event,
    result_queue,
) -> None:
    try:
        if not start_event.wait(timeout=20):
            raise RuntimeError("Timed out waiting to start concurrent allocation.")
        store = PortStore(Path(state_path), availability=lambda _port: True)
        config = load_config(Path(config_path))
        slot, ports = store.get_or_allocate(config, Path(worktree))
        result_queue.put(("ok", slot, ports))
    except Exception as exc:
        result_queue.put(("error", repr(exc), {}))


def wide_terminal(fixture: "WorkspaceFixture"):
    """A terminal wide enough that no column is ellipsized.

    Temporary directories are far longer on some platforms than on others
    (macOS resolves them under /private/var/folders/...), so a fixed COLUMNS
    truncates the WORKTREE column there and not on Linux. Size it to the paths
    the fixture actually produces instead.
    """
    width = len(str(fixture.workspace.resolve())) + 140
    return patch.dict(os.environ, {"COLUMNS": str(width)})


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
        self.workspace = (Path(self.temporary.name) / "My Workspace").resolve()
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
        self.assertIn("Workspace initialized", stdout)
        self.assertIn("my-workspace", stdout)
        self.assertNotIn("\x1b", stdout)
        config = load_config(self.workspace / ".wco.toml")
        self.assertEqual(config.project_name, "my-workspace")
        self.assertEqual(config.compose_files, (self.workspace / "docker-compose.yml",))
        self.assertEqual(config.environment, {})
        generated = (self.workspace / ".wco.toml").read_text()
        self.assertNotIn("[environment]", generated)
        self.assertNotIn("SOURCE_PATH", generated)
        self.assertNotIn("rewrite_container_names", generated)
        self.assertNotIn("rewrite_ports", generated)
        self.assertIn("worktree-relative Compose paths", stdout)
        self.assertNotIn("SOURCE_PATH", stdout)

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
        self.assertFalse(config.isolation.rewrite_container_names)
        self.assertFalse(config.isolation.rewrite_ports)

    def test_loads_container_name_rewrite_opt_in(self) -> None:
        path = self.fixture.workspace / ".wco.toml"
        path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                "[isolation]\nrewrite_container_names = true\n",
            )
        )

        config = load_config(path)

        self.assertTrue(config.isolation.rewrite_container_names)

    def test_rejects_non_boolean_container_name_rewrite_setting(self) -> None:
        path = self.fixture.workspace / ".wco.toml"
        path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                '[isolation]\nrewrite_container_names = "yes"\n',
            )
        )

        with self.assertRaisesRegex(WcoError, "must be a boolean"):
            load_config(path)

    def test_loads_fixed_port_rewrite_opt_in(self) -> None:
        path = self.fixture.workspace / ".wco.toml"
        path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                "[isolation]\nrewrite_ports = true\n",
            )
        )

        config = load_config(path)

        self.assertTrue(config.isolation.rewrite_ports)

    def test_rejects_non_boolean_fixed_port_rewrite_setting(self) -> None:
        path = self.fixture.workspace / ".wco.toml"
        path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                '[isolation]\nrewrite_ports = "yes"\n',
            )
        )

        with self.assertRaisesRegex(WcoError, "must be a boolean"):
            load_config(path)

    def test_rejects_duplicate_isolation_base_ports(self) -> None:
        path = self.fixture.workspace / ".wco.toml"
        path.write_text(CONFIG.replace("DEV_PORT = 5000", "DEV_PORT = 8000"))

        with self.assertRaisesRegex(WcoError, "must be unique"):
            load_config(path)

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

    def test_existing_slot_learns_new_configured_ports(self) -> None:
        self.store.get_or_allocate(self.config, self.fixture.main)
        self.config.path.write_text(
            self.config.path.read_text().replace(
                "DEV_PORT = 5000",
                "DEV_PORT = 5000\nREDIS_PORT = 6379",
            )
        )
        updated = load_config(self.config.path)

        assignment = self.store.get_or_allocate(updated, self.fixture.main)

        self.assertEqual(
            assignment,
            (
                1,
                {"HTTP_PORT": 8100, "DEV_PORT": 5100, "REDIS_PORT": 6479},
            ),
        )

    def test_existing_slot_reports_unavailable_new_ports(self) -> None:
        store = PortStore(self.state, availability=lambda port: port != 6479)
        store.get_or_allocate(self.config, self.fixture.main)
        self.config.path.write_text(
            self.config.path.read_text().replace(
                "DEV_PORT = 5000",
                "DEV_PORT = 5000\nREDIS_PORT = 6379",
            )
        )
        updated = load_config(self.config.path)

        with self.assertRaisesRegex(WcoError, "6479"):
            store.get_or_allocate(updated, self.fixture.main)

    def test_lock_errors_are_reported_as_wco_errors(self) -> None:
        with patch("wco.cli.FileLock") as lock_class:
            lock_class.return_value.acquire.side_effect = OSError("access denied")
            with self.assertRaisesRegex(WcoError, "Cannot lock port state"):
                self.store.get(self.config, self.fixture.main)

    def test_concurrent_processes_allocate_distinct_slots(self) -> None:
        feature = self.fixture.create_repo("feature")
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_allocate_port_worker,
                args=(
                    str(self.state),
                    str(self.config.path),
                    str(worktree),
                    start_event,
                    result_queue,
                ),
            )
            for worktree in (self.fixture.main, feature)
        ]

        for process in processes:
            process.start()
        start_event.set()
        try:
            results = [result_queue.get(timeout=30) for _process in processes]
        finally:
            for process in processes:
                process.join(timeout=30)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual({result[0] for result in results}, {"ok"})
        self.assertEqual({result[1] for result in results}, {1, 2})
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["assignments"]), 2)
        self.assertEqual(
            {assignment["slot"] for assignment in state["assignments"]},
            {1, 2},
        )


class PortOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        self.config = load_config(self.fixture.workspace / ".wco.toml")

    def tearDown(self) -> None:
        self.fixture.close()

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("wco.cli.default_port_store", return_value=self.store):
            result = run(
                arguments,
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_show_renders_shared_and_unallocated_table_rows(self) -> None:
        result, stdout, stderr = self.invoke(["ports", "show"])

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Port assignments", stdout)
        self.assertIn("MODE", stdout)
        self.assertIn("HTTP_PORT", stdout)
        self.assertIn("not allocated", stdout)
        self.assertNotIn("\x1b", stdout)

    def test_show_json_has_stable_shared_and_isolated_shape(self) -> None:
        slot, ports = self.store.get_or_allocate(self.config, self.fixture.main.resolve())

        result, stdout, stderr = self.invoke(
            ["ports", "show", "--format=json"]
        )

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.endswith("\n"))
        self.assertNotIn("\x1b", stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["worktree"], str(self.fixture.main.resolve()))
        self.assertEqual(payload["shared"]["ports"]["HTTP_PORT"], 8000)
        self.assertEqual(payload["isolated"]["slot"], slot)
        self.assertEqual(payload["isolated"]["ports"], ports)
        self.assertTrue(payload["isolated"]["allocated"])

    def test_reallocate_json_reports_new_assignment(self) -> None:
        self.store.get_or_allocate(self.config, self.fixture.main.resolve())

        with patch("wco.cli._docker_container_ids", return_value=[]):
            result, stdout, stderr = self.invoke(
                ["ports", "reallocate", "--format", "json"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["isolated"]["slot"], 2)
        self.assertEqual(payload["isolated"]["ports"]["HTTP_PORT"], 8200)

    def _git_run(self, branches: dict[Path, str]):
        def fake_run(command: list[str], **_kwargs):
            if command[0] != "git":
                raise AssertionError(f"unexpected command: {command}")
            branch = branches.get(Path(command[2]), "main")
            return subprocess.CompletedProcess(command, 0, f"{branch}\n", "")

        return fake_run

    def test_show_all_lists_every_worktree_slot(self) -> None:
        feature = self.fixture.create_repo("feature")
        main_slot, main_ports = self.store.get_or_allocate(
            self.config, self.fixture.main.resolve()
        )
        feature_slot, feature_ports = self.store.get_or_allocate(
            self.config, feature.resolve()
        )
        stdout = io.StringIO()
        branches = {
            self.fixture.main.resolve(): "master",
            feature.resolve(): "feature/login",
        }

        with (
            patch("wco.cli.default_port_store", return_value=self.store),
            wide_terminal(self.fixture),
        ):
            result = run(
                ["ports", "show", "--all"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=io.StringIO(),
                run_process=self._git_run(branches),
            )

        rendered = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("all worktrees", rendered)
        self.assertIn("WORKTREE", rendered)
        self.assertIn("BRANCH", rendered)
        self.assertIn(str(self.fixture.main.resolve()), rendered)
        self.assertIn(str(feature.resolve()), rendered)
        self.assertIn("master", rendered)
        self.assertIn("feature/login", rendered)
        self.assertIn(str(main_ports["HTTP_PORT"]), rendered)
        self.assertIn(str(feature_ports["HTTP_PORT"]), rendered)
        self.assertNotEqual(main_slot, feature_slot)

    def test_show_all_json_lists_assignments_and_marks_drift(self) -> None:
        feature = self.fixture.create_repo("feature")
        self.store.get_or_allocate(self.config, self.fixture.main.resolve())
        self.store.get_or_allocate(self.config, feature.resolve())
        state = json.loads(self.store.path.read_text())
        for item in state["assignments"]:
            if item["worktree"] == str(feature.resolve()):
                item["ports"] = {"LEGACY_PORT": 9999}
        self.store.path.write_text(json.dumps(state))
        stdout = io.StringIO()

        with patch("wco.cli.default_port_store", return_value=self.store):
            result = run(
                ["ports", "show", "--all", "--format", "json"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=io.StringIO(),
                run_process=self._git_run({}),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["shared"]["ports"]["HTTP_PORT"], 8000)
        entries = {item["worktree"]: item for item in payload["isolated"]}
        self.assertEqual(set(entries), {str(self.fixture.main.resolve()), str(feature.resolve())})
        self.assertEqual(entries[str(self.fixture.main.resolve())]["status"], "allocated")
        self.assertEqual(entries[str(feature.resolve())]["status"], "outdated")
        self.assertEqual(entries[str(feature.resolve())]["ports"], {"LEGACY_PORT": 9999})

    def test_show_all_reports_an_empty_state(self) -> None:
        stdout = io.StringIO()

        with patch("wco.cli.default_port_store", return_value=self.store):
            result = run(
                ["ports", "show", "--all"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=io.StringIO(),
                run_process=self._git_run({}),
            )

        self.assertEqual(result, 0)
        self.assertIn("No worktree has an isolated port slot yet", stdout.getvalue())

    def test_show_all_ignores_other_workspaces_and_rejects_reallocate(self) -> None:
        other_workspace = self.fixture.root / "other"
        other_workspace.mkdir()
        (other_workspace / ".wco.toml").write_text(CONFIG)
        (other_workspace / "docker-compose.yml").write_text("services: {}\n")
        other_config = load_config(other_workspace / ".wco.toml")
        self.store.get_or_allocate(other_config, self.fixture.main.resolve())
        self.store.get_or_allocate(self.config, self.fixture.main.resolve())
        stdout = io.StringIO()

        with patch("wco.cli.default_port_store", return_value=self.store):
            result = run(
                ["ports", "show", "--all", "--format", "json"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=io.StringIO(),
                run_process=self._git_run({}),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(payload["isolated"]), 1)

        result, _, stderr = self.invoke(["ports", "reallocate", "--all"])
        self.assertEqual(result, 1)
        self.assertIn("only valid for 'wco ports show'", stderr)

    def test_rejects_unknown_or_duplicate_formats(self) -> None:
        result, _, stderr = self.invoke(["ports", "show", "--format", "yaml"])
        self.assertEqual(result, 1)
        self.assertIn("table", stderr)
        self.assertIn("json", stderr)

        result, _, stderr = self.invoke(
            ["ports", "show", "--format", "json", "--format=table"]
        )
        self.assertEqual(result, 1)
        self.assertIn("only be specified once", stderr)


class StatePathTests(unittest.TestCase):
    def test_xdg_state_home_takes_precedence_on_windows(self) -> None:
        with patch("wco.cli.sys.platform", "win32"):
            current = state_file(
                {"XDG_STATE_HOME": "/custom/state", "LOCALAPPDATA": "/windows/state"}
            )
            legacy = legacy_state_file(
                {"XDG_STATE_HOME": "/custom/state", "LOCALAPPDATA": "/windows/state"}
            )

        self.assertEqual(current, Path("/custom/state/wco/ports.json"))
        self.assertEqual(legacy, Path("/custom/state/bcompose/ports.json"))
        self.assertEqual(
            override_directory({"XDG_STATE_HOME": "/custom/state"}),
            Path("/custom/state/wco/overrides"),
        )

    def test_windows_uses_local_app_data_by_default(self) -> None:
        local_app_data = Path("C:/Users/example/AppData/Local")
        with patch("wco.cli.sys.platform", "win32"):
            current = state_file({"LOCALAPPDATA": str(local_app_data)})
            legacy = legacy_state_file({"LOCALAPPDATA": str(local_app_data)})

        self.assertEqual(current, local_app_data / "wco" / "ports.json")
        self.assertEqual(legacy, local_app_data / "bcompose" / "ports.json")
        with patch("wco.cli.sys.platform", "win32"):
            self.assertEqual(
                override_directory({"LOCALAPPDATA": str(local_app_data)}),
                local_app_data / "wco" / "overrides",
            )


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


class IsolationOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.config_path = self.fixture.workspace / ".wco.toml"
        self.config_path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                "[isolation]\nrewrite_container_names = true\n",
            )
        )
        (self.fixture.main / ".env").write_text("APP_ENV=test\n")
        self.store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        self.override_root = self.fixture.root / "overrides"

    def tearDown(self) -> None:
        self.fixture.close()

    def invocation(self, arguments: list[str] | None = None):
        return prepare_invocation(
            arguments or ["up", "-d"],
            True,
            self.fixture.main,
            self.store,
        )

    def enable_port_rewrite(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text().replace(
                "[isolation]\n",
                "[isolation]\nrewrite_ports = true\n",
            )
        )

    @staticmethod
    def completed(command: list[str], services: dict[str, object]):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"services": services}),
            "",
        )

    def test_generates_minimal_override_and_appends_it_after_user_files(self) -> None:
        compose_file = self.fixture.workspace / "docker-compose.yml"
        original_compose = compose_file.read_text()
        invocation = self.invocation(
            ["--env-file", "custom.env", "--file", "extra.yml", "up", "-d"]
        )
        rendered_commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs):
            rendered_commands.append(command)
            return self.completed(
                command,
                {
                    "web": {"container_name": "fixed-web", "ports": []},
                    "worker": {"container_name": "fixed-worker", "ports": []},
                },
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(
            override.names,
            {
                "web": f"{invocation.instance_name}-fixed-web",
                "worker": f"{invocation.instance_name}-fixed-worker",
            },
        )
        self.assertEqual(compose_file.read_text(), original_compose)
        document = override.path.read_text()
        self.assertIn('"web":', document)
        self.assertIn('container_name: "', document)
        self.assertNotIn("image:", document)
        render_command = rendered_commands[0]
        self.assertEqual(render_command[-5:], ["--profile", "*", "config", "--format", "json"])

        command = build_invocation_command(invocation, override)
        extra_file = command.index("extra.yml")
        override_file = command.index(str(override.path))
        compose_command = command.index("up")
        self.assertLess(extra_file, override_file)
        self.assertLess(override_file, compose_command)

    def test_includes_profile_only_services_and_leaves_safe_names_unchanged(self) -> None:
        invocation = self.invocation(["--profile", "setup", "up", "-d"])

        def fake_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {
                    "setup": {"container_name": "fixed-setup"},
                    "safe": {
                        "container_name": f"{invocation.instance_name}-safe"
                    },
                    "unnamed": {},
                },
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(set(override.names), {"setup"})

    def test_rewrites_declared_fixed_ports_and_preserves_port_attributes(self) -> None:
        self.enable_port_rewrite()
        compose_file = self.fixture.workspace / "docker-compose.yml"
        original_compose = compose_file.read_text()
        invocation = self.invocation()

        def fake_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {
                    "web": {
                        "ports": [
                            {
                                "target": 80,
                                "published": "8000",
                                "protocol": "tcp",
                                "host_ip": "127.0.0.1",
                                "mode": "host",
                            },
                            {"target": 5000, "published": "5000", "protocol": "udp"},
                            {"target": 9000, "protocol": "tcp"},
                        ]
                    }
                },
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        assert override is not None
        self.assertEqual(override.rewritten_ports, 2)
        ports = override.ports["web"]
        self.assertEqual(ports[0]["published"], str(invocation.ports["HTTP_PORT"]))
        self.assertEqual(ports[0]["host_ip"], "127.0.0.1")
        self.assertEqual(ports[0]["mode"], "host")
        self.assertEqual(ports[1]["published"], str(invocation.ports["DEV_PORT"]))
        self.assertEqual(ports[1]["protocol"], "udp")
        self.assertNotIn("published", ports[2])
        document = override.path.read_text()
        self.assertIn("ports: !override", document)
        self.assertIn(f'"published": "{invocation.ports["HTTP_PORT"]}"', document)
        self.assertEqual(compose_file.read_text(), original_compose)

    def test_combines_container_names_and_ports_in_one_override(self) -> None:
        self.enable_port_rewrite()
        invocation = self.invocation()

        def fake_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {
                    "web": {
                        "container_name": "fixed-web",
                        "ports": [{"target": 80, "published": "8000"}],
                    }
                },
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        assert override is not None
        self.assertEqual(set(override.names), {"web"})
        self.assertEqual(set(override.ports), {"web"})
        self.assertEqual(override.rewritten_ports, 1)
        document = override.path.read_text()
        self.assertEqual(document.count('"web":'), 1)
        self.assertIn("container_name:", document)
        self.assertIn("ports: !override", document)

    def test_does_not_override_ports_already_rendered_with_allocated_values(self) -> None:
        self.enable_port_rewrite()
        invocation = self.invocation(["config"])

        def fake_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {
                    "web": {
                        "ports": [
                            {
                                "target": 80,
                                "published": str(invocation.ports["HTTP_PORT"]),
                            }
                        ]
                    }
                },
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        self.assertIsNone(override)

    def test_long_duplicate_names_are_bounded_stable_and_unique(self) -> None:
        invocation = self.invocation()
        original = "fixed-" + "x" * 100
        services = {
            "alpha": {"container_name": original},
            "beta": {"container_name": original},
        }

        def fake_run(command: list[str], **_kwargs):
            return self.completed(command, services)

        first = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )
        second = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        assert first is not None and second is not None
        self.assertEqual(first.names, second.names)
        self.assertEqual(len(set(first.names.values())), 2)
        self.assertTrue(all(len(name) <= 63 for name in first.names.values()))

    def test_different_worktrees_get_different_names_and_state_files(self) -> None:
        feature = self.fixture.create_repo("feature")
        first = self.invocation(["config"])
        second = prepare_invocation(["config"], True, feature, self.store)

        def fake_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {"web": {"container_name": "fixed-web"}},
            )

        first_override = prepare_isolation_override(
            first, fake_run, self.override_root
        )
        second_override = prepare_isolation_override(
            second, fake_run, self.override_root
        )

        assert first_override is not None and second_override is not None
        self.assertNotEqual(first_override.path, second_override.path)
        self.assertNotEqual(
            first_override.names["web"], second_override.names["web"]
        )

    def test_returns_none_when_no_names_need_rewriting(self) -> None:
        invocation = self.invocation(["config"])

        def fake_run(command: list[str], **_kwargs):
            return self.completed(command, {"web": {}})

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.override_root,
        )

        self.assertIsNone(override)
        self.assertFalse(self.override_root.exists())

    def test_rejects_stdin_compose_files_without_consuming_input(self) -> None:
        invocation = self.invocation(["-f-", "up", "-d"])
        called = False

        def fake_run(command: list[str], **_kwargs):
            nonlocal called
            called = True
            return self.completed(command, {})

        with self.assertRaisesRegex(WcoError, "named Compose file"):
            prepare_isolation_override(
                invocation,
                fake_run,
                self.override_root,
            )

        self.assertFalse(called)

    def test_render_failure_does_not_create_an_override(self) -> None:
        invocation = self.invocation(["config"])

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "invalid compose")

        with self.assertRaisesRegex(WcoError, "invalid compose"):
            prepare_isolation_override(
                invocation,
                fake_run,
                self.override_root,
            )

        self.assertFalse(self.override_root.exists())

    def test_start_validation_uses_the_merged_override(self) -> None:
        self.enable_port_rewrite()
        invocation = self.invocation()

        def base_run(command: list[str], **_kwargs):
            return self.completed(
                command,
                {
                    "web": {
                        "container_name": "fixed-web",
                        "ports": [{"target": 80, "published": "8000"}],
                    }
                },
            )

        override = prepare_isolation_override(
            invocation,
            base_run,
            self.override_root,
        )
        assert override is not None
        self.assertEqual(override.rewritten_ports, 1)
        validation_commands: list[list[str]] = []

        def merged_run(command: list[str], **_kwargs):
            validation_commands.append(command)
            return self.completed(
                command,
                {
                    "web": {
                        "container_name": override.names["web"],
                        "ports": [
                            {
                                "target": 80,
                                "published": str(invocation.ports["HTTP_PORT"]),
                            }
                        ],
                    }
                },
            )

        with (
            patch("wco.cli._published_ports", return_value=set()),
            patch("wco.cli.port_is_available", return_value=True),
        ):
            _validate_isolated_start(invocation, override, merged_run)

        self.assertIn(str(override.path), validation_commands[0])
        self.assertEqual(
            validation_commands[0][-3:], ["config", "--format", "json"]
        )


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
        project_directory = captured["args"].index("--project-directory")
        self.assertEqual(
            captured["args"][project_directory + 1],
            str(self.fixture.main.resolve()),
        )
        environment = captured["environment"]
        self.assertEqual(environment["SOURCE_PATH"], str(self.fixture.main.resolve()))
        self.assertIn("WCO context", stderr.getvalue())
        self.assertIn("Worktree", stderr.getvalue())

    def test_isolated_invocations_use_stable_unique_names_and_ports(self) -> None:
        state = self.fixture.root / "state" / "ports.json"
        store = PortStore(state, availability=lambda _port: True)
        first = prepare_invocation(["config"], True, self.fixture.main, store)
        repeated = prepare_invocation(["config"], True, self.fixture.main, store)

        self.assertEqual(first.project_name, repeated.project_name)
        self.assertEqual(first.ports, repeated.ports)
        self.assertIn("main-", first.project_name)
        self.assertEqual(first.environment["PREFIX"], first.instance_name)

    def test_run_applies_generated_override_to_isolated_start(self) -> None:
        config_path = self.fixture.workspace / ".wco.toml"
        config_path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                "[isolation]\nrewrite_container_names = true\nrewrite_ports = true\n",
            )
        )
        (self.fixture.main / ".env").write_text("APP_ENV=test\n")
        store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        captured: dict[str, object] = {}

        def capture(file: str, args: object, environment: object) -> None:
            captured.update(file=file, args=args, environment=environment)

        def fake_run(command: list[str], **_kwargs):
            rendered = {
                "services": {
                    "web": {
                        "container_name": "fixed-web",
                        "ports": [{"target": 80, "published": "8000"}],
                    }
                }
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(rendered), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("wco.cli.default_port_store", return_value=store),
            patch(
                "wco.cli.override_directory",
                return_value=self.fixture.root / "overrides",
            ),
            patch("wco.cli._validate_isolated_start"),
        ):
            result = run(
                ["--isolated", "--file", "extra.yml", "up", "-d"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                exec_fn=capture,
                run_process=fake_run,
            )

        self.assertEqual(result, 0)
        command = list(captured["args"])
        override_files = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--file" and command[index + 1].endswith(".compose.yaml")
        ]
        self.assertEqual(len(override_files), 1)
        self.assertLess(command.index("extra.yml"), command.index(override_files[0]))
        self.assertLess(command.index(override_files[0]), command.index("up"))
        self.assertIn("Name overrides", stderr.getvalue())
        self.assertIn("Port overrides", stderr.getvalue())
        self.assertIn("exact-name-dependent tooling", stderr.getvalue())
        self.assertIn("Rotating 1 fixed published host port", stderr.getvalue())

    def test_shared_invocation_does_not_render_or_add_an_override(self) -> None:
        config_path = self.fixture.workspace / ".wco.toml"
        config_path.write_text(
            CONFIG.replace(
                "[isolation]\n",
                "[isolation]\nrewrite_container_names = true\n",
            )
        )
        captured: dict[str, object] = {}

        def capture(file: str, args: object, environment: object) -> None:
            captured.update(file=file, args=args, environment=environment)

        def unexpected_run(*_args, **_kwargs):
            raise AssertionError("shared mode must not render an override")

        result = run(
            ["config"],
            cwd=self.fixture.main,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            exec_fn=capture,
            run_process=unexpected_run,
        )

        self.assertEqual(result, 0)
        self.assertNotIn(".compose.yaml", " ".join(captured["args"]))

    def test_shared_build_uses_context_from_central_workspace_when_missing_in_worktree(
        self,
    ) -> None:
        central_context = self.fixture.workspace / "docker" / "nginx"
        central_context.mkdir(parents=True)
        (central_context / "Dockerfile").write_text("FROM scratch\n")
        invocation = prepare_invocation(["build", "nginx"], False, self.fixture.main)

        def fake_run(command: list[str], **_kwargs):
            rendered_context = self.fixture.main / "docker" / "nginx"
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "services": {
                            "nginx": {
                                "build": {"context": str(rendered_context)}
                            }
                        }
                    }
                ),
                "",
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.fixture.root / "overrides",
        )

        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(
            override.build_contexts,
            {"nginx": str(central_context.resolve())},
        )
        self.assertIn(
            f'context: {json.dumps(str(central_context.resolve()))}',
            override.path.read_text(),
        )
        command = build_invocation_command(invocation, override)
        self.assertLess(command.index(str(override.path)), command.index("build"))

    def test_shared_build_keeps_an_existing_worktree_context(self) -> None:
        central_context = self.fixture.workspace / "docker" / "nginx"
        worktree_context = self.fixture.main / "docker" / "nginx"
        central_context.mkdir(parents=True)
        worktree_context.mkdir(parents=True)
        invocation = prepare_invocation(["build", "nginx"], False, self.fixture.main)

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "services": {
                            "nginx": {
                                "build": {"context": str(worktree_context)}
                            }
                        }
                    }
                ),
                "",
            )

        override = prepare_isolation_override(
            invocation,
            fake_run,
            self.fixture.root / "overrides",
        )

        self.assertIsNone(override)

    def test_shared_build_preserves_stdin_compose_passthrough(self) -> None:
        invocation = prepare_invocation(
            ["--file", "-", "build"],
            False,
            self.fixture.main,
        )

        def unexpected_run(*_args, **_kwargs):
            raise AssertionError("stdin Compose input must remain untouched")

        override = prepare_isolation_override(
            invocation,
            unexpected_run,
            self.fixture.root / "overrides",
        )

        self.assertIsNone(override)

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


class PsOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        # Isolated commands allocate port slots; keep them out of the real
        # user state file.
        self.store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        patcher = patch("wco.cli.default_port_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.fixture.close()

    @staticmethod
    def _compose_row() -> str:
        return json.dumps(
            {
                "ID": "a" * 64,
                "Name": "example-web-1",
                "Command": "python -m http.server 80",
                "Project": "example",
                "Service": "web",
                "State": "running",
                "Health": "healthy",
                "ExitCode": 0,
                "Publishers": [
                    {
                        "URL": "0.0.0.0",
                        "TargetPort": 80,
                        "PublishedPort": 8080,
                        "Protocol": "tcp",
                    },
                    {
                        "URL": "::",
                        "TargetPort": 80,
                        "PublishedPort": 8080,
                        "Protocol": "tcp",
                    },
                ],
            }
        )

    def _inspection(self, working_dir: str | None = None) -> str:
        if working_dir is None:
            working_dir = str(self.fixture.main.resolve())
        labels = (
            {"com.docker.compose.project.working_dir": working_dir}
            if working_dir
            else {}
        )
        return json.dumps(
            [
                {
                    "Id": "a" * 64,
                    "Created": "2026-08-01T12:00:00Z",
                    "Config": {"Image": "python:3.14-slim", "Labels": labels},
                    "State": {
                        "Status": "running",
                        "StartedAt": "2026-08-02T11:00:00Z",
                        "FinishedAt": "0001-01-01T00:00:00Z",
                        "ExitCode": 0,
                        "Health": {"Status": "healthy"},
                    },
                }
            ]
        )

    def _fake_run(
        self,
        working_dir: str | None = None,
        branch: str = "feature/login",
        commands: list[list[str]] | None = None,
    ):
        def fake_run(command: list[str], **_kwargs):
            if commands is not None:
                commands.append(command)
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, self._inspection(working_dir), ""
                )
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0, f"{branch}\n", "")
            return subprocess.CompletedProcess(command, 0, self._compose_row() + "\n", "")

        return fake_run

    @staticmethod
    def _container(
        identifier: str, name: str, project: str, working_dir: str
    ) -> dict[str, object]:
        return {
            "Id": identifier,
            "Name": f"/{name}",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.project.working_dir": working_dir,
                }
            },
            "State": {
                "Status": "running",
                "StartedAt": "2026-08-02T11:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "ExitCode": 0,
            },
        }

    def _stacks_run(self, feature: Path, commands: list[list[str]] | None = None):
        """Docker sees this workspace's shared and isolated stacks, plus a stranger."""
        main = str(self.fixture.main.resolve())
        isolated = _isolated_name("example", feature.resolve())
        containers = [
            self._container("a" * 64, "example-web-1", "example", main),
            self._container("b" * 64, f"{isolated}-web-1", isolated, str(feature.resolve())),
            self._container("c" * 64, "unrelated-web-1", "unrelated", main),
        ]
        branches = {main: "master", str(feature.resolve()): "feature/login"}

        def fake_run(command: list[str], **_kwargs):
            if commands is not None:
                commands.append(command)
            if command[:2] == ["docker", "ps"]:
                ids = "\n".join(str(item["Id"]) for item in containers)
                return subprocess.CompletedProcess(command, 0, ids + "\n", "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(containers), ""
                )
            if command[0] == "git":
                return subprocess.CompletedProcess(
                    command, 0, f"{branches.get(command[2], 'main')}\n", ""
                )
            raise AssertionError(f"unexpected command: {command}")

        return fake_run

    @staticmethod
    def _column(rendered: str, name: str) -> str:
        lines = ANSI_ESCAPE.sub("", rendered).splitlines()
        header = next(line for line in lines if name in line)
        row = next(line for line in lines if "example-web-1" in line)
        start = header.index(name)
        following = [
            header.index(other)
            for other in ("NAME", "STATUS", "WORKTREE", "BRANCH")
            if header.index(other) > start
        ]
        end = min(following) if following else len(row)
        return row[start:end].strip()

    def test_interactive_ps_renders_colored_enriched_table(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        commands: list[list[str]] = []

        with wide_terminal(self.fixture):
            result = run(
                ["ps", "--all"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._fake_run(commands=commands),
                now_fn=lambda: self.now,
            )

        rendered = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("\x1b[", rendered)
        self.assertIn("Containers", rendered)
        headers = [
            rendered.index(name) for name in ("NAME", "STATUS", "WORKTREE", "BRANCH")
        ]
        self.assertEqual(headers, sorted(headers))
        for dropped in ("IMAGE", "SERVICE", "PORTS", "COMMAND", "CREATED"):
            self.assertNotIn(dropped, rendered)
        self.assertIn("example-web-1", rendered)
        self.assertIn("Up 1 hour", rendered)
        self.assertIn("(healthy)", rendered)
        self.assertEqual(self._column(rendered, "WORKTREE"), str(self.fixture.main.resolve()))
        self.assertEqual(self._column(rendered, "BRANCH"), "feature/login")
        self.assertIn("WCO context", stderr.getvalue())
        self.assertNotIn("Worktree", ANSI_ESCAPE.sub("", stderr.getvalue()))
        self.assertEqual(commands[0][-4:], ["ps", "--format", "json", "--all"])
        self.assertEqual(commands[1][:4], ["docker", "inspect", "--type", "container"])
        self.assertEqual(
            commands[2],
            [
                "git",
                "-C",
                str(self.fixture.main.resolve()),
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ],
        )

    def test_ps_highlights_a_foreign_worktree(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        other = self.fixture.create_repo("feature")

        with wide_terminal(self.fixture):
            result = run(
                ["ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._fake_run(working_dir=str(other.resolve())),
                now_fn=lambda: self.now,
            )

        rendered = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(self._column(rendered, "WORKTREE"), str(other.resolve()))
        self.assertRegex(rendered, r"\x1b\[33m[^\x1b]*" + re.escape(str(other.resolve())))

    def test_isolated_ps_uses_the_same_columns(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()

        with wide_terminal(self.fixture):
            result = run(
                ["--isolated", "ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._fake_run(),
                now_fn=lambda: self.now,
            )

        rendered = stdout.getvalue()
        context = ANSI_ESCAPE.sub("", stderr.getvalue())
        self.assertEqual(result, 0)
        self.assertIn("WCO context", context)
        self.assertNotIn("Worktree", context)
        self.assertEqual(self._column(rendered, "WORKTREE"), str(self.fixture.main.resolve()))
        self.assertEqual(self._column(rendered, "BRANCH"), "feature/login")

    def test_ps_marks_containers_without_a_recorded_worktree(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        commands: list[list[str]] = []

        with wide_terminal(self.fixture):
            result = run(
                ["ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._fake_run(working_dir="", commands=commands),
                now_fn=lambda: self.now,
            )

        rendered = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(self._column(rendered, "WORKTREE"), "-")
        self.assertEqual(self._column(rendered, "BRANCH"), "-")
        self.assertFalse([command for command in commands if command[0] == "git"])

    def test_ps_reports_a_detached_head_as_a_revision(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()

        def fake_run(command: list[str], **kwargs):
            if command[0] == "git" and "--short" in command:
                return subprocess.CompletedProcess(command, 0, "1a2b3c4\n", "")
            return self._fake_run(branch="HEAD")(command, **kwargs)

        with wide_terminal(self.fixture):
            result = run(
                ["ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=fake_run,
                now_fn=lambda: self.now,
            )

        self.assertEqual(result, 0)
        self.assertEqual(self._column(stdout.getvalue(), "BRANCH"), "(1a2b3c4)")

    def test_isolated_ps_fits_a_narrow_terminal(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()

        with patch.dict(os.environ, {"COLUMNS": "54"}):
            result = run(
                ["--isolated", "ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._fake_run(),
                now_fn=lambda: self.now,
            )

        lines = ANSI_ESCAPE.sub("", stdout.getvalue()).splitlines()
        self.assertEqual(result, 0)
        self.assertTrue(lines)
        self.assertLessEqual(max(map(len, lines)), 54)

    def test_interactive_ps_fits_a_narrow_terminal(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        fake_run = self._fake_run()

        with patch.dict(os.environ, {"COLUMNS": "54"}):
            result = run(
                ["ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=fake_run,
                now_fn=lambda: self.now,
            )

        self.assertEqual(result, 0)
        lines = ANSI_ESCAPE.sub("", stdout.getvalue()).splitlines()
        self.assertTrue(lines)
        self.assertLessEqual(max(map(len, lines)), 54)

    def test_empty_interactive_ps_has_explicit_empty_state(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 0, "", "compose warning\n")

        result = run(
            ["ps"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=stderr,
            run_process=fake_run,
            now_fn=lambda: self.now,
        )

        self.assertEqual(result, 0)
        self.assertIn("NAME", stdout.getvalue())
        self.assertIn("No containers found", stdout.getvalue())
        self.assertIn("compose warning", stderr.getvalue())

    def test_non_terminal_and_explicit_formats_preserve_exec_passthrough(self) -> None:
        captured: list[list[str]] = []

        def capture(_file: str, arguments: list[str], _environment: object) -> None:
            captured.append(arguments)

        result = run(
            ["ps"],
            cwd=self.fixture.main,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            exec_fn=capture,
        )
        self.assertEqual(result, 0)
        self.assertEqual(captured[-1][-1], "ps")

        result = run(
            ["ps", "--format", "json"],
            cwd=self.fixture.main,
            stdout=TerminalBuffer(),
            stderr=TerminalBuffer(),
            exec_fn=capture,
        )
        self.assertEqual(result, 0)
        self.assertEqual(captured[-1][-3:], ["ps", "--format", "json"])

    def test_compose_failure_preserves_output_and_status(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 17, "partial\n", "compose failed\n")

        result = run(
            ["ps"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=stderr,
            run_process=fake_run,
        )

        self.assertEqual(result, 17)
        self.assertIn("partial", stdout.getvalue())
        self.assertIn("compose failed", stderr.getvalue())

    def test_inspect_race_warns_and_keeps_compose_row(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        call_count = 0

        def fake_run(command: list[str], **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(command, 0, self._compose_row(), "")
            return subprocess.CompletedProcess(command, 1, "[]", "No such container")

        result = run(
            ["ps"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=stderr,
            run_process=fake_run,
            now_fn=lambda: self.now,
        )

        self.assertEqual(result, 0)
        self.assertIn("example-web-1", stdout.getvalue())
        self.assertIn("Could not enrich every container", stderr.getvalue())

    def test_stacks_lists_shared_and_isolated_containers_across_worktrees(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        commands: list[list[str]] = []
        feature = self.fixture.create_repo("feature")

        with wide_terminal(self.fixture):
            result = run(
                ["stacks"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=stderr,
                run_process=self._stacks_run(feature, commands=commands),
                now_fn=lambda: self.now,
            )

        rendered = ANSI_ESCAPE.sub("", stdout.getvalue())
        rows = [line for line in rendered.splitlines() if "example-" in line]
        self.assertEqual(result, 0)
        self.assertIn("Workspace stacks", rendered)
        self.assertEqual(len(rows), 2)
        self.assertIn("shared", rows[0])
        self.assertIn("example-web-1", rows[0])
        self.assertIn(str(self.fixture.main.resolve()), rows[0])
        self.assertIn("master", rows[0])
        self.assertIn("isolated", rows[1])
        self.assertIn(str(feature.resolve()), rows[1])
        self.assertIn("feature/login", rows[1])
        self.assertNotIn("unrelated", rendered)
        self.assertEqual(
            commands[0],
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                "{{.ID}}",
            ],
        )

    def test_stacks_numbers_the_shared_stack_1_and_isolated_stacks_from_2(self) -> None:
        stdout = TerminalBuffer()
        feature = self.fixture.create_repo("feature")

        with wide_terminal(self.fixture):
            result = run(
                ["stacks"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=TerminalBuffer(),
                run_process=self._stacks_run(feature),
                now_fn=lambda: self.now,
            )

        rendered = ANSI_ESCAPE.sub("", stdout.getvalue())
        rows = [line for line in rendered.splitlines() if "example-" in line]
        self.assertEqual(result, 0)
        self.assertIn("ID", rendered)
        self.assertIn("1.1", rows[0])
        self.assertIn("2.1", rows[1])
        # The ID is recorded, so it survives into the next listing.
        self.assertEqual(
            self.store.list_stack_ids(load_config(self.fixture.workspace / ".wco.toml")),
            {feature.resolve(): 2},
        )

    def test_ps_numbers_containers_within_the_current_stack(self) -> None:
        stdout = TerminalBuffer()

        with wide_terminal(self.fixture):
            result = run(
                ["ps"],
                cwd=self.fixture.main,
                stdout=stdout,
                stderr=TerminalBuffer(),
                run_process=self._fake_run(),
                now_fn=lambda: self.now,
            )

        rendered = ANSI_ESCAPE.sub("", stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(self._column(rendered, "ID"), "1.1")

    def test_stacks_all_includes_stopped_containers(self) -> None:
        commands: list[list[str]] = []
        feature = self.fixture.create_repo("feature")

        result = run(
            ["stacks", "--all"],
            cwd=self.fixture.main,
            stdout=TerminalBuffer(),
            stderr=TerminalBuffer(),
            run_process=self._stacks_run(feature, commands=commands),
            now_fn=lambda: self.now,
        )

        self.assertEqual(result, 0)
        self.assertEqual(commands[0][:3], ["docker", "ps", "-a"])

    def test_stacks_json_reports_every_container(self) -> None:
        stdout = io.StringIO()
        feature = self.fixture.create_repo("feature")

        result = run(
            ["stacks", "--format", "json"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=io.StringIO(),
            run_process=self._stacks_run(feature),
            now_fn=lambda: self.now,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["project"], "example")
        self.assertEqual(payload["worktree"], str(self.fixture.main.resolve()))
        self.assertEqual(
            [(item["mode"], item["worktree"], item["branch"]) for item in payload["containers"]],
            [
                ("shared", str(self.fixture.main.resolve()), "master"),
                ("isolated", str(feature.resolve()), "feature/login"),
            ],
        )
        self.assertEqual(
            [(item["id"], item["stack_id"]) for item in payload["containers"]],
            [("1.1", 1), ("2.1", 2)],
        )

    def test_stacks_reports_an_empty_workspace(self) -> None:
        stdout = TerminalBuffer()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run(
            ["stacks"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=TerminalBuffer(),
            run_process=fake_run,
            now_fn=lambda: self.now,
        )

        self.assertEqual(result, 0)
        self.assertIn("No containers found for this workspace", stdout.getvalue())

    def test_stacks_rejects_unknown_options_and_surfaces_docker_errors(self) -> None:
        stderr = io.StringIO()
        result = run(
            ["stacks", "--everything"],
            cwd=self.fixture.main,
            stdout=io.StringIO(),
            stderr=stderr,
            run_process=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result, 1)
        self.assertIn("Unknown wco stacks option: --everything", stderr.getvalue())

        stderr = io.StringIO()

        def failing_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "daemon unreachable\n")

        result = run(
            ["stacks"],
            cwd=self.fixture.main,
            stdout=io.StringIO(),
            stderr=stderr,
            run_process=failing_run,
        )
        self.assertEqual(result, 1)
        self.assertIn("daemon unreachable", stderr.getvalue())

    def _down_all_run(
        self,
        feature: Path,
        commands: list[list[str]],
        failing_worktree: Path | None = None,
    ):
        """Compose calls are recorded; everything else uses the stacks fixture."""
        base = self._stacks_run(feature, commands=commands)

        def fake_run(command: list[str], **kwargs):
            if command[:2] == ["docker", "compose"]:
                commands.append(command)
                failed = (
                    failing_worktree is not None
                    and str(failing_worktree.resolve()) in command
                )
                return subprocess.CompletedProcess(command, 3 if failed else 0, "", "")
            return base(command, **kwargs)

        return fake_run

    def test_isolated_down_all_targets_every_worktree(self) -> None:
        feature = self.fixture.create_repo("feature")
        commands: list[list[str]] = []
        fake_run = self._down_all_run(feature, commands)
        # main holds a port slot but runs no isolated container; feature is the
        # reverse, so the two discovery sources are both exercised.
        self.store.get_or_allocate(
            load_config(self.fixture.workspace / ".wco.toml"),
            self.fixture.main.resolve(),
        )

        executed: list[list[str]] = []
        result = run(
            ["--isolated", "down"],
            cwd=self.fixture.main,
            stdout=TerminalBuffer(),
            stderr=TerminalBuffer(),
            run_process=fake_run,
            exec_fn=lambda _file, args, _environment: executed.append(list(args)),
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(executed), 1)
        self.assertEqual([c for c in commands if c[-1] == "down"], [])

        commands.clear()
        result = run(
            ["--isolated", "down", "--all"],
            cwd=self.fixture.main,
            stdout=TerminalBuffer(),
            stderr=TerminalBuffer(),
            run_process=fake_run,
        )

        self.assertEqual(result, 0)
        downs = [command for command in commands if command[-1] == "down"]
        directories = [
            command[command.index("--project-directory") + 1] for command in downs
        ]
        projects = [command[command.index("--project-name") + 1] for command in downs]
        self.assertEqual(len(downs), 2)
        self.assertEqual(
            directories,
            sorted([str(feature.resolve()), str(self.fixture.main.resolve())]),
        )
        self.assertEqual(
            projects,
            [_isolated_name("example", Path(directory)) for directory in directories],
        )
        self.assertNotIn("--all", [argument for command in downs for argument in command])

    def test_isolated_down_all_reports_an_empty_workspace(self) -> None:
        stdout = TerminalBuffer()

        def fake_run(command: list[str], **_kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run(
            ["--isolated", "down", "--all"],
            cwd=self.fixture.main,
            stdout=stdout,
            stderr=TerminalBuffer(),
            run_process=fake_run,
        )

        self.assertEqual(result, 0)
        self.assertIn("No worktree has an isolated stack", stdout.getvalue())

    def test_isolated_down_all_continues_after_a_failure(self) -> None:
        feature = self.fixture.create_repo("feature")
        commands: list[list[str]] = []
        fake_run = self._down_all_run(feature, commands, failing_worktree=feature)
        self.store.get_or_allocate(
            load_config(self.fixture.workspace / ".wco.toml"),
            self.fixture.main.resolve(),
        )

        result = run(
            ["--isolated", "down", "--all"],
            cwd=self.fixture.main,
            stdout=TerminalBuffer(),
            stderr=TerminalBuffer(),
            run_process=fake_run,
        )

        self.assertEqual(result, 3)
        self.assertEqual(len([c for c in commands if c[-1] == "down"]), 2)

    def test_no_color_environment_disables_ansi(self) -> None:
        stdout = TerminalBuffer()
        stderr = TerminalBuffer()
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            result = run(["--help"], stdout=stdout, stderr=stderr)

        self.assertEqual(result, 0)
        self.assertNotRegex(stdout.getvalue(), r"\x1b\[(?:3[0-9]|9[0-7])m")


class StackIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.config = load_config(self.fixture.workspace / ".wco.toml")
        self.store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        self.store.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.fixture.close()

    def test_ids_start_after_the_shared_stack_and_are_stable(self) -> None:
        feature = self.fixture.create_repo("feature")
        first = self.store.stack_id(self.config, self.fixture.main)
        second = self.store.stack_id(self.config, feature)

        self.assertEqual(first, SHARED_STACK_ID + 1)
        self.assertEqual(second, SHARED_STACK_ID + 2)
        self.assertEqual(self.store.stack_id(self.config, self.fixture.main), first)
        self.assertEqual(
            self.store.worktree_for_stack_id(self.config, second), feature
        )
        self.assertIsNone(self.store.worktree_for_stack_id(self.config, 99))

    def test_a_removed_worktree_frees_its_id_for_reuse(self) -> None:
        feature = self.fixture.create_repo("feature")
        self.store.stack_id(self.config, self.fixture.main)
        self.assertEqual(self.store.stack_id(self.config, feature), 3)

        subprocess.run(["rm", "-rf", str(feature)], check=True)
        self.assertEqual(self.store.list_stack_ids(self.config), {self.fixture.main: 2})

        other = self.fixture.create_repo("other")
        self.assertEqual(self.store.stack_id(self.config, other), 3)

    def test_ids_are_allocated_without_any_isolated_ports(self) -> None:
        (self.fixture.workspace / ".wco.toml").write_text(
            CONFIG.split("[isolation.ports]")[0]
        )
        config = load_config(self.fixture.workspace / ".wco.toml")
        self.assertEqual(config.isolation.ports, {})

        with patch("wco.cli.default_port_store", return_value=self.store):
            invocation = prepare_invocation(["ps"], True, self.fixture.main)

        self.assertEqual(invocation.stack_id, SHARED_STACK_ID + 1)
        self.assertIsNone(invocation.slot)

    def test_a_version_1_state_file_is_upgraded_without_losing_assignments(self) -> None:
        slot, ports = 1, {"HTTP_PORT": 8100, "DEV_PORT": 5100}
        self.store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "assignments": [
                        {
                            "config": str(self.config.path),
                            "worktree": str(self.fixture.main),
                            "slot": slot,
                            "ports": ports,
                        }
                    ],
                }
            )
        )

        identifier = self.store.stack_id(self.config, self.fixture.main)
        data = json.loads(self.store.path.read_text())

        self.assertEqual(data["version"], STATE_VERSION)
        self.assertEqual(len(data["assignments"]), 1)
        self.assertEqual(data["assignments"][0]["slot"], slot)
        self.assertEqual(data["assignments"][0]["ports"], ports)
        self.assertEqual(data["stacks"][0]["id"], identifier)

    def test_an_unknown_state_version_is_still_rejected(self) -> None:
        self.store.path.write_text(json.dumps({"version": 99, "assignments": []}))
        with self.assertRaises(WcoError):
            self.store.stack_id(self.config, self.fixture.main)


class TargetParsingTests(unittest.TestCase):
    def test_an_id_is_only_read_directly_after_the_compose_command(self) -> None:
        self.assertEqual(split_target(["down", "2"]), (["down"], Target(2, None)))
        self.assertEqual(
            split_target(["restart", "2.1"]), (["restart"], Target(2, 1))
        )
        self.assertEqual(
            split_target(["--profile", "x", "down", "3"]),
            (["--profile", "x", "down"], Target(3, None)),
        )

    def test_numeric_arguments_elsewhere_are_passed_through(self) -> None:
        for arguments in (
            ["logs", "--tail", "20"],
            ["logs", "-f", "2.1"],
            ["up", "-d"],
            ["down"],
        ):
            self.assertEqual(split_target(arguments), (list(arguments), None))

    def test_zero_is_rejected_as_an_id(self) -> None:
        for arguments in (["down", "0"], ["restart", "2.0"]):
            with self.assertRaises(WcoError) as error:
                split_target(arguments)
            self.assertIn("IDs start at 1", str(error.exception))


class TargetDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkspaceFixture()
        self.feature = self.fixture.create_repo("feature")
        self.config = load_config(self.fixture.workspace / ".wco.toml")
        self.store = PortStore(
            self.fixture.root / "state" / "ports.json",
            availability=lambda _port: True,
        )
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        for patcher in (
            patch("wco.cli.default_port_store", return_value=self.store),
            patch("wco.cli._validate_isolated_start"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Startup commands such as 'restart' require this in the target worktree.
        (self.feature / ".env").write_text("")
        # Give the feature worktree stack ID 2.
        self.stack_id = self.store.stack_id(self.config, self.feature.resolve())
        self.project = _isolated_name("example", self.feature.resolve())

    def tearDown(self) -> None:
        self.fixture.close()

    def _capture(self, captured: dict[str, object]):
        def capture(file: str, args: object, environment: object) -> None:
            captured.update(file=file, args=args, environment=environment)

        return capture

    def _container_run(self, services: list[str]):
        """Docker reports one container per service in the isolated project."""
        containers = [
            {
                "Id": f"{index}" * 64,
                "Name": f"/{self.project}-{service}-1",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": self.project,
                        "com.docker.compose.service": service,
                        "com.docker.compose.container-number": "1",
                    }
                },
                "State": {"Status": "running"},
            }
            for index, service in enumerate(services, start=1)
        ]

        def fake_run(command: list[str], **_kwargs):
            if command[:2] == ["docker", "ps"]:
                ids = "\n".join(str(item["Id"]) for item in containers)
                return subprocess.CompletedProcess(command, 0, ids + "\n", "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(containers), ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        return fake_run

    def _run(self, argv: list[str], run_process=None, cwd: Path | None = None):
        captured: dict[str, object] = {}
        stdout, stderr = io.StringIO(), io.StringIO()
        result = run(
            argv,
            cwd=cwd or self.fixture.main,
            stdout=stdout,
            stderr=stderr,
            exec_fn=self._capture(captured),
            run_process=run_process
            or (lambda command, **_k: subprocess.CompletedProcess(command, 0, "", "")),
        )
        return result, captured, stdout.getvalue(), stderr.getvalue()

    def test_a_stack_id_retargets_another_worktree(self) -> None:
        result, captured, _stdout, stderr = self._run(["down", "2"])

        command = list(captured["args"])
        self.assertEqual(result, 0)
        self.assertEqual(
            command[command.index("--project-name") + 1], self.project
        )
        self.assertEqual(
            command[command.index("--project-directory") + 1],
            str(self.feature.resolve()),
        )
        self.assertEqual(command[-1], "down")
        self.assertIn("isolated", stderr)
        self.assertIn("stack 2", ANSI_ESCAPE.sub("", stderr))

    def test_isolated_flag_is_accepted_alongside_an_isolated_id(self) -> None:
        _result, with_flag, _out, _err = self._run(["--isolated", "down", "2"])
        _result, without_flag, _out, _err = self._run(["down", "2"])

        self.assertEqual(list(with_flag["args"]), list(without_flag["args"]))

    def test_stack_1_is_the_shared_stack_and_rejects_isolated(self) -> None:
        result, captured, _stdout, _stderr = self._run(["down", "1"])
        command = list(captured["args"])
        self.assertEqual(result, 0)
        self.assertEqual(command[command.index("--project-name") + 1], "example")

        result, _captured, _stdout, stderr = self._run(["--isolated", "down", "1"])
        self.assertEqual(result, 1)
        self.assertIn("shared stack", stderr)

    def test_a_container_id_resolves_to_its_service_name(self) -> None:
        result, captured, _stdout, _stderr = self._run(
            ["restart", "2.2"], run_process=self._container_run(["db", "web"])
        )

        command = list(captured["args"])
        self.assertEqual(result, 0)
        self.assertEqual(command[-2:], ["restart", "web"])
        self.assertEqual(
            command[command.index("--project-name") + 1], self.project
        )

    def test_a_container_id_precedes_the_command_arguments_for_exec(self) -> None:
        result, captured, _stdout, _stderr = self._run(
            ["exec", "2.1", "sh"], run_process=self._container_run(["db", "web"])
        )

        self.assertEqual(result, 0)
        self.assertEqual(list(captured["args"])[-3:], ["exec", "db", "sh"])

    def test_down_rejects_a_container_id(self) -> None:
        result, _captured, _stdout, stderr = self._run(["down", "2.1"])

        self.assertEqual(result, 1)
        self.assertIn("targets a whole stack", stderr)
        self.assertIn("wco stop 2.1", stderr)

    def test_an_out_of_range_container_id_is_rejected(self) -> None:
        result, _captured, _stdout, stderr = self._run(
            ["restart", "2.9"], run_process=self._container_run(["db", "web"])
        )

        self.assertEqual(result, 1)
        self.assertIn("out of range", stderr)
        self.assertIn("2.1-2.2", stderr)

    def test_an_unknown_stack_id_names_wco_stacks(self) -> None:
        result, _captured, _stdout, stderr = self._run(["down", "7"])

        self.assertEqual(result, 1)
        self.assertIn("No stack has ID 7", stderr)
        self.assertIn("wco stacks", stderr)

    def test_a_stack_id_whose_worktree_is_gone_is_reported(self) -> None:
        data = json.loads(self.store.path.read_text())
        data["stacks"][0]["worktree"] = str(self.fixture.workspace / "vanished")
        self.store.path.write_text(json.dumps(data))

        result, _captured, _stdout, stderr = self._run(["down", "2"])

        self.assertEqual(result, 1)
        # _prune drops the entry before lookup, so the ID reads as unknown.
        self.assertIn("No stack has ID 2", stderr)

    def test_down_all_is_unaffected_by_target_parsing(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run(
            ["--isolated", "down", "--all"],
            cwd=self.fixture.main,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            run_process=fake_run,
        )

        self.assertEqual(result, 0)
        self.assertTrue(any(command[-1] == "down" for command in commands))


if __name__ == "__main__":
    unittest.main()
