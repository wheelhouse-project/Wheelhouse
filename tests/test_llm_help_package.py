"""Package guards for the one-file LLM help kit and the official GPT files.

The kit ships ONE user-facing file: the canonical help payload
`services/wheelhouse/knowledge/wheelhouse_help.md`, whose embedded
"## Instructions for AI Assistant" section carries the assistant behavior
rules. The 2026-07-17 source-of-truth design
(docs/plans/2026-07-17-help-doc-source-of-truth-design.md) retired the
generated companion `llm/assistant-instructions.txt` and its extractor,
superseding decisions 8 and 9 of the 2026-07-15 packaging design. The llm/
folder instead ships the two files behind the official WheelHouse ChatGPT
GPT -- `gpt-instructions.txt` and `gpt-action-openapi.json` (one GET of the
help doc's raw GitHub URL) -- so anyone can also build their own
live-fetching assistant from them.

Pure stdlib (pathlib + json + re only); no service imports, no fixtures.
"""
import json
import re
from pathlib import Path

# tests/ -> repo root is one level up from this file's directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_DOC = (
    _REPO_ROOT / "services" / "wheelhouse" / "knowledge" / "wheelhouse_help.md"
)
_LLM_DIR = _REPO_ROOT / "scripts" / "release" / "public" / "llm"
_LLM_README = _LLM_DIR / "README.md"
_GPT_INSTRUCTIONS = _LLM_DIR / "gpt-instructions.txt"
_GPT_ACTION_SCHEMA = _LLM_DIR / "gpt-action-openapi.json"
_LANDING_PAGE = _REPO_ROOT / "scripts" / "release" / "public" / "site" / "index.html"
_PUBLIC_README = _REPO_ROOT / "scripts" / "release" / "public" / "README.md"

_HEADING = "## Instructions for AI Assistant"
_RAW_DOC_URL = (
    "https://raw.githubusercontent.com/wheelhouse-project/Wheelhouse/main/"
    "services/wheelhouse/knowledge/wheelhouse_help.md"
)


# ---------------------------------------------------------------------------
# Retirement guards: the generated companion and its machinery must be gone.

_RETIRED_PATHS = (
    _LLM_DIR / "assistant-instructions.txt",
    _REPO_ROOT / "scripts" / "release" / "extract_assistant_instructions.py",
    _REPO_ROOT
    / "scripts"
    / "release"
    / "tests"
    / "test_extract_assistant_instructions.py",
)


def test_assistant_instructions_companion_is_retired():
    leftovers = [str(p.relative_to(_REPO_ROOT)) for p in _RETIRED_PATHS if p.exists()]
    assert not leftovers, (
        f"retired assistant-instructions machinery still present: {leftovers}. "
        "The 2026-07-17 source-of-truth design shrank the kit to one file; "
        "the behavior rules travel only inside the help document."
    )


def test_no_shipped_doc_references_assistant_instructions_txt():
    for path in (_LLM_README, _LANDING_PAGE, _PUBLIC_README):
        content = path.read_text(encoding="utf-8")
        assert "assistant-instructions" not in content, (
            f"{path.name} still references the retired "
            "assistant-instructions.txt; the setup is upload-one-file now."
        )


# ---------------------------------------------------------------------------
# The embedded instruction section is now the ONLY home of the rules.


def test_embedded_instruction_section_present_and_wellformed():
    lines = _HELP_DOC.read_text(encoding="utf-8").splitlines()
    heading_count = lines.count(_HEADING)
    assert heading_count == 1, (
        f"expected exactly one '{_HEADING}' heading, found {heading_count}"
    )
    rest = lines[lines.index(_HEADING) + 1 :]
    terminators = [i for i, line in enumerate(rest) if line == "---"]
    assert terminators, "no '---' terminator after the instruction heading"
    body = "\n".join(rest[: terminators[0]]).strip()
    assert body, "instruction section is empty"
    # Pin the load-bearing directives as whole sentences (on
    # whitespace-normalized text), not lone tokens: a token check stays green
    # when the sentence around it inverts the rule. Editing one of these
    # sentences in the help document is a conscious contract change and must
    # update this test in the same commit.
    norm = " ".join(body.split())
    assert (
        "answer ONLY from this document. Never invent features, commands, or"
        " settings not documented here." in norm
    ), "the grounding rule is missing or weakened in the embedded instructions"
    assert (
        "such as <!-- install-doc:start -->. They are structural markers for"
        " tooling. Ignore them and never mention them." in norm
    ), (
        "the ignore-HTML-comments rule is missing or no longer tells the "
        "assistant to ignore the markers; uploaded copies would mention them"
    )
    assert (
        'read it from the "Generated" line in the footer' in norm
    ), (
        "the version-disclosure rule (report the release from the footer's "
        "Generated line) is missing from the embedded instructions"
    )
    assert (
        'Ignore the footer\'s "Wheelhouse version" line; it is an internal'
        " build identifier." in norm
    ), (
        "the rule to ignore the internal build-identifier footer line is "
        "missing or inverted in the embedded instructions"
    )


# ---------------------------------------------------------------------------
# The official GPT's two files.


def test_gpt_files_exist():
    assert _GPT_INSTRUCTIONS.is_file(), f"missing {_GPT_INSTRUCTIONS}"
    assert _GPT_ACTION_SCHEMA.is_file(), f"missing {_GPT_ACTION_SCHEMA}"


def test_gpt_instructions_contract():
    text = _GPT_INSTRUCTIONS.read_text(encoding="utf-8")
    # Pin each directive as a whole sentence on whitespace-normalized text,
    # not a lone token: a token check stays green when the sentence around it
    # weakens the rule (fetch once instead of every time, refusal clause
    # deleted, version rule inverted). Editing one of these sentences is a
    # conscious contract change and must update this test in the same commit.
    norm = " ".join(text.split())
    # Fetch-first, on every question, via the named Action operation.
    assert (
        "FETCH FIRST, EVERY TIME: before answering any Wheelhouse question,"
        " call the getHelpDocument action" in norm
    ), "the fetch-before-every-answer directive is missing or weakened"
    # Grounding: only the just-fetched document, never memory.
    assert (
        "ONLY from the document you just fetched, never from memory" in norm
    ), "the never-answer-from-memory grounding rule is missing or weakened"
    # Ignore the structural markers visible in the raw markdown.
    assert (
        "such as <!-- install-doc:start -->; they are structural markers for"
        " tooling. Ignore them and never mention them to the user." in norm
    ), "the ignore-HTML-comments directive is missing or weakened"
    # Fetch-failure rule: admit the guide is unreachable AND refuse to
    # answer from memory, in the same directive.
    assert (
        "IF THE FETCH FAILS: tell the user plainly that you cannot reach the"
        " current Wheelhouse guide right now, and do NOT answer"
        " Wheelhouse-specific questions from memory." in norm
    ), "the fetch-failure refusal directive is missing or weakened"
    assert "https://wheelhouse-project.org/" in text
    assert "https://github.com/wheelhouse-project/Wheelhouse" in text
    # Version disclosure: the stamped "Wheelhouse version" footer line names
    # the release the guide describes (the export stamps it at publish time;
    # only the pre-release private tree carries a build identifier there).
    assert (
        'The "Wheelhouse version" line at the very end of the guide names'
        " that release." in norm
    ), "the version-disclosure directive is missing or inverted"
    # Email drafting: never claim mailbox access; mailto links draft only.
    assert (
        "This GPT does not have mailbox access and must never claim that it"
        " can read, search, send, or modify the user's email." in norm
    ), "the no-mailbox-access directive is missing or weakened"
    assert (
        "a mailto link creates a draft only and never sends automatically"
        in norm
    ), "the mailto-never-sends directive is missing or weakened"
    # The paste target is ChatGPT's instructions field: keep it plain ASCII
    # so nothing mangles in transit.
    assert text.isascii(), "gpt-instructions.txt must be plain ASCII"


def test_gpt_action_schema_is_one_get_of_the_raw_doc():
    schema = json.loads(_GPT_ACTION_SCHEMA.read_text(encoding="utf-8"))
    assert str(schema.get("openapi", "")) == "3.1.0", (
        "gpt-action-openapi.json must declare OpenAPI 3.1.0 exactly: the"
        " ChatGPT Actions editor requires a 3.1 schema, so a downgrade to"
        " 3.0.x would be rejected when the Action is configured"
    )
    servers = [s["url"] for s in schema.get("servers", [])]
    assert len(servers) == 1, f"expected exactly one server, got {servers}"
    paths = schema.get("paths", {})
    assert len(paths) == 1, f"expected exactly one path, got {list(paths)}"
    ((path, item),) = paths.items()
    assert list(item.keys()) == ["get"], (
        f"the single path must define exactly one GET, got {list(item.keys())}"
    )
    assert item["get"].get("operationId") == "getHelpDocument"
    assert servers[0].rstrip("/") + path == _RAW_DOC_URL, (
        "server url + path must reassemble to the canonical raw help-doc URL"
    )
    # The response contract: exactly one 200 response returning the raw
    # markdown as a text/plain string. Without these checks, deleting the
    # responses object or swapping the media type would leave the Action
    # declaring a contract ChatGPT no longer receives.
    responses = item["get"].get("responses", {})
    assert list(responses.keys()) == ["200"], (
        f"expected exactly one 200 response, got {list(responses)}"
    )
    content = responses["200"].get("content", {})
    assert list(content.keys()) == ["text/plain"], (
        f"the 200 response must declare exactly text/plain, got {list(content)}"
    )
    assert content["text/plain"].get("schema", {}).get("type") == "string", (
        "the text/plain schema type must be 'string' so the Action hands"
        " ChatGPT the raw markdown document"
    )


# ---------------------------------------------------------------------------
# Kit folder, landing page, and README link integrity.

# Anchors the landing page must expose: the section itself plus one
# subsection per supported provider (provider setup steps live ONLY on the
# landing page; everything else links to these).
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
    assert _GPT_INSTRUCTIONS.is_file(), f"missing {_GPT_INSTRUCTIONS}"
    assert _GPT_ACTION_SCHEMA.is_file(), f"missing {_GPT_ACTION_SCHEMA}"
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
    for path in (_LLM_README, _GPT_INSTRUCTIONS, _GPT_ACTION_SCHEMA, _LANDING_PAGE):
        content = path.read_text(encoding="utf-8")
        offenders = [p for p in _PLACEHOLDERS if p in content]
        assert not offenders, f"{path.name} contains placeholders: {offenders}"


def test_llm_readme_canonical_links_resolve():
    """The llm README's file links must resolve in the PUBLIC repo layout:
    llm/ sits at the repo root, the canonical help doc ships at
    services/wheelhouse/knowledge/. Map each public-relative link back to
    the dev tree and require the target to exist."""
    content = _LLM_README.read_text(encoding="utf-8")
    assert "(../services/wheelhouse/knowledge/wheelhouse_help.md)" in content, (
        "llm/README.md no longer links the canonical help doc at its "
        "public-repo path (llm/.. -> services/wheelhouse/knowledge/)"
    )
    assert "(./gpt-instructions.txt)" in content, (
        "llm/README.md no longer links gpt-instructions.txt"
    )
    assert "(./gpt-action-openapi.json)" in content, (
        "llm/README.md no longer links gpt-action-openapi.json"
    )
    # Public "../services/..." resolves against the repo root; the dev-tree
    # equivalent is _REPO_ROOT / services/... The GPT files live in the
    # same folder in both layouts.
    assert (_REPO_ROOT / "services/wheelhouse/knowledge/wheelhouse_help.md").is_file()
    assert _GPT_INSTRUCTIONS.is_file()
    assert _GPT_ACTION_SCHEMA.is_file()
    # Every landing-page anchor the README links must exist on the page.
    html = _LANDING_PAGE.read_text(encoding="utf-8")
    for anchor in ("#llm-chatgpt", "#llm-gemini", "#llm-claude", "#llm-perplexity"):
        assert anchor in content, f"llm/README.md dropped the {anchor} link"
        assert f'id="{anchor[1:]}"' in html, f"page lost the {anchor} target"


# The exact public-repo blob URL the landing page must link. Substring
# checks are not enough: an href on the wrong host or a non-blob URL would
# still contain the repo-relative path, so this is matched against parsed
# href values.
_CANONICAL_HELP_URL = (
    "https://github.com/wheelhouse-project/Wheelhouse/blob/main/"
    "services/wheelhouse/knowledge/wheelhouse_help.md"
)

_SITE_ANCHOR_URLS = tuple(
    f"https://wheelhouse-project.org/#llm-{provider}"
    for provider in ("chatgpt", "gemini", "claude", "perplexity")
)


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def test_landing_page_links_canonical_help_doc():
    """The landing page's download link must be an actual href holding the
    exact canonical blob URL of the one file the kit ships."""
    hrefs = _hrefs(_LANDING_PAGE.read_text(encoding="utf-8"))
    assert _CANONICAL_HELP_URL in hrefs, (
        f"landing page is missing the canonical help-doc href. Present "
        f"hrefs to GitHub blobs: {[h for h in hrefs if 'blob' in h]}"
    )


def test_public_readme_help_kit_links_resolve():
    """The public README publishes the same help-kit links as the llm README:
    the help document, the llm folder, and the four landing-page anchors.
    Each anchor URL must target an id that exists on the landing page."""
    content = _PUBLIC_README.read_text(encoding="utf-8")
    for link in (
        "(./services/wheelhouse/knowledge/wheelhouse_help.md)",
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
