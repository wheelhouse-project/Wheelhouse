"""Mutation gate for the update download (wh-release-update-check).

services/wheelhouse/update_install.py decides which installer script the
"Update now" action downloads, whether the download is complete and genuine,
where it is saved, and which parameters the installer gets. This gate breaks
each of those decisions in turn and requires named tests in
tests/test_update_install.py to fail. A mutation that nothing catches is a
finding against the tests, not against the gate, and the gate does not
change the module to make it pass.

The same layer as tests/mutation_gate_update_check.py starts pytest: the
shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
owns the pattern checks, the exactly-one-match rule, the compile of every
mutant, the byte-exact restore and the scope line. This file replaces only
the launch layer, because the shared launcher goes through scripts/run_tests.py
and ``uv run``, and a worktree must not build a .venv through it. It sets
PYTHONDONTWRITEBYTECODE=1 and COLUMNS=1000, reads verdicts only from the
short-summary section, treats a pytest ERROR as no verdict, and prints each
failing test's reason.

update_install.py has CRLF line endings. The runner translates each "\\n" in a
pattern to the file's own ending, so the patterns below are written with "\\n".

Every mutation changes a VALUE a guard reads, or the body a guard protects,
and none changes how a loop advances, so no mutant can hang the suite. A
mutant that adds a folder (the order mutations) adds it inside the tests'
own tmp_path.

Usage (from services/wheelhouse):
    .venv/Scripts/python.exe tests/mutation_gate_update_install.py --check
    .venv/Scripts/python.exe tests/mutation_gate_update_install.py
    .venv/Scripts/python.exe tests/mutation_gate_update_install.py the-saved-file-name
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

WORKTREE = Path(__file__).resolve().parents[3]
WHEELHOUSE = WORKTREE / "services/wheelhouse"

spec = importlib.util.spec_from_file_location(
    "update_install_shared_gate",
    WORKTREE / "services/stt_providers/shared/tests/mutation_gate_runner.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# The launch layer
# ---------------------------------------------------------------------------

def _main_checkout() -> Path:
    """The checkout that owns the virtual environments."""
    git = WORKTREE / ".git"
    if git.is_dir():
        return WORKTREE
    gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    return gitdir.parents[2]


MAIN = _main_checkout()


def _interpreter(service: Path) -> Path:
    """The python.exe that owns a service's dependencies."""
    local = service / ".venv/Scripts/python.exe"
    if local.exists():
        return local
    return MAIN / service.relative_to(WORKTREE) / ".venv/Scripts/python.exe"


RUN_TIMEOUT = 180
_SUMMARY_HEADER = "short test summary info"


def _bare_name(text: str) -> str:
    """A collected or reported test id, without its path and parameter list.

    The name is read as the identifiers after the first "::", so a parameter
    id that holds a space cannot cut the name short, and a failure reason
    that holds "::" cannot move it.
    """
    node = text.split("::", 1)[1]
    return re.match(r"\w+(?:::\w+)*", node).group(0).split("::")[-1]


def _summary(output: str) -> str:
    return output.split(_SUMMARY_HEADER, 1)[1] if _SUMMARY_HEADER in output else ""


def failed_names(output: str) -> set[str]:
    """The tests pytest's short summary reports as FAILED, and only those."""
    return {
        _bare_name(line[len("FAILED "):])
        for line in _summary(output).splitlines()
        if line.startswith("FAILED ")
    }


def run_tests(service: Path, test_file, collect: bool = False):
    # COLUMNS: pytest cuts a short-summary line to the terminal width, and at
    # the 80-column default these ids leave no room for the reason.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")
    command = [
        str(_interpreter(service)), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:randomly",
    ]
    command += ["--collect-only", "-q"] if collect else ["-rf", "--tb=short"]
    result = subprocess.run(
        command, cwd=str(service), env=env,
        capture_output=True, text=True, timeout=RUN_TIMEOUT,
    )
    output = result.stdout + result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("collection failed: " + output[-2000:])
        return result
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"pytest exited {result.returncode}: " + output[-2000:])
    errored = [line for line in _summary(output).splitlines()
               if line.startswith("ERROR ")]
    if errored:
        raise RuntimeError("pytest reported an ERROR, which is no verdict: "
                           + "; ".join(errored[:3]))
    # The reason of each failure is the only thing that tells a mutant that
    # reached the assertion from one that crashed upstream of it.
    for line in _summary(output).splitlines():
        if line.startswith("FAILED "):
            print("  " + line[len("FAILED "):][:600], flush=True)
    return result


def collected_names(service: Path, test_file) -> set[str]:
    result = run_tests(service, test_file, collect=True)
    return {_bare_name(line) for line in result.stdout.splitlines()
            if "::" in line}


def clear_pycache(service: Path) -> None:
    skip = {".venv", ".git", "node_modules", ".pytest_cache", "vendor"}
    for parent, directories, _ in os.walk(service):
        directories[:] = [d for d in directories if d not in skip]
        if "__pycache__" in directories:
            directories.remove("__pycache__")
            shutil.rmtree(Path(parent) / "__pycache__", ignore_errors=True)


runner._run_pytest = run_tests
runner._collected_names = collected_names
runner._failed_names = failed_names
runner._clear_pycache = clear_pycache


# ---------------------------------------------------------------------------
# The mutations
# ---------------------------------------------------------------------------

TARGET = WHEELHOUSE / "update_install.py"
SUITE = "tests/test_update_install.py"
# One mutation's catcher lives in the other file, so that mutation runs
# both. The shared runner takes a tuple of paths for test_file.
SUITE_AND_UPDATE_NOW = (SUITE, "tests/test_update_now.py")

MKDTEMP = 'tempfile.mkdtemp(prefix="wheelhouse-update-", dir=parent)'
SIZE_CHECK = "    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:\n"
ADDRESS_CHECK = "    if url != _expected_asset_url(_checked_tag(tag)):\n"
ASSET_TEST = "        if isinstance(asset, dict) and asset.get(\"name\") == INSTALLER_ASSET_NAME:\n"
PARAMS_LINE = ('    params += ["-StartNow", "yes", "-AutoStart", '
               '"yes" if auto_start else "no"]\n')
PROVIDER_TEST = "        if isinstance(candidate, str) and candidate in VALID_STT_PROVIDERS:\n"
ENGINE_ORDER = ("    for read in (lambda: running_engine, "
                "lambda: config_service.get(STT_PROVIDER_KEY, None)):\n")


def mutation(name, old, new, expect, test_file=SUITE):
    return dict(name=name, service=WHEELHOUSE, test_file=test_file, file=TARGET,
                old=old, new=new, expect=list(expect))


MUTATIONS = [
    # --- the repository path check -----------------------------------------
    mutation(
        "the-repository-path-is-never-refused",
        '    if not (path.startswith("/repos/") and path.endswith(_RELEASES_SUFFIX)):\n',
        "    if False:\n",
        ["test_an_unexpected_releases_address_form_is_refused"]),
    # Each half of the check alone. A survivor at 06c98fdc (the two entries
    # below): the only test address, https://example.com/other/path, failed
    # BOTH halves, so keeping either half alone still refused it. The test is
    # now parametrized with one address per half: no-repos-prefix
    # (/other/releases/latest) catches the first entry, and no-latest-suffix
    # (/repos/x/y/other) catches the second.
    mutation(
        "the-repository-path-needs-only-the-suffix",
        '    if not (path.startswith("/repos/") and path.endswith(_RELEASES_SUFFIX)):\n',
        "    if not path.endswith(_RELEASES_SUFFIX):\n",
        ["test_an_unexpected_releases_address_form_is_refused"]),
    mutation(
        "the-repository-path-needs-only-the-prefix",
        '    if not (path.startswith("/repos/") and path.endswith(_RELEASES_SUFFIX)):\n',
        '    if not path.startswith("/repos/"):\n',
        ["test_an_unexpected_releases_address_form_is_refused"]),
    mutation(
        "the-repository-path-keeps-the-repos-segment",
        '    return path[len("/repos/"):-len(_RELEASES_SUFFIX)]\n',
        "    return path[1:-len(_RELEASES_SUFFIX)]\n",
        ["test_find_asset_returns_the_address_and_the_digest",
         "test_fetch_asset_asks_for_the_tag_address_once"]),
    # --- the tag check -------------------------------------------------------
    mutation(
        "a-tag-that-is-not-a-version-is-accepted",
        "    if not isinstance(tag, str) or parse_version(tag) is None:\n",
        "    if not isinstance(tag, str):\n",
        ["test_a_tag_that_is_not_a_version_is_refused"]),
    mutation(
        "the-release-address-skips-the-tag-check",
        "    tag = _checked_tag(tag)\n    base = RELEASES_URL",
        "    base = RELEASES_URL",
        ["test_a_tag_that_is_not_a_version_is_refused"]),
    # A survivor at 06c98fdc. find_installer_asset calls _checked_tag(tag),
    # but every test passed a valid tag to it, and fetch_installer_asset has
    # already refused a bad tag through release_url_for_tag. The new test
    # calls find_installer_asset with a tag that is not a version and with
    # the exact asset address for that tag text, so only the tag check refuses.
    mutation(
        "the-asset-address-check-skips-the-tag-check",
        ADDRESS_CHECK,
        "    if url != _expected_asset_url(tag):\n",
        ["test_find_asset_refuses_a_tag_that_is_not_a_version_even_with_its_own_address"]),
    # --- the address of the release of a tag -----------------------------------
    mutation(
        "the-release-address-is-the-latest-release",
        '    base = RELEASES_URL[: -len("latest")]\n',
        "    base = RELEASES_URL\n",
        ["test_the_release_address_names_the_tag_in_the_same_repository"]),
    mutation(
        "the-release-address-has-no-tags-segment-name",
        '    return base + "tags/" + urllib.parse.quote(tag, safe="")\n',
        '    return base + "tag/" + urllib.parse.quote(tag, safe="")\n',
        ["test_the_release_address_names_the_tag_in_the_same_repository"]),
    mutation(
        "the-fetch-asks-for-latest-not-the-tag",
        "    body = _get(get, release_url_for_tag(tag), running_version)\n",
        "    body = _get(get, RELEASES_URL, running_version)\n",
        ["test_fetch_asset_asks_for_the_tag_address_once"]),
    mutation(
        "the-fetch-sends-no-running-version",
        "    body = _get(get, release_url_for_tag(tag), running_version)\n",
        '    body = _get(get, release_url_for_tag(tag), "")\n',
        ["test_fetch_asset_asks_for_the_tag_address_once"]),
    # --- the expected asset address ------------------------------------------
    mutation(
        "the-asset-address-is-never-compared",
        ADDRESS_CHECK,
        "    if False:\n",
        ["test_find_asset_refuses_an_address_that_is_not_the_script_of_that_tag"]),
    mutation(
        "the-asset-address-is-compared-by-repository-only",
        ADDRESS_CHECK,
        "    if not (isinstance(url, str) and url.startswith("
        '_expected_asset_url(_checked_tag(tag)).rsplit("/", 2)[0])):\n',
        ["test_find_asset_refuses_an_address_that_is_not_the_script_of_that_tag"]),
    mutation(
        "the-asset-address-is-compared-by-path-only",
        ADDRESS_CHECK,
        "    if not (isinstance(url, str) and url.endswith("
        '_expected_asset_url(_checked_tag(tag)).split("github.com", 1)[1])):\n',
        ["test_find_asset_refuses_an_address_that_is_not_the_script_of_that_tag"]),
    mutation(
        "the-expected-asset-address-is-plain-http",
        '        f"https://github.com/{_repo_path()}/releases/download/"\n',
        '        f"http://github.com/{_repo_path()}/releases/download/"\n',
        ["test_find_asset_refuses_an_address_that_is_not_the_script_of_that_tag",
         "test_find_asset_returns_the_address_and_the_digest"]),
    mutation(
        "the-expected-asset-address-names-another-file",
        "        f\"{urllib.parse.quote(tag, safe='')}/{INSTALLER_ASSET_NAME}\"\n",
        "        f\"{urllib.parse.quote(tag, safe='')}/Wheelhouse-Setup.exe\"\n",
        ["test_find_asset_returns_the_address_and_the_digest",
         "test_fetch_asset_asks_for_the_tag_address_once"]),
    # --- the digest pattern --------------------------------------------------
    mutation(
        "the-digest-pattern-accepts-any-length",
        '_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-fA-F]{64})")\n',
        '_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-fA-F]+)")\n',
        ["test_find_asset_refuses_a_digest_it_cannot_read"]),
    mutation(
        "the-digest-pattern-accepts-any-characters",
        '_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-fA-F]{64})")\n',
        '_DIGEST_PATTERN = re.compile(r"sha256:(.{64})")\n',
        ["test_find_asset_refuses_a_digest_it_cannot_read"]),
    mutation(
        "the-digest-pattern-refuses-upper-case",
        '_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-fA-F]{64})")\n',
        '_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")\n',
        ["test_find_asset_reads_an_upper_case_digest"]),
    # A survivor at 06c98fdc. The refused digests in
    # test_find_asset_refuses_a_digest_it_cannot_read were all too short, the
    # wrong length or the wrong characters. None was a valid sha256:<64 hex>
    # followed by extra text, which is the only input where fullmatch and
    # match differ. Two such digests are now in the list.
    mutation(
        "the-digest-pattern-accepts-text-after-the-digest",
        "        match = _DIGEST_PATTERN.fullmatch(value)\n",
        "        match = _DIGEST_PATTERN.match(value)\n",
        ["test_find_asset_refuses_a_digest_it_cannot_read"]),
    mutation(
        "the-digest-keeps-its-upper-case",
        "            return match.group(1).lower()\n",
        "            return match.group(1)\n",
        ["test_find_asset_reads_an_upper_case_digest"]),
    mutation(
        "a-missing-digest-field-reads-as-an-empty-digest",
        "    if value is None:\n        return None\n",
        '    if value is None:\n        return ""\n',
        ["test_find_asset_without_a_digest_field_gives_no_digest",
         "test_find_asset_with_a_null_digest_gives_no_digest"]),
    mutation(
        "an-unreadable-digest-reads-as-no-digest",
        '    raise UpdateCheckError(f"digest is not in the form sha256:<hex>: {value!r}")\n',
        "    return None\n",
        ["test_find_asset_refuses_a_digest_it_cannot_read"]),
    # --- the size check and its limit ----------------------------------------
    mutation(
        "the-size-field-is-not-read",
        '        size=_read_size(asset.get("size")),\n',
        "        size=1,\n",
        ["test_find_asset_refuses_a_size_that_is_not_a_positive_integer",
         "test_find_asset_reads_the_size"]),
    mutation(
        "a-boolean-size-is-accepted",
        SIZE_CHECK,
        "    if not isinstance(value, int) or value <= 0:\n",
        ["test_find_asset_refuses_a_size_that_is_not_a_positive_integer"]),
    mutation(
        "a-size-of-zero-is-accepted",
        SIZE_CHECK,
        "    if not isinstance(value, int) or isinstance(value, bool) or value < 0:\n",
        ["test_find_asset_refuses_a_size_that_is_not_a_positive_integer"]),
    mutation(
        "a-negative-size-is-accepted",
        SIZE_CHECK,
        "    if not isinstance(value, int) or isinstance(value, bool):\n",
        ["test_find_asset_refuses_a_size_that_is_not_a_positive_integer"]),
    mutation(
        "a-size-that-is-not-an-integer-is-accepted",
        SIZE_CHECK,
        "    if isinstance(value, bool) or value <= 0:\n",
        ["test_find_asset_refuses_a_size_that_is_not_a_positive_integer"]),
    mutation(
        "the-size-limit-is-one-byte-too-high",
        "    if value > MAX_RESPONSE_BYTES:\n",
        "    if value > MAX_RESPONSE_BYTES + 1:\n",
        ["test_find_asset_refuses_a_size_over_the_limit_and_names_both_numbers"]),
    mutation(
        "the-size-limit-refuses-the-limit-itself",
        "    if value > MAX_RESPONSE_BYTES:\n",
        "    if value >= MAX_RESPONSE_BYTES:\n",
        ["test_find_asset_accepts_a_size_equal_to_the_limit"]),
    mutation(
        "the-size-limit-is-never-checked",
        "    if value > MAX_RESPONSE_BYTES:\n",
        "    if False:\n",
        ["test_find_asset_refuses_a_size_over_the_limit_and_names_both_numbers"]),
    mutation(
        "the-size-limit-error-does-not-name-the-limit",
        '            f"asset size {value} is larger than the download limit {MAX_RESPONSE_BYTES}"\n',
        '            f"asset size {value} is larger than the download limit"\n',
        ["test_find_asset_refuses_a_size_over_the_limit_and_names_both_numbers"]),
    # --- the asset search ----------------------------------------------------
    mutation(
        "the-asset-search-takes-the-first-asset-of-any-name",
        ASSET_TEST,
        "        if isinstance(asset, dict):\n",
        ["test_find_asset_returns_the_address_and_the_digest"]),
    mutation(
        "the-asset-search-reads-a-list-item-that-is-not-a-dictionary",
        ASSET_TEST,
        '        if asset.get("name") == INSTALLER_ASSET_NAME:\n',
        ["test_find_asset_refuses_an_answer_without_the_script"]),
    mutation(
        "an-answer-that-is-not-a-release-is-read",
        "    if not isinstance(release, dict):\n",
        "    if False:\n",
        ["test_find_asset_refuses_an_answer_without_the_script"]),
    mutation(
        "an-answer-without-an-asset-list-is-read",
        "    if not isinstance(assets, list):\n",
        "    if False:\n",
        ["test_find_asset_refuses_an_answer_without_the_script"]),
    # --- the request ---------------------------------------------------------
    mutation(
        "an-error-of-another-type-from-the-fetch-is-not-wrapped",
        "    except Exception as exc:\n"
        '        raise UpdateCheckError(f"request failed: {exc}") from exc\n',
        "    except OSError as exc:\n"
        '        raise UpdateCheckError(f"request failed: {exc}") from exc\n',
        ["test_fetch_asset_wraps_an_error_of_another_type_from_the_fetch",
         "test_prepare_turns_an_ordinary_failure_into_the_one_error"]),
    mutation(
        "an-answer-that-is-not-bytes-is-read",
        "    if not isinstance(body, (bytes, bytearray)):\n",
        "    if False:\n",
        ["test_fetch_asset_refuses_an_answer_that_is_not_bytes"]),
    # A survivor at 06c98fdc. The over-limit body in both named tests was
    # refused by a later check anyway: it was not JSON in the fetch test, and
    # its length was not asset.size in the download test. The fetch test now
    # gives a valid release padded with blanks past the limit, and the
    # download test gives an asset whose size equals the over-limit body.
    mutation(
        "an-answer-over-the-limit-is-read",
        "    if len(body) > MAX_RESPONSE_BYTES:\n",
        "    if False:\n",
        ["test_fetch_asset_refuses_an_answer_over_the_limit_from_any_fetch",
         "test_download_over_the_limit_leaves_no_file"]),
    mutation(
        "an-answer-that-is-not-json-is-not-wrapped",
        "    except (UnicodeDecodeError, ValueError, RecursionError) as exc:\n",
        "    except (UnicodeDecodeError, RecursionError) as exc:\n",
        ["test_fetch_asset_refuses_an_answer_it_cannot_read"]),
    mutation(
        "an-answer-nested-too-deeply-is-not-wrapped",
        "    except (UnicodeDecodeError, ValueError, RecursionError) as exc:\n",
        "    except (UnicodeDecodeError, ValueError) as exc:\n",
        ["test_fetch_asset_refuses_an_answer_nested_too_deeply_to_parse"]),
    mutation(
        "the-download-sends-no-running-version",
        "    body = _get(get, asset.url, running_version)\n",
        '    body = _get(get, asset.url, "")\n',
        ["test_download_keeps_the_file_when_the_digest_matches"]),
    # --- the empty body ------------------------------------------------------
    # A survivor at 06c98fdc. The empty body was refused by the exact-length
    # check too, because the test asset had size > 0. The empty check is
    # visible only when asset.size == 0, and the test now builds that asset.
    mutation(
        "an-empty-body-is-not-refused-by-its-own-check",
        '    if not body:\n        raise UpdateCheckError("the installer download is empty")\n',
        "    if not body:\n        pass\n",
        ["test_download_empty_body_is_refused"]),
    # --- the exact-length check ----------------------------------------------
    mutation(
        "the-length-of-the-body-is-never-checked",
        "    if len(body) != asset.size:\n",
        "    if False:\n",
        ["test_download_refuses_a_body_that_is_not_the_size_in_the_answer",
         "test_prepare_refuses_a_cut_download_with_or_without_a_digest"]),
    mutation(
        "only-a-body-shorter-than-the-size-is-refused",
        "    if len(body) != asset.size:\n",
        "    if len(body) < asset.size:\n",
        ["test_download_refuses_a_body_that_is_not_the_size_in_the_answer"]),
    mutation(
        "only-a-body-longer-than-the-size-is-refused",
        "    if len(body) != asset.size:\n",
        "    if len(body) > asset.size:\n",
        ["test_download_refuses_a_body_that_is_not_the_size_in_the_answer",
         "test_prepare_refuses_a_cut_download_with_or_without_a_digest"]),
    # --- the digest comparison -----------------------------------------------
    mutation(
        "the-digest-is-never-compared",
        "        if actual != asset.sha256:\n",
        "        if False:\n",
        ["test_download_raises_on_a_mismatch_and_leaves_no_file",
         "test_prepare_refuses_a_script_that_does_not_match_the_digest"]),
    mutation(
        "the-digest-comparison-is-inverted",
        "        if actual != asset.sha256:\n",
        "        if actual == asset.sha256:\n",
        ["test_download_raises_on_a_mismatch_and_leaves_no_file",
         "test_download_keeps_the_file_when_the_digest_matches"]),
    mutation(
        "the-digest-is-compared-in-upper-case-to-a-lower-case-digest",
        "        actual = hashlib.sha256(body).hexdigest()\n",
        "        actual = hashlib.sha256(body).hexdigest().upper()\n",
        ["test_download_keeps_the_file_when_the_digest_matches",
         "test_prepare_returns_the_path_and_checks_the_digest"]),
    # --- the order of the two checks and the folder --------------------------
    mutation(
        "the-folder-is-made-before-the-empty-check",
        "    if not body:\n",
        "    " + MKDTEMP + "\n    if not body:\n",
        ["test_download_empty_body_is_refused"]),
    mutation(
        "the-folder-is-made-before-the-length-check",
        "    if len(body) != asset.size:\n",
        "    " + MKDTEMP + "\n    if len(body) != asset.size:\n",
        ["test_download_refuses_a_body_that_is_not_the_size_in_the_answer"]),
    mutation(
        "the-folder-is-made-before-the-digest-check",
        "    if asset.sha256 is not None:\n",
        "    " + MKDTEMP + "\n    if asset.sha256 is not None:\n",
        ["test_download_raises_on_a_mismatch_and_leaves_no_file",
         "test_prepare_refuses_a_script_that_does_not_match_the_digest"]),
    # --- the temporary folder ------------------------------------------------
    mutation(
        "the-folder-is-made-in-the-system-folder-not-in-the-parent",
        '        folder = tempfile.mkdtemp(prefix="wheelhouse-update-", dir=parent)\n',
        '        folder = tempfile.mkdtemp(prefix="wheelhouse-update-")\n',
        ["test_download_keeps_the_file_when_the_digest_matches"]),
    mutation(
        "the-folder-is-one-fixed-folder-not-a-new-one",
        '        folder = tempfile.mkdtemp(prefix="wheelhouse-update-", dir=parent)\n',
        "        folder = str(Path(tempfile.gettempdir() if parent is None else parent)"
        ' / "wheelhouse-update-fixed")\n'
        "        Path(folder).mkdir(exist_ok=True)\n",
        ["test_two_downloads_use_two_different_files"]),
    # A survivor at 06c98fdc: no test read the name of the new folder. The
    # named test now asserts the prefix.
    mutation(
        "the-folder-name-has-no-wheelhouse-prefix",
        '        folder = tempfile.mkdtemp(prefix="wheelhouse-update-", dir=parent)\n',
        '        folder = tempfile.mkdtemp(prefix="tmp", dir=parent)\n',
        ["test_download_keeps_the_file_when_the_digest_matches"]),
    # --- the saved file name -------------------------------------------------
    mutation(
        "the-saved-file-has-another-name",
        "        path = Path(folder) / INSTALLER_ASSET_NAME\n",
        '        path = Path(folder) / "installer.ps1"\n',
        ["test_download_keeps_the_file_when_the_digest_matches"]),
    mutation(
        "the-saved-file-is-empty",
        "        path.write_bytes(body)\n",
        '        path.write_bytes(b"")\n',
        ["test_download_keeps_the_file_when_the_digest_matches",
         "test_download_accepts_a_script_given_as_a_bytearray"]),
    # --- the OSError path ----------------------------------------------------
    mutation(
        "an-os-error-is-not-turned-into-the-one-error",
        "    except OSError as exc:\n        if folder is not None:\n",
        "    except ValueError as exc:\n        if folder is not None:\n",
        ["test_download_to_a_folder_that_cannot_be_used_raises_the_one_error",
         "test_download_removes_its_folder_when_the_file_cannot_be_saved"]),
    mutation(
        "the-folder-stays-when-the-file-cannot-be-saved",
        "            shutil.rmtree(folder, ignore_errors=True)\n",
        "            pass\n",
        ["test_download_removes_its_folder_when_the_file_cannot_be_saved"]),
    mutation(
        "the-save-error-does-not-name-the-cause",
        '        raise UpdateCheckError(f"could not save the installer: {exc}") from exc\n',
        '        raise UpdateCheckError("could not save the installer") from exc\n',
        ["test_download_removes_its_folder_when_the_file_cannot_be_saved"]),
    # --- the missing-digest log line -----------------------------------------
    mutation(
        "the-missing-digest-line-is-not-logged",
        '        logger.info("update_install: the release answer has no digest, so none was checked")\n',
        "        pass\n",
        ["test_download_without_a_digest_keeps_the_file_and_logs_one_line"]),
    mutation(
        "the-missing-digest-line-is-logged-for-a-checked-download",
        "    if asset.sha256 is None:\n",
        "    if True:\n",
        ["test_download_with_a_digest_logs_no_missing_digest_line"]),
    # --- the digest_checked answer -------------------------------------------
    mutation(
        "digest-checked-is-always-true",
        "digest_checked=asset.sha256 is not None)",
        "digest_checked=True)",
        ["test_prepare_without_a_digest_says_so_in_the_result"]),
    mutation(
        "digest-checked-is-always-false",
        "digest_checked=asset.sha256 is not None)",
        "digest_checked=False)",
        ["test_prepare_returns_the_path_and_checks_the_digest"]),
    mutation(
        "the-prepared-update-names-the-running-version-as-its-tag",
        "script_path=path, tag=tag,",
        "script_path=path, tag=running_version,",
        ["test_prepare_returns_the_path_and_checks_the_digest"]),
    # --- the engine whitelist --------------------------------------------------
    mutation(
        "an-engine-the-installer-would-reject-is-passed",
        PROVIDER_TEST,
        "        if isinstance(candidate, str):\n",
        ["test_parameters_leave_out_a_provider_the_installer_would_reject"]),
    mutation(
        "the-whitelist-lacks-an-engine-the-installer-accepts",
        'VALID_STT_PROVIDERS = ("parakeet_tdt", "google_stt", "distil_medium_en")\n',
        'VALID_STT_PROVIDERS = ("parakeet_tdt", "google_stt")\n',
        ["test_parameters_match_the_installer_parameter_block"]),
    mutation(
        "the-whitelist-holds-an-engine-the-installer-rejects",
        '"parakeet_tdt", "google_stt", "distil_medium_en")\n',
        '"parakeet_tdt", "google_stt", "distil_medium_en", "whisper")\n',
        ["test_parameters_match_the_installer_parameter_block",
         "test_parameters_leave_out_a_provider_the_installer_would_reject"]),
    mutation(
        "the-engine-is-read-from-another-config-key",
        'STT_PROVIDER_KEY = "stt.last_provider"\n',
        'STT_PROVIDER_KEY = "stt.provider"\n',
        ["test_parameters_ask_the_config_for_the_provider_key_the_app_writes",
         "test_parameters_pass_the_answers_of_the_current_install"]),
    mutation(
        "the-engine-is-passed-under-another-parameter-name",
        '        params += ["-SttProvider", engine]\n',
        '        params += ["-Provider", engine]\n',
        ["test_parameters_pass_the_answers_of_the_current_install"]),
    # A survivor at 06c98fdc. The one test that gave a config with no key
    # asserted only that no warning is logged, not the returned list. A new
    # test asserts the returned list for a config with no engine key. The
    # mutant gives the config read a default engine in place of None.
    mutation(
        "a-missing-engine-key-reads-as-an-engine",
        "lambda: config_service.get(STT_PROVIDER_KEY, None)",
        'lambda: config_service.get(STT_PROVIDER_KEY, "google_stt")',
        ["test_parameters_pass_no_engine_when_the_config_has_no_engine_key",
         "test_no_source_at_all_leaves_the_engine_out_and_logs_one_line"]),
    # --- the warning for an engine the installer would reject ----------------
    # These two change the warning logged when no candidate was valid (the
    # branch "elif rejected:"), which names every rejected name.
    mutation(
        "no-warning-for-an-engine-the-installer-would-reject",
        '        logger.warning("update_install: engine %s not passed to the installer", names)\n',
        "        pass\n",
        ["test_parameters_log_one_warning_for_a_rejected_engine_name",
         "test_neither_source_valid_leaves_the_engine_out_and_logs_one_line"]),
    mutation(
        "the-warning-does-not-name-the-engine",
        '        logger.warning("update_install: engine %s not passed to the installer", names)\n',
        '        logger.warning("update_install: engine not passed to the installer")\n',
        ["test_parameters_log_one_warning_for_a_rejected_engine_name",
         "test_neither_source_valid_leaves_the_engine_out_and_logs_one_line"]),
    # The mutant counts an absent name (None) as a rejected one, so an absent
    # engine, or a valid engine with an absent first candidate, logs a warning.
    mutation(
        "a-warning-for-an-absent-engine",
        "        if candidate is not None:\n            rejected.append(candidate)\n",
        "        rejected.append(candidate)\n",
        ["test_parameters_log_no_warning_when_the_engine_is_absent_or_valid"]),
    # --- the engine of the running Wheelhouse (wh-release-update-check.1.3) --
    # The mutant replaces the running engine with None, so the function reads
    # only the config.
    mutation(
        "the-running-engine-is-ignored",
        "lambda: running_engine,",
        "lambda: None,",
        ["test_a_scalar_stt_config_still_passes_the_running_engine",
         "test_the_running_engine_wins_over_a_different_valid_last_provider",
         "test_an_invalid_running_engine_falls_back_to_a_valid_last_provider",
         "test_neither_source_valid_leaves_the_engine_out_and_logs_one_line",
         "test_a_scalar_stt_config_launches_the_installer_with_the_running_engine"],
        test_file=SUITE_AND_UPDATE_NOW),
    # The mutant reads the config first and the running engine second.
    mutation(
        "the-config-wins-over-the-running-engine",
        ENGINE_ORDER,
        "    for read in (lambda: config_service.get(STT_PROVIDER_KEY, None), "
        "lambda: running_engine):\n",
        ["test_the_running_engine_wins_over_a_different_valid_last_provider",
         "test_an_invalid_running_engine_falls_back_to_a_valid_last_provider"]),
    # The mutant accepts the running engine without the whitelist check, so an
    # invalid running engine (any non-None value) is passed to the installer.
    mutation(
        "an-invalid-running-engine-is-passed",
        PROVIDER_TEST,
        "        if (candidate is not None and candidate is running_engine) or "
        "(isinstance(candidate, str) and candidate in VALID_STT_PROVIDERS):\n",
        ["test_an_invalid_running_engine_falls_back_to_a_valid_last_provider",
         "test_neither_source_valid_leaves_the_engine_out_and_logs_one_line"]),
    # --- the start-at-login answer -------------------------------------------
    mutation(
        "the-start-at-login-answer-is-inverted",
        PARAMS_LINE,
        '    params += ["-StartNow", "yes", "-AutoStart", "no" if auto_start else "yes"]\n',
        ["test_parameters_pass_the_answers_of_the_current_install",
         "test_parameters_pass_no_when_the_start_at_login_shortcut_is_absent"]),
    # The mutant reads the real shortcut, so which test fails depends on the
    # machine: with no shortcut present the explicit True case fails, with
    # one present the explicit False case fails. Only the first is named,
    # because this gate runs where no shortcut exists. Measured 2026-10-09:
    # on this machine the False case passes under the mutant.
    mutation(
        "the-given-start-at-login-answer-is-ignored",
        "    if auto_start is None:\n",
        "    if True:\n",
        ["test_parameters_pass_the_answers_of_the_current_install"]),
    mutation(
        "the-shortcut-is-not-read-when-the-caller-gives-no-answer",
        "        auto_start = parakeet_model.startup_shortcut_exists()\n",
        "        auto_start = True\n",
        ["test_parameters_read_the_shortcut_when_the_caller_gives_no_answer"]),
    # --- the fixed parameters ------------------------------------------------
    mutation(
        "the-installer-is-told-not-to-start-the-app",
        PARAMS_LINE,
        '    params += ["-StartNow", "no", "-AutoStart", "yes" if auto_start else "no"]\n',
        ["test_parameters_pass_the_answers_of_the_current_install",
         "test_parameters_leave_out_a_provider_the_installer_would_reject"]),
    mutation(
        "the-start-now-parameter-is-dropped",
        PARAMS_LINE,
        '    params += ["-AutoStart", "yes" if auto_start else "no"]\n',
        ["test_parameters_pass_the_answers_of_the_current_install"]),
    mutation(
        "the-ai-mode-parameter-is-added",
        PARAMS_LINE,
        '    params += ["-StartNow", "yes", "-AiMode", "keep", "-AutoStart", '
        '"yes" if auto_start else "no"]\n',
        ["test_parameters_pass_the_answers_of_the_current_install",
         "test_parameters_leave_out_a_provider_the_installer_would_reject"]),
]


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
