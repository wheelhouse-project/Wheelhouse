"""One-off helper: insert the runtime-folder bootstrap into every entry point.

It is committed so the fourteen insertions are reproducible and reviewable,
not because anyone should run it again. The leading underscore keeps pytest
from collecting it, and it refuses a file that already carries the call, so a
second run changes nothing.
"""
import ast
import sys
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[3] / "services"

FILES = (
    "wheelhouse/launcher.py",
    "wheelhouse/main.py",
    "wheelhouse/input_proc.py",
    "wheelhouse/gui.py",
    "stt_providers/sherpa_offline_parakeet_stt_server/main.py",
    "stt_providers/sherpa_offline_parakeet_stt_server/launcher.py",
    "stt_providers/google_stt_server/main.py",
    "stt_providers/google_stt_server/launcher.py",
    "stt_providers/distil_medium_en/main.py",
    "stt_providers/distil_medium_en/launcher.py",
    "syscheck/syscheck.py",
    "installer/components/detector.py",
    "stt_providers/evaluation/run_benchmark.py",
    "stt_providers/evaluation/generate_corpus.py",
)

SNIPPET = '''\
# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()

'''


def insertion_line(tree, text):
    """1-based line the snippet goes on: after the docstring and __future__."""
    line = 1
    for node in tree.body:
        is_docstring = (isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str))
        is_future = (isinstance(node, ast.ImportFrom)
                     and node.module == "__future__")
        if is_docstring or is_future:
            line = node.end_lineno + 1
            continue
        break
    return line


def main():
    for relative in FILES:
        path = SERVICES / relative
        text = path.read_text(encoding="utf-8")
        if "add_runtime_dll_directory" in text:
            print(f"SKIP (already present) {relative}")
            continue
        tree = ast.parse(text, filename=str(path))
        at = insertion_line(tree, text)
        lines = text.splitlines(keepends=True)
        before = "".join(lines[:at - 1])
        after = "".join(lines[at - 1:])
        if before and not before.endswith("\n\n"):
            before = before.rstrip("\n") + "\n\n"
        path.write_text(before + SNIPPET + after, encoding="utf-8")
        print(f"INSERTED at line {at} in {relative}")


if __name__ == "__main__":
    sys.exit(main())
