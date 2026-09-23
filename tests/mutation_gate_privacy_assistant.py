"""Mutation gate for the published privacy notice's assistant guards.

Written for finding wh-gem-replaces-gpt-assistant.2.1: the release export
shipped `scripts/release/public/PRIVACY.md` still describing the hosted
assistant as the retired ChatGPT custom GPT, with an OpenAI Action and a
GitHub fetch, and naming OpenAI as the processor of the conversation. No
test caught it, because the one scan that covered the whole public overlay
looked only for the retired GPT's web address, and the privacy notice named
the GPT in prose without ever giving that address.

The two guards added with the fix live in
`tests/test_llm_help_package.py`:

  test_no_shipped_file_presents_the_assistant_as_the_retired_gpt
  test_privacy_notice_names_google_as_the_assistant_processor

Each mutation below breaks exactly one thing those guards protect. A
mutation counts as caught only when the named test fails AND the expected
assertion text appears in the output. A red for any other reason proves
nothing, so this gate reports it as an error, not as a catch.

Run it from the repository root:

    python tests/mutation_gate_privacy_assistant.py

It exits 0 only when every mutation is caught. Each mutation names the
file it edits, and the gate restores that file with write_bytes in a
finally block. At the end it refuses to report success unless BOTH target
files match their original bytes.
"""
import os
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

# tests/ -> repo root is one level up from this file's directory.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = _REPO_ROOT / "scripts" / "release" / "public" / "PRIVACY.md"
# The second target, added for finding wh-gem-replaces-gpt-assistant.2.2:
# the unreleased changelog entry told an updating user that the assistant
# runs inside ChatGPT and needs a ChatGPT account, and the first version of
# the phrase guard reported green over it.
CHANGELOG = _REPO_ROOT / "scripts" / "release" / "public" / "CHANGELOG.md"
TESTS = _REPO_ROOT / "tests" / "test_llm_help_package.py"
# The guards are pure stdlib, but pytest itself comes from this service's
# environment; the file has no service imports.
SERVICE = _REPO_ROOT / "services" / "wheelhouse"

SCAN_TEST = "test_no_shipped_file_presents_the_assistant_as_the_retired_gpt"
PROC_TEST = "test_privacy_notice_names_google_as_the_assistant_processor"

MUTATIONS = [
    {
        "name": "the-action-prose-returns",
        "old": "The assistant retrieves nothing while it answers.",
        "new": (
            "Before answering, the assistant uses an OpenAI Action to"
            " retrieve the documentation."
        ),
        "test": SCAN_TEST,
        "expect": "openai action",
    },
    {
        "name": "the-github-fetch-returns",
        "old": "It makes no request to the Wheelhouse project, to GitHub,",
        "new": (
            "It requests the file from `raw.githubusercontent.com`, not"
            " from the Wheelhouse project, to GitHub,"
        ),
        "test": SCAN_TEST,
        "expect": "raw.githubusercontent.com",
    },
    {
        "name": "the-retired-assistant-name-returns",
        "old": "# Wheelhouse Assistant\n",
        "new": "# Wheelhouse Help GPT\n",
        "test": SCAN_TEST,
        "expect": "wheelhouse help gpt",
    },
    {
        "name": "the-gem-address-goes-missing",
        "old": (
            "at <https://gemini.google.com/gem/"
            "1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A>"
        ),
        "new": "in the Gemini app",
        "test": PROC_TEST,
        "expect": "does not give the assistant's address",
    },
    {
        "name": "the-processor-goes-unnamed",
        "old": (
            "are processed by Google as part of providing the Gemini service"
        ),
        "new": "are processed as part of providing the Gemini service",
        "test": PROC_TEST,
        "expect": "does not say who processes a conversation",
    },
    {
        "name": "the-assistants-home-returns-to-chatgpt",
        "target": "changelog",
        "old": (
            "the Wheelhouse Assistant runs inside Google Gemini and that"
            " Gemini"
        ),
        "new": "the Wheelhouse Assistant runs inside ChatGPT and that ChatGPT",
        "test": SCAN_TEST,
        "expect": "inside chatgpt",
    },
    {
        "name": "the-wrong-account-returns-wrapped-across-two-lines",
        # The line wrap is the point. The sentence that actually shipped
        # split "needs a ChatGPT account" across a line break, so a raw
        # substring search could not have matched it however complete the
        # phrase list was. This mutation reproduces that shape, and it is
        # caught only because the scan collapses whitespace first.
        "target": "changelog",
        "old": (
            "asks you to sign in first; a free account works, and a Google"
            " account, an\n  Apple account, or an email address will do."
        ),
        "new": (
            "asks you to sign in first. You need a ChatGPT\n  account;"
            " a free account works."
        ),
        "test": SCAN_TEST,
        "expect": "chatgpt account",
    },
    {
        "name": "openai-returns-as-the-processor",
        # This mutation KEEPS the Google sentence on purpose. The guard
        # asserts the Google sentence first, so a mutation that broke both
        # would stop at the first assertion and never reach the banned
        # phrase it claims to prove.
        "old": "Read those before you dictate anything sensitive",
        "new": (
            "Some conversations are processed by OpenAI instead. Read those"
            " before you dictate anything sensitive"
        ),
        "test": PROC_TEST,
        "expect": "still says OpenAI processes the",
    },
]


def run_tests(selection: str) -> tuple[int, str]:
    """Run the named guards and return the exit code and combined output."""
    env = dict(os.environ)
    # A mutation can leave the file the same length, so cached bytecode of
    # the unmutated read could otherwise be reused.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # pytest cuts the reason off a short-summary line narrower than the
    # terminal, and a captured run has no terminal.
    env["COLUMNS"] = "1000"
    proc = subprocess.run(
        [
            "uv", "run", "--project", ".", "python", "-m", "pytest",
            str(TESTS), "-k", selection, "-q", "-rf", "-p", "no:randomly",
        ],
        cwd=str(SERVICE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    original = TARGET.read_bytes()
    changelog_original = CHANGELOG.read_bytes()
    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    baseline_rc, baseline_out = run_tests(f"{SCAN_TEST} or {PROC_TEST}")
    if baseline_rc != 0:
        print("BASELINE IS RED -- refusing to start")
        print(baseline_out[-2000:])
        return 1
    print(f"baseline green: {baseline_out.strip().splitlines()[-1]}")
    print(f"running {len(MUTATIONS)} of {len(MUTATIONS)} mutations")

    errors: list[str] = []
    caught: list[str] = []
    survived: list[str] = []
    sources = {
        "privacy": (TARGET, original),
        "changelog": (CHANGELOG, changelog_original),
    }
    for mut in MUTATIONS:
        path, source_bytes = sources[mut.get("target", "privacy")]
        text = source_bytes.decode("utf-8")
        newline = "\r\n" if b"\r\n" in source_bytes else "\n"
        old = mut["old"].replace("\n", newline)
        count = text.count(old)
        if count != 1:
            errors.append(f"{mut['name']}: pattern matched {count} times")
            print(f"[error]    {mut['name']}: pattern matched {count} times")
            continue
        mutated = text.replace(old, mut["new"].replace("\n", newline), 1)
        try:
            path.write_bytes(mutated.encode("utf-8"))
            rc, out = run_tests(mut["test"])
        finally:
            path.write_bytes(source_bytes)
        if rc == 0:
            survived.append(mut["name"])
            print(f"[SURVIVED] {mut['name']}")
        elif mut["expect"] in out:
            caught.append(mut["name"])
            print(f"[caught]   {mut['name']} -- {mut['expect']!r} fired")
        else:
            errors.append(f"{mut['name']}: red, but the expected text is absent")
            print(f"[error]    {mut['name']}: red for the wrong reason")
            print(out[-1500:])

    print(
        f"\nSUMMARY: ran {len(MUTATIONS)} of {len(MUTATIONS)};"
        f" caught {len(caught)}, survived {len(survived)},"
        f" errors {len(errors)}"
    )
    if TARGET.read_bytes() != original:
        print(f"{TARGET} NOT RESTORED -- read it before committing anything")
        return 1
    if CHANGELOG.read_bytes() != changelog_original:
        print(f"{CHANGELOG} NOT RESTORED -- read it before committing anything")
        return 1
    return 1 if (survived or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
