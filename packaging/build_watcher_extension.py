"""Build the standalone Medhunt Watcher extension from watcher and shared files."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


WATCHER_FILES = (
    "manifest.json",
    "watcher.html",
    "watcher.css",
    "watcher.js",
    "watcher-background.js",
    "watcher-content.js",
)
SHARED_FILES = ("indeed-content.js", "inject.js")


def build(project: Path, output: Path) -> None:
    project = project.resolve()
    watcher = project / "src_pkg" / "watcher_frontend"
    shared = project / "src_pkg" / "frontend"
    output = output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in WATCHER_FILES:
        shutil.copy2(watcher / name, output / name)
    for name in SHARED_FILES:
        shutil.copy2(shared / name, output / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.project, args.output)


if __name__ == "__main__":
    main()
