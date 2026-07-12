# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "tomlkit",
# ]
# ///
"""Patch the pywhispercpp wheel path in the shared STT pyproject.toml (wh-e2m7).

Called by scripts/build_stt_vulkan_wheel.bat after a successful wheel build so
the [tool.uv.sources] entry always points at the wheel that actually exists on
disk. Replaces the old copy-paste-the-filename manual step.

Uses tomlkit so comments and formatting survive the rewrite. Idempotent: a
second run with the same wheel filename is a no-op.

Usage:
    uv run scripts/patch_stt_wheel_source.py --wheel <wheel-filename>
    uv run scripts/patch_stt_wheel_source.py --wheel <wheel-filename> \
        --pyproject <path/to/pyproject.toml>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import tomlkit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PYPROJECT = (
    REPO_ROOT / "services" / "stt_providers" / "shared" / "pyproject.toml"
)


def patch(pyproject_path: Path, wheel_filename: str) -> int:
    """Rewrite [tool.uv.sources].pywhispercpp.path to the new wheel.

    Returns 0 on success (including the already-current no-op), 1 on any
    validation failure. Never writes on failure.
    """
    if not pyproject_path.is_file():
        print(f"error: pyproject not found: {pyproject_path}", file=sys.stderr)
        return 1

    wheel_path = pyproject_path.parent / "vendor" / "wheels" / wheel_filename
    if not wheel_path.is_file():
        print(
            f"error: wheel not found on disk: {wheel_path}\n"
            "refusing to point pyproject.toml at a nonexistent wheel",
            file=sys.stderr,
        )
        return 1

    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    try:
        entry = doc["tool"]["uv"]["sources"]["pywhispercpp"]
    except (KeyError, TypeError):
        print(
            "error: no [tool.uv.sources] pywhispercpp entry in "
            f"{pyproject_path}; add one manually first",
            file=sys.stderr,
        )
        return 1

    old_path = str(entry.get("path", ""))
    new_path = f"vendor/wheels/{wheel_filename}"
    if old_path == new_path:
        print(f"unchanged: pywhispercpp source already points at {new_path}")
        return 0

    entry["path"] = new_path
    pyproject_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    print(f"patched {pyproject_path}:")
    print(f"  old: {old_path}")
    print(f"  new: {new_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Point [tool.uv.sources].pywhispercpp at a new vendored wheel."
    )
    parser.add_argument(
        "--wheel", required=True,
        help="Wheel filename (basename only) inside vendor/wheels/",
    )
    parser.add_argument(
        "--pyproject", type=Path, default=DEFAULT_PYPROJECT,
        help=f"pyproject.toml to patch (default: {DEFAULT_PYPROJECT})",
    )
    args = parser.parse_args()
    return patch(args.pyproject, args.wheel)


if __name__ == "__main__":
    sys.exit(main())
