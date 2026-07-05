#!/usr/bin/env python3
"""Bump the patch component of __version__ in geobridge/__init__.py by 1.

Used by the release workflow (.github/workflows/publish.yml) right before
tagging and publishing. geobridge/__init__.py is the single source of truth
for the version — pyproject.toml reads it dynamically via
[tool.hatch.version].
"""

import os
import re
from pathlib import Path

INIT_PATH = Path(__file__).resolve().parent.parent / "geobridge" / "__init__.py"

VERSION_RE = re.compile(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"')


def main() -> None:
    text = INIT_PATH.read_text()
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit(f'Could not find __version__ = "X.Y.Z" in {INIT_PATH}')

    major, minor, patch = (int(g) for g in match.groups())
    new_version = f"{major}.{minor}.{patch + 1}"

    new_text = f"{text[:match.start()]}__version__ = \"{new_version}\"{text[match.end():]}"
    INIT_PATH.write_text(new_text)

    print(new_version)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(f"version={new_version}\n")


if __name__ == "__main__":
    main()
