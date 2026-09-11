"""Create the comment-free browser bundle used by release packages.

The readable sources stay in ``src_pkg/frontend`` for development.  Only the
generated directory is embedded in the Windows installer.  Minification is a
distribution hardening measure, not a security boundary: code that executes in
a browser remains inspectable by the computer owner.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


def release_javascript(source: str) -> str:
    """Remove explanatory full-line comments without rewriting JavaScript.

    Template literals contain significant whitespace. Generic legacy JS
    minifiers can corrupt that HTML, so the release step deliberately preserves
    every executable character and only removes standalone source comments.
    """
    lines = source.splitlines(keepends=True)
    return "".join(
        "\n" if line.lstrip().startswith("//") else line
        for line in lines
    )


def build(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    if source == output or source in output.parents:
        raise ValueError("The production output must not be inside the source directory.")
    if not (source / "manifest.json").is_file():
        raise FileNotFoundError(f"Extension manifest not found in {source}")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for item in source.iterdir():
        if not item.is_file():
            continue
        target = output / item.name
        if item.suffix.casefold() == ".js":
            target.write_text(
                release_javascript(item.read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copy2(item, target)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    icon_paths = {
        *manifest.get("icons", {}).values(),
        *manifest.get("action", {}).get("default_icon", {}).values(),
    }
    for relative in sorted(icon_paths):
        source_icon = source / relative
        target_icon = output / relative
        if not source_icon.is_file():
            raise RuntimeError(f"Production extension icon is missing: {relative}")
        target_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_icon, target_icon)
    required = {
        manifest["background"]["service_worker"],
        manifest["side_panel"]["default_path"],
        *icon_paths,
        *(script for entry in manifest.get("content_scripts", []) for script in entry.get("js", [])),
    }
    missing = sorted(name for name in required if not (output / name).is_file())
    if missing:
        raise RuntimeError(f"Production extension is missing referenced files: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
