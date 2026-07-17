"""Package guards for the bring-your-own-LLM help kit (wh-llm-help-kit).

The kit ships two user-facing files: the canonical help payload
`services/wheelhouse/knowledge/wheelhouse_help.md` and the provider-neutral
`scripts/release/public/llm/assistant-instructions.txt`. The txt is GENERATED
from the help doc's embedded "## Instructions for AI Assistant" section --
that section is the single source of the assistant behavior rules (design
decision 9, docs/plans/2026-07-15-llm-help-packaging-design.md). This test
re-derives the expected txt from the help doc independently of the extractor
script, so a drifted or hand-edited txt fails here.

Pure stdlib (pathlib + re only); no service imports, no fixtures.
"""
import re
from pathlib import Path

# tests/ -> repo root is one level up from this file's directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_DOC = (
    _REPO_ROOT / "services" / "wheelhouse" / "knowledge" / "wheelhouse_help.md"
)
_INSTRUCTIONS_TXT = (
    _REPO_ROOT
    / "scripts"
    / "release"
    / "public"
    / "llm"
    / "assistant-instructions.txt"
)

_HEADING = "## Instructions for AI Assistant"


def _embedded_instruction_body() -> str:
    """Extract the instruction-section body from the help doc.

    The section is delimited by its heading line (which must occur exactly
    once) and the first subsequent line that is exactly `---`. Leading and
    trailing blank lines -- including whitespace-only lines -- are stripped;
    the result ends with a single newline. This mirrors the contract of
    scripts/release/extract_assistant_instructions.py, re-derived
    independently so a drifted txt fails here.
    """
    lines = _HELP_DOC.read_text(encoding="utf-8").splitlines()
    heading_count = lines.count(_HEADING)
    assert heading_count == 1, (
        f"expected exactly one '{_HEADING}' heading, found {heading_count}"
    )
    rest = lines[lines.index(_HEADING) + 1 :]
    terminators = [i for i, line in enumerate(rest) if line == "---"]
    assert terminators, "no '---' terminator after the instruction heading"
    body = rest[: terminators[0]]
    while body and not body[0].strip():
        body = body[1:]
    while body and not body[-1].strip():
        body = body[:-1]
    assert body, "instruction section is empty"
    return "\n".join(body) + "\n"


def test_assistant_instructions_txt_exists():
    assert _INSTRUCTIONS_TXT.is_file(), (
        f"assistant-instructions.txt not found at {_INSTRUCTIONS_TXT}. "
        "Generate it from the help doc's embedded instruction section "
        "(see the extraction step in .claude/skills/generate-help-doc/SKILL.md)."
    )


def test_assistant_instructions_txt_matches_embedded_section():
    expected = _embedded_instruction_body()
    actual = _INSTRUCTIONS_TXT.read_text(encoding="utf-8")
    assert actual == expected, (
        "assistant-instructions.txt has drifted from the embedded "
        "'## Instructions for AI Assistant' section of wheelhouse_help.md. "
        "The embedded section is the single source of truth: regenerate the "
        "txt from the doc; never edit the txt directly."
    )


_LLM_DIR = _REPO_ROOT / "scripts" / "release" / "public" / "llm"
_LLM_README = _LLM_DIR / "README.md"
_LANDING_PAGE = _REPO_ROOT / "scripts" / "release" / "public" / "site" / "index.html"

# Anchors the landing page must expose: the section itself plus one
# subsection per supported provider (design decision 10 -- provider setup
# steps live ONLY on the landing page, everything else links to these).
_REQUIRED_ANCHORS = (
    'id="llm-help"',
    'id="llm-chatgpt"',
    'id="llm-gemini"',
    'id="llm-claude"',
    'id="llm-perplexity"',
)

# Placeholder fragments that must never ship in help-kit files. The
# angle-bracket ORG token is spelled as two adjacent string pieces so the
# release export's publish-day placeholder sweep (a raw text scan over
# every exported file, this one included) does not trip on this guard's
# own constant; the runtime value is unchanged.
_PLACEHOLDERS = ("<OR" "G>", "[support channel", "to be updated", "TODO")


def test_help_kit_required_files_exist():
    assert _LLM_README.is_file(), f"missing {_LLM_README}"
    assert _INSTRUCTIONS_TXT.is_file(), f"missing {_INSTRUCTIONS_TXT}"
    assert _HELP_DOC.is_file(), f"missing {_HELP_DOC}"
    assert _LANDING_PAGE.is_file(), f"missing {_LANDING_PAGE}"


def test_landing_page_has_all_provider_anchors():
    html = _LANDING_PAGE.read_text(encoding="utf-8")
    missing = [a for a in _REQUIRED_ANCHORS if a not in html]
    assert not missing, (
        f"landing page is missing LLM help anchors: {missing}. The llm/ "
        "README and the public README link to these anchors, so removing "
        "one breaks published links."
    )


def test_help_kit_files_have_no_placeholders():
    for path in (_LLM_README, _INSTRUCTIONS_TXT, _LANDING_PAGE):
        content = path.read_text(encoding="utf-8")
        offenders = [p for p in _PLACEHOLDERS if p in content]
        assert not offenders, f"{path.name} contains placeholders: {offenders}"


def test_llm_readme_canonical_links_resolve():
    """The llm README's two file links must resolve in the PUBLIC repo
    layout: llm/ sits at the repo root, the canonical help doc ships at
    services/wheelhouse/knowledge/. Map each public-relative link back to
    the dev tree and require the target to exist."""
    content = _LLM_README.read_text(encoding="utf-8")
    assert "(../services/wheelhouse/knowledge/wheelhouse_help.md)" in content, (
        "llm/README.md no longer links the canonical help doc at its "
        "public-repo path (llm/.. -> services/wheelhouse/knowledge/)"
    )
    assert "(./assistant-instructions.txt)" in content, (
        "llm/README.md no longer links assistant-instructions.txt"
    )
    # Public "../services/..." resolves against the repo root; the dev-tree
    # equivalent is _REPO_ROOT / services/... The sibling txt lives in the
    # same folder in both layouts.
    assert (_REPO_ROOT / "services/wheelhouse/knowledge/wheelhouse_help.md").is_file()
    assert (_LLM_DIR / "assistant-instructions.txt").is_file()
    # Every landing-page anchor the README links must exist on the page.
    html = _LANDING_PAGE.read_text(encoding="utf-8")
    for anchor in ("#llm-chatgpt", "#llm-gemini", "#llm-claude", "#llm-perplexity"):
        assert anchor in content, f"llm/README.md dropped the {anchor} link"
        assert f'id="{anchor[1:]}"' in html, f"page lost the {anchor} target"


# The exact public-repo blob URLs the landing page must link. Substring
# checks are not enough: an href on the wrong host or a non-blob URL would
# still contain the repo-relative path (codex review finding
# wh-llm-help-kit.2.5), so these are matched against parsed href values.
_CANONICAL_HELP_URL = (
    "https://github.com/wheelhouse-project/WheelHouse/blob/main/"
    "services/wheelhouse/knowledge/wheelhouse_help.md"
)
_CANONICAL_TXT_URL = (
    "https://github.com/wheelhouse-project/WheelHouse/blob/main/"
    "llm/assistant-instructions.txt"
)

_PUBLIC_README = _REPO_ROOT / "scripts" / "release" / "public" / "README.md"
_SITE_ANCHOR_URLS = tuple(
    f"https://wheelhouse-project.github.io/WheelHouse/#llm-{provider}"
    for provider in ("chatgpt", "gemini", "claude", "perplexity")
)


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def test_landing_page_links_canonical_files():
    """The landing page's two download links must be actual hrefs holding
    the exact canonical blob URLs of the files this package ships."""
    hrefs = _hrefs(_LANDING_PAGE.read_text(encoding="utf-8"))
    missing = [u for u in (_CANONICAL_HELP_URL, _CANONICAL_TXT_URL) if u not in hrefs]
    assert not missing, (
        f"landing page is missing canonical download hrefs: {missing}. "
        f"Present hrefs to GitHub blobs: "
        f"{[h for h in hrefs if 'blob' in h]}"
    )


def test_public_readme_help_kit_links_resolve():
    """The public README publishes the same help-kit links as the llm README:
    the two files, the llm folder, and the four landing-page anchors. Each
    anchor URL must target an id that exists on the landing page."""
    content = _PUBLIC_README.read_text(encoding="utf-8")
    for link in (
        "(./services/wheelhouse/knowledge/wheelhouse_help.md)",
        "(./llm/assistant-instructions.txt)",
        "(./llm/README.md)",
    ):
        assert link in content, f"public README dropped the {link} link"
    html = _LANDING_PAGE.read_text(encoding="utf-8")
    for url in _SITE_ANCHOR_URLS:
        assert url in content, f"public README dropped the {url} link"
        fragment = url.rsplit("#", 1)[1]
        assert f'id="{fragment}"' in html, (
            f"landing page lost the id the public README links: #{fragment}"
        )
