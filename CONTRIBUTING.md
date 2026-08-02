# Contributing to wco

Thank you for helping improve `wco`.

## Development setup

`wco` requires Python 3.11 or newer, Git, Docker, and Docker Compose v2. It can
be developed on Linux, macOS, native Windows, or WSL2.

Clone the repository, then install the command in editable mode:

```bash
uv tool install --editable --force .
```

## Tests

Run the complete test suite from the repository root:

```bash
uv run --no-project --with-editable . python -m unittest discover -v
uv run --no-project --with-editable . python -m compileall -q src tests
```

Build the release distributions when changing packaging:

```bash
uv build --clear
```

## Pull requests

- Keep changes focused and include tests for new behavior or bug fixes.
- Preserve compatibility with the supported `.wco.toml` schema unless the change is intentionally breaking.
- Update `README.md` and `CHANGELOG.md` for user-visible changes.
- Ensure all CI checks pass before requesting review.

By contributing, you agree that your contributions will be licensed under the
MIT License.
