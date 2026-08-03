from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


UNICODE_GLYPHS = {
    "heading": "▌",
    "running": "●",
    "pending": "◐",
    "idle": "○",
    "stopped": "✖",
    "unknown": "·",
    "error": "✖",
    "warning": "▲",
    "hint": "→",
}

ASCII_GLYPHS = {
    "heading": "|",
    "running": "*",
    "pending": "~",
    "idle": "o",
    "stopped": "x",
    "unknown": ".",
    "error": "x",
    "warning": "!",
    "hint": "->",
}

STATE_KINDS = {
    "running": "running",
    "healthy": "running",
    "starting": "pending",
    "restarting": "pending",
    "created": "pending",
    "paused": "idle",
    "removing": "idle",
    "exited": "stopped",
    "dead": "stopped",
    "unhealthy": "stopped",
}

KIND_STYLES = {
    "running": "green",
    "pending": "yellow",
    "idle": "cyan",
    "stopped": "red",
    "unknown": "dim",
}

PORT_STATUS_STYLES = {
    "allocated": "green",
    "configured": "green",
    "not allocated": "yellow",
    "not configured": "dim",
    "outdated": "yellow",
}

HEADING_STYLE = "bold cyan"
LABEL_STYLE = "dim"


@dataclass(frozen=True)
class PortRow:
    mode: str
    project: str
    slot: str
    variable: str
    port: str
    status: str
    worktree: str = "-"
    branch: str = "-"


@dataclass(frozen=True)
class PsRow:
    name: str
    status: str
    state: str
    health: str
    worktree: str = "-"
    branch: str = "-"
    id: str = "-"


@dataclass(frozen=True)
class StackRow(PsRow):
    mode: str = "shared"
    project: str = "-"


def _state_kind(state: str, health: str) -> str:
    if health:
        kind = STATE_KINDS.get(health.lower())
        if kind is not None:
            return kind
    return STATE_KINDS.get(state.lower(), "unknown")


class Output:
    """Terminal-aware presentation for WCO-owned output."""

    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
        *,
        force_terminal: bool | None = None,
    ) -> None:
        console_options = {
            "force_terminal": force_terminal,
            "highlight": False,
            "markup": False,
            "emoji": False,
            "safe_box": True,
        }
        self.stdout = Console(file=stdout, **console_options)
        self.stderr = Console(file=stderr, stderr=True, **console_options)
        self._glyphs = (
            UNICODE_GLYPHS if self._supports_unicode(self.stdout) else ASCII_GLYPHS
        )

    @staticmethod
    def _supports_unicode(console: Console) -> bool:
        if console.legacy_windows:
            return False
        try:
            "".join(UNICODE_GLYPHS.values()).encode(console.encoding or "ascii")
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    def _glyph(self, name: str) -> str:
        return self._glyphs[name]

    def _heading(self, console: Console, title: str, subtitle: str = "") -> None:
        heading = Text()
        if console.is_terminal:
            heading.append(f"{self._glyph('heading')} ", style="cyan")
        heading.append(title, style=HEADING_STYLE)
        if subtitle:
            heading.append(f"  {subtitle}", style=LABEL_STYLE)
        console.print()
        console.print(heading, no_wrap=True, overflow="ellipsis")

    @staticmethod
    def _table() -> Table:
        return Table(
            box=box.SIMPLE_HEAD,
            border_style=LABEL_STYLE,
            header_style="bold",
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
            collapse_padding=True,
        )

    @staticmethod
    def _fields() -> Table:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        table.add_column(style=LABEL_STYLE, no_wrap=True)
        table.add_column(overflow="fold")
        return table

    def _hint(self, console: Console, message: str) -> None:
        hint = Text()
        if console.is_terminal:
            hint.append(f"{self._glyph('hint')} ", style="cyan")
        hint.append(message, style=LABEL_STYLE)
        console.print(hint, soft_wrap=True)

    def _prefix(self, console: Console, glyph: str, label: str) -> str:
        return f"{self._glyph(glyph)} {label}" if console.is_terminal else label

    def hint(self, message: str) -> None:
        self.stdout.print()
        self._hint(self.stdout, message)

    def help(self, content: str, *, error: bool = False) -> None:
        console = self.stderr if error else self.stdout
        section = ""
        for line in content.rstrip("\n").splitlines():
            stripped = line.strip()
            if line.startswith("Usage:"):
                section = "Usage"
                label, value = line.split(":", 1)
                console.print(
                    Text.assemble((f"{label}:", HEADING_STYLE), (value, "cyan")),
                    soft_wrap=True,
                )
            elif line == stripped and stripped.endswith(":"):
                section = stripped.rstrip(":")
                console.print(Text(line, style=HEADING_STYLE))
            elif section == "Usage" and stripped.startswith("wco"):
                console.print(Text(line, style="cyan"), soft_wrap=True)
            elif section == "Options" and stripped.startswith("-"):
                indent = line[: len(line) - len(line.lstrip())]
                flags, separator, description = stripped.partition("  ")
                console.print(
                    Text.assemble(
                        indent,
                        (flags, "green"),
                        separator,
                        description,
                    ),
                    soft_wrap=True,
                )
            elif section == "Examples" and stripped.startswith("wco"):
                command, separator, remainder = stripped.partition(" ")
                console.print(
                    Text.assemble(
                        "  ",
                        (command, "bold green"),
                        separator,
                        (remainder, "green"),
                    ),
                    soft_wrap=True,
                )
            else:
                console.print(Text(line), soft_wrap=True)

    def version(self, version: str) -> None:
        self.stdout.print(
            Text.assemble(("wco", HEADING_STYLE), " ", (version, "cyan"))
        )

    def error(self, message: str) -> None:
        self.stderr.print(
            Text.assemble(
                (self._prefix(self.stderr, "error", "wco: error:"), "bold red"),
                f" {message}",
            ),
            soft_wrap=True,
        )

    def warning(self, message: str) -> None:
        self.stderr.print(
            Text.assemble(
                (self._prefix(self.stderr, "warning", "wco: warning:"), "bold yellow"),
                f" {message}",
            ),
            soft_wrap=True,
        )

    def context(
        self,
        worktree: Path | None,
        project: str,
        isolated: bool,
        slot: int | None,
        name_overrides: int = 0,
        port_overrides: int = 0,
        *,
        stack_id: int | None = None,
        current_worktree: Path | None = None,
    ) -> None:
        mode = "isolated" if isolated else "shared"
        badge = Text.assemble(
            (mode, "magenta" if isolated else "cyan"),
            (f"  stack {stack_id}", LABEL_STYLE) if stack_id is not None else "",
            (f"  slot {slot}", LABEL_STYLE) if slot is not None else "",
        )
        self._heading(self.stderr, "WCO context")
        table = self._fields()
        if worktree is not None:
            mismatch = current_worktree is not None and worktree != current_worktree
            table.add_row(
                "Worktree", Text(str(worktree), style="yellow" if mismatch else "")
            )
        table.add_row("Project", Text(project, style="bold"))
        table.add_row("Mode", badge)
        if name_overrides:
            table.add_row("Name overrides", str(name_overrides))
        if port_overrides:
            table.add_row("Port overrides", str(port_overrides))
        self.stderr.print(table)

    def initialized(self, config: Path, compose_file: str, project: str) -> None:
        self._heading(self.stdout, "Workspace initialized")
        table = self._fields()
        table.add_row("Config", str(config))
        table.add_row("Compose file", compose_file)
        table.add_row("Project", Text(project, style="bold cyan"))
        self.stdout.print(table)
        self.stdout.print()
        self._hint(
            self.stdout,
            "Next: use worktree-relative Compose paths and add any published "
            "port variables to [isolation.ports].",
        )

    def ports(
        self,
        title: str,
        rows: Sequence[PortRow],
        *,
        show_worktree: bool = False,
        current_worktree: Path | None = None,
    ) -> None:
        self._heading(self.stdout, title)
        table = self._table()
        table.add_column("MODE", no_wrap=True)
        if show_worktree:
            table.add_column("WORKTREE", overflow="fold")
            table.add_column("BRANCH", style="blue", overflow="fold")
        table.add_column("PROJECT", overflow="fold")
        table.add_column("SLOT", justify="right", no_wrap=True)
        table.add_column("VARIABLE", no_wrap=True)
        table.add_column("PORT", justify="right", no_wrap=True)
        table.add_column("STATUS", no_wrap=True)
        previous_group: tuple[str, str, str, str] | None = None
        for row in rows:
            group = (row.mode, row.worktree, row.project, row.slot)
            repeated = group == previous_group
            if previous_group is not None and not repeated:
                table.add_section()
            previous_group = group
            mode_style = "magenta" if row.mode == "isolated" else "cyan"
            cells = [
                Text("", style=mode_style)
                if repeated
                else Text(row.mode, style=mode_style),
            ]
            if show_worktree:
                cells.append(
                    Text("")
                    if repeated
                    else self._worktree_cell(row.worktree, current_worktree)
                )
                cells.append(Text("") if repeated else self._value(row.branch))
            cells.extend(
                [
                    Text("") if repeated else Text(row.project),
                    Text("") if repeated else self._value(row.slot),
                    self._value(row.variable),
                    self._value(row.port, style="bold"),
                    Text("", style=LABEL_STYLE)
                    if repeated
                    else Text(
                        row.status,
                        style=PORT_STATUS_STYLES.get(row.status, LABEL_STYLE),
                    ),
                ]
            )
            table.add_row(*cells)
        self.stdout.print(table)

    @staticmethod
    def _value(value: str, *, style: str = "") -> Text:
        return Text(value, style=LABEL_STYLE if value == "-" else style)

    def ps(
        self,
        rows: Sequence[PsRow],
        *,
        no_trunc: bool,
        current_worktree: Path | None = None,
    ) -> None:
        self._heading(self.stdout, "Containers", self._ps_summary(rows))
        table = self._table()
        overflow = "fold" if no_trunc else "ellipsis"
        no_wrap = not no_trunc
        table.expand = True
        table.add_column("", no_wrap=True, width=1)
        table.add_column("ID", no_wrap=True)
        table.add_column(
            "NAME",
            style="cyan",
            overflow=overflow,
            no_wrap=no_wrap,
            ratio=4,
            min_width=6,
        )
        table.add_column("STATUS", overflow="fold", ratio=3, min_width=7)
        table.add_column(
            "WORKTREE",
            overflow=overflow,
            no_wrap=no_wrap,
            ratio=5,
            min_width=8,
        )
        table.add_column(
            "BRANCH",
            style="blue",
            overflow=overflow,
            no_wrap=no_wrap,
            ratio=3,
            min_width=6,
        )
        cells = []
        for row in rows:
            kind = _state_kind(row.state, row.health)
            style = KIND_STYLES[kind]
            if row.health.lower() == "unhealthy":
                style = "bold red"
            cells.append(
                (
                    Text(self._glyph(kind), style=style),
                    self._value(row.id, style="bold"),
                    Text(row.name),
                    self._status_lines(row.status, style),
                    self._worktree_cell(row.worktree, current_worktree),
                    self._value(row.branch),
                )
            )
        table.expand = self._natural_width(table, cells) > self.stdout.width
        for cell in cells:
            table.add_row(*cell)
        self.stdout.print(table)
        if not rows:
            self._hint(
                self.stdout,
                "No containers found for this project. Start one with 'wco up -d'.",
            )

    def stacks(
        self,
        rows: Sequence[StackRow],
        *,
        current_worktree: Path | None = None,
    ) -> None:
        self._heading(self.stdout, "Workspace stacks", self._ps_summary(rows))
        table = self._table()
        table.expand = True
        table.add_column("", no_wrap=True, width=1)
        table.add_column("ID", no_wrap=True)
        table.add_column("MODE", no_wrap=True)
        table.add_column("NAME", style="cyan", overflow="ellipsis", no_wrap=True, ratio=4, min_width=6)
        table.add_column("STATUS", overflow="fold", ratio=3, min_width=7)
        table.add_column(
            "WORKTREE", overflow="ellipsis", no_wrap=True, ratio=5, min_width=8
        )
        table.add_column(
            "BRANCH", style="blue", overflow="ellipsis", no_wrap=True, ratio=3, min_width=6
        )
        cells = []
        for row in rows:
            kind = _state_kind(row.state, row.health)
            style = KIND_STYLES[kind]
            if row.health.lower() == "unhealthy":
                style = "bold red"
            cells.append(
                (
                    Text(self._glyph(kind), style=style),
                    self._value(row.id, style="bold"),
                    Text(row.mode, style="magenta" if row.mode == "isolated" else "cyan"),
                    Text(row.name),
                    self._status_lines(row.status, style),
                    self._worktree_cell(row.worktree, current_worktree),
                    self._value(row.branch),
                )
            )
        table.expand = self._natural_width(table, cells) > self.stdout.width
        for cell in cells:
            table.add_row(*cell)
        self.stdout.print(table)
        if not rows:
            self._hint(
                self.stdout,
                "No containers found for this workspace. Start one with 'wco up -d' "
                "or 'wco --isolated up -d'.",
            )

    @staticmethod
    def _worktree_cell(worktree: str, current_worktree: Path | None) -> Text:
        if worktree == "-":
            return Text(worktree, style=LABEL_STYLE)
        mismatch = current_worktree is not None and worktree != str(current_worktree)
        return Text(worktree, style="yellow" if mismatch else "")

    @staticmethod
    def _natural_width(table: Table, cells: Sequence[Sequence[Text]]) -> int:
        widths = []
        for index, column in enumerate(table.columns):
            content = [str(cell[index]) for cell in cells] + [str(column.header)]
            widths.append(max(len(line) for text in content for line in text.split("\n")))
        return sum(widths) + 2 * (len(widths) - 1)

    @staticmethod
    def _status_lines(status: str, style: str) -> Text:
        head, separator, health = status.partition(" (")
        if not separator:
            return Text(status, style=style)
        return Text.assemble((head, style), "\n", (f"({health}", style))

    def _ps_summary(self, rows: Sequence[PsRow]) -> str:
        if not rows:
            return ""
        counts: dict[str, int] = {}
        for row in rows:
            state = row.state.lower() or "unknown"
            counts[state] = counts.get(state, 0) + 1
        total = f"{len(rows)} container{'s' if len(rows) != 1 else ''}"
        breakdown = "  ".join(f"{count} {state}" for state, count in counts.items())
        separator = f"  {self._glyph('unknown')}  "
        return f"{total}{separator}{breakdown}"
