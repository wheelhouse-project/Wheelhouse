"""Package guards for the LLM help kit and the official assistant's file.

The kit ships the canonical help payload
`services/wheelhouse/knowledge/wheelhouse_help.md`, whose embedded
"## Instructions for AI Assistant" section carries the assistant behavior
rules, plus the separate command and configuration reference
`services/wheelhouse/knowledge/wheelhouse_reference.md`. The two-file build
(wh-helpdoc-migration.3.1) moved the exhaustive command and setting tables
out of the size-limited help document into that reference, so the embedded
rules ground the assistant in both documents. The 2026-07-17 source-of-truth
design (docs/plans/2026-07-17-help-doc-source-of-truth-design.md) retired the
generated companion `llm/assistant-instructions.txt` and its extractor,
superseding decisions 8 and 9 of the 2026-07-15 packaging design.

The llm/ folder now ships ONE assistant file: `gem-instructions.txt`, the
instruction text behind the official Wheelhouse Assistant, which moved from a
ChatGPT custom GPT to a Google Gemini Gem on 2026-09-22 because OpenAI stops
running custom GPTs on 2026-12-11 (wh-gem-replaces-gpt-assistant). The Gem's
knowledge is STORED -- the three documents are attached as files -- so the
assistant no longer fetches anything and no Action schema exists. The two
files that shipped before, `gpt-instructions.txt` and
`gpt-action-openapi.json`, are deleted; the guards that protected
fetch behavior, Action operations, and ChatGPT's own email draft card went
with them, each with a comment at the point of deletion.

Pure stdlib (pathlib + re only); no service imports, no fixtures.
"""
import re
from pathlib import Path

# tests/ -> repo root is one level up from this file's directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_DOC = (
    _REPO_ROOT / "services" / "wheelhouse" / "knowledge" / "wheelhouse_help.md"
)
_PUBLIC_DIR = _REPO_ROOT / "scripts" / "release" / "public"
_LLM_DIR = _PUBLIC_DIR / "llm"
_LLM_README = _LLM_DIR / "README.md"
_GEM_INSTRUCTIONS = _LLM_DIR / "gem-instructions.txt"
# The website page that carries the "Using the documentation with an AI
# assistant" section, its llm-* anchors, and the canonical help-doc link.
# It was index.html until the six-page site (wh-site-redesign-adopt) moved
# the section to help.html.
_HELP_PAGE = _PUBLIC_DIR / "site" / "help.html"
_PUBLIC_README = _PUBLIC_DIR / "README.md"
# The published privacy notice. Every site page footer links it, and
# public/README.md tells the reader it "states exactly what each one
# sends", so it is the document a user reads before deciding what to
# dictate to the assistant.
_PRIVACY_NOTICE = _PUBLIC_DIR / "PRIVACY.md"

_HEADING = "## Instructions for AI Assistant"
# The project's support address. A markdown code span is the only form of it
# that survives a chat assistant's renderer: it renders as plain text, and no
# renderer turns it into a link. Every other form was tried on the live
# ChatGPT GPT and failed -- see
# test_gem_instructions_never_give_the_support_address_as_bare_text. The
# finding is about renderers in general, not about ChatGPT, so it carries to
# the Gem unchanged.
_SUPPORT_ADDRESS = "help@wheelhouse-project.org"
_ADDRESS_CODE_SPAN = "`help@wheelhouse-project.org`"

# The official assistant's public address, named in both shipped READMEs.
_GEM_URL = "https://gemini.google.com/gem/1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A"

# The retired ChatGPT GPT's public address. Spelled as two adjacent string
# pieces for the same reason as the _PLACEHOLDERS constant below: the release
# export scans every exported file as raw text, this one included, so a
# literal here could be mistaken for a live reference to the dead GPT. The
# runtime value is unchanged.
_RETIRED_GPT_URL_FRAGMENT = "chatgpt.com/g/g-6a5ab9206" "8d0819198db2a83135b9540"

# The three GPT Action operation names. The Gem has no Actions, so naming one
# in its instructions would tell the assistant to call something that does not
# exist.
_RETIRED_ACTION_OPERATIONS = (
    "getHelpDocument",
    "getInstallGuide",
    "getCommandReference",
)

# Phrases that can only present the hosted assistant as the retired ChatGPT
# custom GPT. The bare words "custom GPT" are NOT here on purpose: the
# changelog records the retirement as history, and the help page tells a
# reader who wants ChatGPT to make a Project and NOT a custom GPT. Both uses
# are correct. Neither is the changelog's own history of the retired
# assistant ("a ChatGPT assistant that always answers...", "build a ChatGPT
# GPT..."), which describes a past release and must stay as written. Every
# phrase here has no correct use in a shipped file.
_RETIRED_ASSISTANT_PHRASES = (
    "wheelhouse help gpt",
    "openai action",
    "raw.githubusercontent.com",
    # The assistant's own home. It runs inside Google Gemini.
    "inside chatgpt",
    # The account a reader is told to get. Gemini takes a Google account, an
    # Apple account, or an email address.
    "chatgpt account",
)

# The first changelog heading with a real date, "## [x.y.z] - YYYY-MM-DD".
# Everything from it down is a released section.
_RELEASED_CHANGELOG_HEADING = re.compile(
    r"^## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}[ \t]*\r?$", re.MULTILINE
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
    # Deleted 2026-09-22 with the move to the Gem
    # (wh-gem-replaces-gpt-assistant): the GPT's instruction text and its
    # Action schema. A Gem stores its knowledge instead of fetching it, so
    # the schema has no meaning on it, and OpenAI stops running custom GPTs
    # on 2026-12-11.
    _LLM_DIR / "gpt-instructions.txt",
    _LLM_DIR / "gpt-action-openapi.json",
)


def test_assistant_instructions_companion_is_retired():
    leftovers = [str(p.relative_to(_REPO_ROOT)) for p in _RETIRED_PATHS if p.exists()]
    assert not leftovers, (
        f"retired assistant-instructions machinery still present: {leftovers}. "
        "The 2026-07-17 source-of-truth design retired the separate companion "
        "text; the behavior rules travel only inside the help document. The "
        "two gpt-* files were retired on 2026-09-22 with the move to the Gem."
    )


def test_no_shipped_doc_references_assistant_instructions_txt():
    # Matches the retired FILE name. The six-page site's help.html uses the
    # CSS class "assistant-instructions" for its own section, which is not a
    # reference to the file (wh-site-redesign-adopt).
    for path in (_LLM_README, _HELP_PAGE, _PUBLIC_README):
        content = path.read_text(encoding="utf-8")
        assert "assistant-instructions.txt" not in content, (
            f"{path.name} still references the retired "
            "assistant-instructions.txt; the behavior rules are embedded in "
            "the help document now."
        )


def test_no_shipped_doc_references_the_deleted_gpt_files():
    """Nothing shipped may point at the two deleted GPT files.

    A README link to a file the export no longer copies is a dead link in
    the published repository, and the manifest's post-condition check would
    not catch it: the manifest only asserts what IS present.
    """
    for path in (_LLM_README, _HELP_PAGE, _PUBLIC_README):
        content = path.read_text(encoding="utf-8")
        for name in ("gpt-instructions.txt", "gpt-action-openapi.json"):
            assert name not in content, (
                f"{path.name} still references {name}, which was deleted on "
                "2026-09-22 with the move to the Gemini Gem; the link would "
                "be dead in the published repository."
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
        "answer only from the Wheelhouse documents provided to you" in norm
    ), (
        "the grounding rule no longer grounds answers in the provided "
        "Wheelhouse documents"
    )
    assert (
        "the separate Wheelhouse command and configuration reference" in norm
    ), (
        "the embedded instructions no longer point the assistant at the "
        "separate command and configuration reference; the two-file upload "
        "setup would refuse detailed command and setting questions"
    )
    assert (
        "use the command and configuration reference when it is available"
        in norm
    ), (
        "the rule routing command and configuration lookups to the reference "
        "is missing from the embedded instructions"
    )
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


def test_help_document_gives_the_support_address_only_in_a_code_span():
    # The help document is read by more than the official assistant: people
    # load it into ChatGPT, Gemini, Claude and Perplexity, and those
    # assistants copy the address out of it into their answers. A bare
    # address copied into an answer becomes a link that opens a browser tab
    # with no content, which is the defect that started this work.
    # Instructing the official assistant not to copy the bare form fixes only
    # that assistant; putting the code span in the document fixes every
    # reader of it.
    #
    # Nothing clickable is lost. The assembled document's own occurrences of
    # the address already render as plain text on the website and on GitHub,
    # because neither renderer auto-links a bare address in body prose. The
    # website shell's separate mailto links are not part of this document and
    # are untouched.
    #
    # The document is a BUILD PRODUCT. The prose lives in
    # knowledge/helpdoc/sections/*.md; this test reads the built artifact so
    # it fails if a source edit is made without rebuilding.
    text = _HELP_DOC.read_text(encoding="utf-8")
    assert _ADDRESS_CODE_SPAN in text, (
        "the help document no longer gives the support address in a code span"
    )
    assert _SUPPORT_ADDRESS not in text.replace(_ADDRESS_CODE_SPAN, ""), (
        f"the help document gives {_SUPPORT_ADDRESS} as a bare address"
        " somewhere outside a code span. Fix the prose in"
        " services/wheelhouse/knowledge/helpdoc/sections/, then rebuild with"
        " scripts/release/build_helpdoc.py -- editing the built document"
        " directly fails the rebuild-and-compare check"
    )


# ---------------------------------------------------------------------------
# The official assistant's one file.


def test_gem_instructions_file_exists():
    # Was test_gpt_files_exist, which also required gpt-action-openapi.json.
    # DELETED with that file: a Gem has no Actions, so there is no Action
    # schema to ship or to validate.
    assert _GEM_INSTRUCTIONS.is_file(), f"missing {_GEM_INSTRUCTIONS}"


def test_gem_instructions_contract():
    text = _GEM_INSTRUCTIONS.read_text(encoding="utf-8")
    # Pin each directive as a whole sentence on whitespace-normalized text,
    # not a lone token: a token check stays green when the sentence around it
    # weakens the rule (the grounding rule softened, refusal clause deleted,
    # version rule inverted). Editing one of these sentences is a conscious
    # contract change and must update this test in the same commit.
    norm = " ".join(text.split())

    # DELETED with the GPT: a length guard pinned at 8000 characters, because
    # the ChatGPT builder REJECTS an instruction file over that length. The
    # Gem builder is a different product and no measured limit for it is
    # recorded anywhere in this repository, so re-pinning a number here would
    # be a guess. The file is 7,092 characters today; if a Gem limit is ever
    # measured, restore the guard with that number and cite the measurement.

    # Grounding. The Gem's knowledge is STORED, not fetched: the three
    # documents are attached files. This directive replaces the GPT's
    # "FETCH FIRST, EVERY TIME", and it carries the same load -- every
    # Wheelhouse fact comes from the documents and never from memory.
    assert (
        "YOUR KNOWLEDGE FILES ARE THE ONLY SOURCE OF WHEELHOUSE FACTS." in norm
    ), "the grounding directive is missing or weakened"
    assert (
        "Answer these only from the knowledge files. Never state a Wheelhouse"
        " fact from memory, even when you are sure of it." in norm
    ), "the never-answer-a-Wheelhouse-question-from-memory rule is missing"

    # Routing. The GPT routed by Action name (getCommandReference,
    # getHelpDocument, getInstallGuide); the Gem routes by document heading,
    # because it looks the document up in its Knowledge section instead of
    # fetching it. Same three destinations, same three subjects.
    assert (
        '"Wheelhouse Voice Command and Configuration Reference" -- the exact'
        " wording of every voice command, every configuration key, and every"
        " default value." in norm
    ), "the reference document is no longer described as the command/config source"
    assert (
        '"Wheelhouse Installation Guide" -- installing, upgrading,'
        " uninstalling, and installer troubleshooting." in norm
    ), "installation questions no longer route to the separate installation guide"
    assert (
        '"Wheelhouse Help Document" -- everything else: what Wheelhouse is,'
        " getting started, hardware, speech engines, concepts, and"
        " troubleshooting." in norm
    ), "the route-every-other-question-to-the-guide directive is missing"

    # Ignore the structural markers visible in the raw markdown.
    assert (
        "Never mention the documents' own formatting -- table of contents"
        " lines, anchor links, section numbers, or comment markers such as"
        ' "<!-- install-doc:start -->". Describe only what the documentation'
        " says." in norm
    ), "the ignore-document-formatting directive is missing or weakened"

    # Unreadable-knowledge rule: admit the documentation is unreachable AND
    # refuse to answer from memory, in the same directive. Adapted from the
    # GPT's "IF THE FETCH FAILS"; the failure mode moved from a failed HTTP
    # GET to a missing or unreadable attachment, but the required behavior is
    # identical.
    assert (
        "IF YOUR KNOWLEDGE FILES ARE MISSING OR YOU CANNOT READ THEM: tell the"
        " user plainly that you cannot reach the Wheelhouse documentation, and"
        " do NOT answer Wheelhouse questions from memory." in norm
    ), "the unreadable-knowledge refusal directive is missing or weakened"
    # That refusal stops Wheelhouse answers and nothing else. Without this
    # line the block reads as a general refusal, and a user whose question has
    # a Windows half gets nothing for that half either.
    assert (
        "This stops Wheelhouse answers only; keep answering every other kind"
        " of question from your own knowledge." in norm
    ), "the unreadable-knowledge block is no longer limited to Wheelhouse answers"

    assert "https://wheelhouse-project.org/" in text
    assert "https://github.com/wheelhouse-project/Wheelhouse" in text

    # Version disclosure: each document names the release it describes in its
    # "Generated ... for the vX.Y.Z release" footer line. The guide's footer
    # also has a "Wheelhouse version" line, which in development copies holds
    # an internal build identifier (for example backup/dev/20251127-...). The
    # guide's own embedded instructions say the same
    # (services/wheelhouse/knowledge/wheelhouse_help.md, "Instructions for AI
    # Assistant"); this file once named the wrong line and contradicted them
    # (wh-gpt-version-line-wrong). The Gem states it in every Wheelhouse
    # answer, in a fixed "Source:" line, rather than only when the answer
    # could depend on the version.
    assert (
        "SAY WHERE THE ANSWER CAME FROM AND WHICH RELEASE IT DESCRIBES. End"
        " every answer to a Wheelhouse question with one line in this form:"
        in norm
    ), "the version-disclosure directive is missing or inverted"
    assert (
        "Source: the user guide, which describes Wheelhouse v1.1.0." in norm
    ), "the example Source line is gone; the required form is no longer shown"
    assert (
        "Name the document or documents you actually read for that answer."
        in norm
    ), "the Source line no longer has to name the document that was read"
    assert (
        'Take the release number from the "Generated" line at the end of each'
        ' document, which reads "Generated: <date> for the vX.Y.Z release".'
        in norm
    ), "the directive no longer names the Generated footer line as the source"
    # The public export stamps that line with the release number
    # (scripts/release/manifest.toml, the wheelhouse_help.md [[sanitize]]
    # stamp), and the assistant reads the public copy, so the reason clause
    # must hold for both copies: only development copies carry the identifier.
    assert (
        'Ignore the separate "Wheelhouse version" line; in development copies'
        " it holds an internal build identifier." in norm
    ), "the directive no longer tells the assistant to ignore the build identifier"

    # Email. The address form, the order a help answer puts its options in,
    # and what the assistant may claim about sending mail are pinned in the
    # tests below this one.
    assert (
        "THE SUPPORT EMAIL ADDRESS: write it only inside a code span, copied"
        " character for character from this line:" in norm
    ), "the code-span-only rule for the support address is missing or weakened"
    assert (
        "Never write it as a mailto: link." in norm
    ), "the no-mailto-link rule is missing or weakened"

    # The answer policy splits by question kind. Wheelhouse facts stay
    # grounded in the three documents, then a refusal. Everything else --
    # Windows, microphones, speech recognition in general, ordinary computer
    # questions -- is answered from the model's own knowledge and must never
    # draw the refusal script.
    assert (
        "TWO KINDS OF QUESTION: decide which you are answering before"
        " anything else." in norm
    ), "the two-kinds-of-question classification is missing"
    assert (
        "Answer these fully from your own knowledge." in norm
    ), "the answer-general-questions-from-your-own-knowledge directive is missing"
    assert (
        "never tell the user you have no information about them" in norm
    ), "the do-not-refuse-general-questions directive is missing or weakened"
    # A question the model cannot classify must not fall down the
    # look-then-refuse path and end in "I don't have information about that"
    # when the model could have answered it.
    assert (
        "If you cannot tell which kind a question is, look in the documents."
        in norm
    ), "the cannot-classify fallback is missing"
    assert (
        "If none of them covers it, answer from your own knowledge and say so;"
        " do not use the refusal script, which is for questions you have"
        " established are about Wheelhouse." in norm
    ), "the cannot-classify miss no longer routes away from the refusal script"
    assert (
        "Refusing is the wrong answer to a question you can answer." in norm
    ), "the do-not-refuse-what-you-know directive is missing or weakened"
    # The audience split describes who is asking, not what may be answered
    # from memory. Without this scope line a current user's Windows question
    # draws "answer from the documentation".
    assert (
        "a question of the other kind is answered from your own knowledge"
        " whoever asks" in norm
    ), "the audience section is no longer scoped to Wheelhouse questions"

    # DELETED with the GPT: the GitHub-repository-search fallback and its
    # four supporting guards -- the search directive itself, the
    # "only after you have read all three" precondition, the FETCH FIRST
    # pointer to it, the "Do not search the GitHub repository instead:"
    # clause in the fetch-failure block, and the
    # "never present unreleased work as current behavior" caution. The Gem's
    # instructions contain no repository-search step at all: a Gem cannot
    # browse, so there is nothing to scope, order, or caution about. The
    # refusal script is now the step straight after reading all three
    # documents, which the ordering guard below pins.

    # Reading every document before declaring something missing. The GPT had
    # to fetch the remaining documents; the Gem has to read them.
    assert (
        "SEARCH EVERY DOCUMENT BEFORE YOU SAY SOMETHING IS MISSING." in norm
    ), "the read-every-document-first directive is missing"
    assert (
        "When it does not answer the question, read the other two before doing"
        " anything else." in norm
    ), "the read-the-other-documents transition is missing"
    # DELETED with fetching: "If a fetch fails, take IF THE FETCH FAILS." --
    # the route out of the two-document transition when an HTTP GET failed.
    # A stored document does not fail partway through a transition; the
    # missing-knowledge-files rule above covers the whole session instead.

    # A question with a separable general part belongs to the mixed-question
    # rule, not the cannot-classify branch. Without this precedence, the
    # file's own mixed example ("my microphone keeps cutting out while I
    # dictate") reaches the branch, and a document hit there would collapse it
    # into a Wheelhouse-only answer that drops the general half.
    # The precedence must be limited to a part the model can answer WITHOUT
    # knowing anything about Wheelhouse. Its earlier form fired on any
    # question that merely "has a part" outside Wheelhouse, which let a real
    # question -- "which Windows microphone setting lets Wheelhouse hear me"
    # -- be answered from memory, and answering it asserts which permission
    # governs Wheelhouse's audio capture. That is a Wheelhouse fact, and the
    # grounding rule forbids supplying one from memory.
    assert (
        "That rule comes first: whenever a question has a general part you can"
        " answer on its own, answer that part from your own knowledge no"
        " matter what the rest of this section says." in norm
    ), "the mixed-question rule no longer takes precedence, or is unscoped"
    assert (
        "A part is not answerable on its own when answering it requires"
        " knowing something about Wheelhouse" in norm
    ), "the dependency limit on the precedence rule is gone"
    assert (
        "Read that fact from the documents; never supply it from memory."
        in norm
    ), "a Wheelhouse fact a general answer depends on may come from memory"

    # The refusal script, and its scope.
    assert (
        "WHEN THE DOCUMENTS DO NOT ANSWER A WHEELHOUSE QUESTION, and you have"
        " looked in all three, say this:" in norm
    ), "the refusal script no longer reads as the step after reading all three"
    assert (
        "Use that refusal only for questions about Wheelhouse itself;"
        " anything outside Wheelhouse is answered from your own knowledge."
        in norm
    ), "the refusal script is no longer limited to Wheelhouse questions"
    # Order matters: a refusal script placed before the read-everything
    # directive would let the assistant give up without opening the other two
    # documents. This is the adapted form of the old
    # search-before-refusal ordering guard.
    assert norm.index(
        "SEARCH EVERY DOCUMENT BEFORE YOU SAY SOMETHING IS MISSING."
    ) < norm.index(
        "WHEN THE DOCUMENTS DO NOT ANSWER A WHEELHOUSE QUESTION,"
    ), (
        "the refusal script comes before the read-every-document directive;"
        " the assistant would refuse without opening the other documents"
    )

    # Usability rules the file carries for a first-time user.
    assert (
        "When describing a voice command, always give an example of what to"
        " say." in norm
    ), "the give-an-example rule for voice commands is missing"
    assert (
        "If someone seems overwhelmed, point them at the guide's \"Day 1 Quick"
        " Start\" section and say to ignore everything else for now." in norm
    ), "the Day 1 Quick Start pointer is missing"

    # The last rule in the file, and the one with the most direct user harm:
    # a plausible invented command or config key wastes the user's time and
    # cannot be told from a real one by reading the answer.
    assert (
        "NEVER INVENT A VOICE COMMAND, A CONFIGURATION KEY, OR A DEFAULT"
        " VALUE. Every one of them must be copied from the reference document,"
        " character for character." in norm
    ), "the do-not-invent-commands-keys-or-defaults rule is missing or weakened"
    assert (
        "If the exact wording is not there, say it is not there rather than"
        " guessing a plausible form." in norm
    ), "the say-it-is-not-there half of the do-not-invent rule is gone"

    # The paste target is the Gem builder's instructions field: keep it plain
    # ASCII so nothing mangles in transit.
    assert text.isascii(), "gem-instructions.txt must be plain ASCII"


def test_gem_instructions_name_no_gpt_action():
    """The Gem has no Actions, so it must not be told to call one.

    The GPT's three Action operations were the only way it reached a
    document. Leaving one of those names in the instruction text tells the
    assistant to call a tool that does not exist, which is a failure it
    cannot recover from and cannot report usefully.
    """
    text = _GEM_INSTRUCTIONS.read_text(encoding="utf-8")
    named = [op for op in _RETIRED_ACTION_OPERATIONS if op in text]
    assert not named, (
        f"gem-instructions.txt names retired GPT Action operations: {named}."
        " A Gem reads attached documents; it has no Actions to call."
    )


def test_gem_instructions_never_give_the_support_address_as_bare_text():
    # Four observations on the live ChatGPT GPT, in order. The first three
    # are from 2026-08-02, the fourth from 2026-08-03:
    #   1. A bare address renders as a link to a blank browser page.
    #   2. A markdown link with a mailto: destination does not open a mail
    #      program either. ChatGPT stops it at an "External site" dialog
    #      showing the whole percent-encoded URI, and its "Open link" button
    #      opens a browser tab with no content.
    #   3. The ChatGPT draft card's Send button does send the message.
    #   4. The draft card is ChatGPT's OWN email feature reacting to
    #      draft-shaped text, not its rendering of the mailto link.
    # A code span is the only form that survives: it renders as plain text and
    # no renderer turns it into a link. That is a property of markdown
    # rendering, not of ChatGPT, so it still holds on the Gem, and the Gem
    # instructions still carry the rule.
    text = _GEM_INSTRUCTIONS.read_text(encoding="utf-8")
    assert _ADDRESS_CODE_SPAN in text, (
        "the code-span form of the support address is missing; it is the only"
        " form that reliably renders as plain text"
    )
    # No mailto: URI in any form. An earlier version of this test pinned two
    # exact link strings, which would have stayed green against a third link
    # written differently. Banning the scheme outright cannot be sidestepped.
    # The word appears exactly once in the file, in the sentence forbidding
    # it; a second occurrence is a link. Counting rather than subtracting the
    # known sentence keeps the check independent of where the line wraps.
    assert text.count("mailto:") == 1, (
        f'"mailto:" appears {text.count("mailto:")} times in'
        " gem-instructions.txt; it belongs only in the sentence forbidding"
        " it. Clicking a mailto link in a chat answer opens a browser tab"
        " with no content -- verified twice on the live assistant, with two"
        " different link forms"
    )
    assert "](mailto:" not in text, (
        "gem-instructions.txt builds a markdown link with a mailto:"
        " destination again; that is the exact form that was verified dead"
    )
    # Every literal occurrence of the address must sit inside a code span.
    # Removing the code span leaves no bare address anywhere in the file --
    # including the refusal script the assistant is told to say word for word,
    # which is where the first broken link came from.
    assert _SUPPORT_ADDRESS not in text.replace(_ADDRESS_CODE_SPAN, ""), (
        f"gem-instructions.txt still gives {_SUPPORT_ADDRESS} as a bare"
        " address somewhere outside a code span; a chat renderer turns a bare"
        " address into a link that opens a blank page"
    )
    norm = " ".join(text.split())
    # The attached help document gives the address in prose, and the
    # assistant reads that document on every Wheelhouse question. Copying the
    # address out of it is the exact path the first broken link came down.
    # Inverting this one sentence leaves every other check in this file green,
    # so it needs an assertion of its own. Found by mutation 19/53 surviving
    # against the GPT file; the sentence now says "the documentation you read"
    # in place of "the fetched documentation".
    assert (
        "Never write it from memory or out of the documentation you read."
        in norm
    ), (
        "the assistant may now write the support address from memory or copy"
        " it out of the documentation it read, where it appears in prose"
    )
    # The refusal script is the answer a user is most likely to act on, and
    # it used to carry a mailto link. It now carries the code span.
    assert (
        "You can email the developer at `help@wheelhouse-project.org`, or"
        " reach them at the Wheelhouse GitHub page:" in norm
    ), "the refusal script no longer gives the address as a code span"


def test_gem_instructions_do_not_lead_a_help_answer_with_an_email_draft():
    # Observed on the live ChatGPT GPT 2026-08-03, answering "How can I get
    # help?": the assistant wrote a subject line and a message body, ChatGPT
    # turned that layout into an email draft card, and the card became the
    # first and largest thing in the answer -- as though sending mail were the
    # only way to get help. The in-app Help and the GitHub page were one line
    # of prose underneath it. The fix is about ORDER, not only about which
    # form the address takes: the draft is written only when the user asks for
    # it. The order rule survives the move to the Gem on its own merits --
    # email is the slowest of the four ways to get help whatever renders it.
    norm = " ".join(_GEM_INSTRUCTIONS.read_text(encoding="utf-8").split())
    assert (
        "WHEN ASKED HOW TO GET HELP or how to report a problem, give the"
        " several ways to get help and let email be one of them, never the"
        " first and never the only one." in norm
    ), "a help answer may lead with email again"
    assert (
        'Lead with the help built into Wheelhouse: right-click the floating'
        ' button or the tray icon and choose Help, or say "x-ray help".'
        in norm
    ), "the in-app help is no longer the first thing a help answer offers"
    # The rule needs a literal address to reproduce, and it must be pinned
    # TOGETHER with the instruction to copy it. Asserting the code span
    # appears somewhere in the file is not enough: the refusal script lower
    # down holds an identical code span, so deleting this one leaves the
    # bare-address test green while the support answer loses the address it
    # was told to copy. Found by mutation 27/49 surviving.
    assert (
        "write it only inside a code span, copied character for character"
        f" from this line: {_ADDRESS_CODE_SPAN}" in norm
    ), "the address block no longer gives a literal address to copy"
    # The draft is written only on request.
    assert (
        "Do NOT write out a subject line and a message body unless the user"
        " takes up that offer." in norm
    ), "the assistant may write an unrequested email draft again"
    # DELETED with the GPT: the reason clause that named ChatGPT's draft card
    # as what the subject-line-plus-body layout triggers. The Gem does not
    # build draft cards, so naming that trigger would describe a mechanism
    # that no longer exists.


# DELETED with the GPT: test_gpt_instructions_warn_that_the_draft_card_has_no
# _recipient. Its subject was ChatGPT's own email draft card, which appeared
# with an EMPTY Recipients field above a working Send button (observed
# 2026-08-03), so the instructions had to tell the user to paste the address
# in before pressing Send. Gemini builds no draft card and offers no Send
# button, so there is no empty field to warn about. The half of that test
# that still has a subject -- the answer must name the address the message
# goes to -- moved into
# test_gem_instructions_pin_the_drafted_message_content below.


def test_gem_instructions_pin_the_drafted_message_content():
    # What the assistant actually writes into the message. Each rule here is
    # one a mutation can delete on its own, so each gets its own assertion
    # rather than one assertion over the whole block.
    norm = " ".join(_GEM_INSTRUCTIONS.read_text(encoding="utf-8").split())
    assert (
        "WHEN THE USER WANTS TO SEND A MESSAGE, write the text for them to"
        " copy." in norm
    ), "the write-the-message-for-the-user directive is missing"
    # Moved here from the deleted draft-card test: the user has to be told
    # which address to send to, in the form that renders as plain text, and
    # that they must send it themselves. On the Gem this sentence carries the
    # whole mechanism -- there is no card and no Send button, so a message
    # with no stated recipient reaches nobody.
    assert (
        "Say first, in a sentence, that they send it to"
        f" {_ADDRESS_CODE_SPAN} from their own mail program, because you"
        " cannot send it for them." in norm
    ), (
        "the drafted message no longer names the address it goes to, or no"
        " longer says the user must send it themselves"
    )
    assert (
        'Use the subject "Wheelhouse bug report" for a defect and "Wheelhouse'
        ' question" for anything else.' in norm
    ), "the two subject lines are gone; every message would arrive unsorted"
    assert (
        "For a defect, give one prompt per line for the user to fill in: what"
        " I did, what I expected, what happened instead, the full error"
        " message, and the Wheelhouse version" in norm
    ), "the defect-report prompts are missing or shortened"
    # A prompt the user cannot answer is worse than no prompt: the version is
    # not visible anywhere obvious, so the draft says where to find it.
    assert (
        "the Wheelhouse version, which is in About Wheelhouse in the"
        " right-click menu." in norm
    ), (
        "the draft asks for the Wheelhouse version without saying where to"
        " find it"
    )
    # The user pastes this text into their own mail program and sends it, so
    # anything the assistant writes into the draft can leave their machine.
    assert (
        "Never put passwords, tokens, medical or financial details in the"
        " message." in norm
    ), "the rule keeping secrets out of the drafted message is missing"


def test_gem_instructions_claim_no_mail_powers():
    # The assistant has no mailbox and no way to send. Claiming either is a
    # promise the user acts on and that nothing keeps.
    #
    # DELETED with the GPT: the half of this test that forbade the phrases
    # "no mailbox access" and "sends nothing", and required the sentence
    # correcting them. Its subject was ChatGPT's draft card, whose Send
    # button really does send (verified 2026-08-02), which made a blanket
    # "nothing will be sent" claim false. Gemini shows no such card and sends
    # nothing, so the file now states the opposite -- it cannot send -- and
    # that statement is true.
    norm = " ".join(_GEM_INSTRUCTIONS.read_text(encoding="utf-8").split())
    assert (
        "Never claim you can read or search the user's mailbox, and never"
        " claim you can send mail for them." in norm
    ), "the no-mailbox-reading and no-sending rules are missing or weakened"


# DELETED with the GPT: test_gpt_action_schema_gets_both_raw_docs, and the
# _RAW_DOC_URL, _RAW_REFERENCE_URL and _EXPECTED_OPERATIONS constants it
# used. Its subject was gpt-action-openapi.json -- the OpenAPI 3.1.0
# document declaring three GETs (getHelpDocument, getInstallGuide,
# getCommandReference), each reassembling to a raw.githubusercontent.com URL
# and returning text/plain. The Gem stores its three documents as attached
# files instead of fetching them, so there is no schema, no server, no
# operation and no response contract left to check. The json import went with
# it. test_gem_instructions_name_no_gpt_action above guards the one thing
# that survives: the operation names must not appear in the instruction text.


# ---------------------------------------------------------------------------
# Kit folder, help page, and README link integrity.

# Anchors the help page must expose: the section itself plus one
# subsection per supported provider (provider setup steps live ONLY on the
# help page; everything else links to these).
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
    assert _GEM_INSTRUCTIONS.is_file(), f"missing {_GEM_INSTRUCTIONS}"
    assert _HELP_DOC.is_file(), f"missing {_HELP_DOC}"
    assert _HELP_PAGE.is_file(), f"missing {_HELP_PAGE}"


def test_landing_page_has_all_provider_anchors():
    html = _HELP_PAGE.read_text(encoding="utf-8")
    missing = [a for a in _REQUIRED_ANCHORS if a not in html]
    assert not missing, (
        f"help page is missing LLM help anchors: {missing}. The llm/ "
        "README and the public README link to these anchors, so removing "
        "one breaks published links."
    )


def test_help_kit_files_have_no_placeholders():
    for path in (_LLM_README, _GEM_INSTRUCTIONS, _HELP_PAGE):
        content = path.read_text(encoding="utf-8")
        offenders = [p for p in _PLACEHOLDERS if p in content]
        assert not offenders, f"{path.name} contains placeholders: {offenders}"


def test_no_shipped_public_file_links_the_retired_gpt():
    """The retired GPT's address must be gone from everything shipped.

    OpenAI stops running custom GPTs on 2026-12-11, so a surviving link
    sends the user to a page that will stop answering. The scan covers the
    whole public overlay, not a fixed list: the address reached the site
    pages, the config reference, and both READMEs, and a fixed list would
    miss whichever file it lands in next. Files that are not text (the
    screenshots, the favicon, the plaque) are skipped by the decode failure.
    """
    offenders = []
    for path in sorted(_PUBLIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        if _RETIRED_GPT_URL_FRAGMENT in content:
            offenders.append(str(path.relative_to(_PUBLIC_DIR)))
    assert not offenders, (
        f"these shipped files still link the retired ChatGPT GPT: {offenders}."
        " The official assistant is the Gemini Gem at"
        f" {_GEM_URL}; OpenAI stops running custom GPTs on 2026-12-11."
    )


def test_no_shipped_file_presents_the_assistant_as_the_retired_gpt():
    """No shipped file may describe the assistant as the ChatGPT GPT.

    The address guard above catches a link. It cannot catch prose: the
    privacy notice named the GPT, its OpenAI Action, and the GitHub fetch
    without ever giving the address, and shipped that way (finding
    wh-gem-replaces-gpt-assistant.2.1). A reader deciding what to dictate
    to the assistant was pointed at the wrong data processor and at a
    fetch the Gem does not perform. The scan covers the whole public
    overlay for the same reason the address scan does.

    The first version of this guard missed the changelog entry that told
    the reader the assistant "runs inside ChatGPT and needs a ChatGPT
    account" (finding wh-gem-replaces-gpt-assistant.2.2), for two reasons
    now fixed: the phrase list did not name that shape, and the sentence
    wrapped across two lines, so a raw substring search could not have
    matched it even if it had. That finding called the entry unreleased,
    which was false: 1.1.0 shipped it on 2026-09-22.

    Released changelog sections are history and keep their published
    wording, so CHANGELOG.md is scanned only above its first heading that
    carries a real date, which is the unreleased section. Once a release
    cut dates that heading, the scanned region is empty until the next
    unreleased section exists, and that is intended. Every other public
    file is scanned whole.
    """
    offenders = []
    for path in sorted(_PUBLIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        if path == _PUBLIC_DIR / "CHANGELOG.md":
            released = _RELEASED_CHANGELOG_HEADING.search(raw)
            if released is not None:
                raw = raw[: released.start()]
        # Every run of whitespace becomes one space before matching. Prose
        # in a markdown file wraps, and the offending changelog sentence
        # split "needs a ChatGPT account" across two lines, which a raw
        # substring search walks straight past.
        content = " ".join(raw.split()).lower()
        for phrase in _RETIRED_ASSISTANT_PHRASES:
            if phrase in content:
                offenders.append(f"{path.relative_to(_PUBLIC_DIR)}: {phrase}")
    assert not offenders, (
        "these shipped files still describe the Wheelhouse Assistant as the"
        f" retired ChatGPT GPT: {offenders}. The assistant is the Gemini Gem"
        f" at {_GEM_URL}; it stores its knowledge as attached files and"
        " fetches nothing at answer time."
    )


def test_privacy_notice_names_google_as_the_assistant_processor():
    """The privacy notice must name the Gem and Google, not OpenAI.

    A privacy notice that names the wrong processor sends the reader to
    the wrong company's terms. Wheelhouse users dictate passwords,
    medical text, and legal text, so the notice has to be right about who
    receives a conversation. The test matches the phrase "processed by
    OpenAI" and not the bare word: the same document correctly names the
    OpenAI-compatible server the dictation fix-up feature talks to, which
    has nothing to do with the assistant.
    """
    content = _PRIVACY_NOTICE.read_text(encoding="utf-8")
    assert _GEM_URL in content, (
        f"{_PRIVACY_NOTICE.name} does not give the assistant's address"
        f" ({_GEM_URL}), so a reader cannot tell which assistant the"
        " section describes"
    )
    assert "processed by Google" in content, (
        f"{_PRIVACY_NOTICE.name} does not say who processes a conversation"
        " with the assistant. Google runs the Gem, and the reader needs that"
        " name to find the right terms of service and privacy policy"
    )
    assert "processed by OpenAI" not in content, (
        f"{_PRIVACY_NOTICE.name} still says OpenAI processes the"
        " conversation. OpenAI processes no Wheelhouse Assistant"
        " conversation any more, so that sentence points the reader at the"
        " wrong company's terms of use and privacy policy."
    )


def test_shipped_readmes_name_the_official_gem():
    """Both READMEs must point at the official assistant by its real address.

    This is the one link a user is most likely to follow, and it is the
    whole reason the llm/ folder exists. Each README opens with it.
    """
    for path in (_LLM_README, _PUBLIC_README):
        content = path.read_text(encoding="utf-8")
        assert _GEM_URL in content, (
            f"{path.name} no longer gives the official Wheelhouse Assistant's"
            f" address ({_GEM_URL}); the fastest path to help is missing from"
            " the document that is supposed to lead with it"
        )


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
    assert "(./gem-instructions.txt)" in content, (
        "llm/README.md no longer links gem-instructions.txt, the one "
        "assistant file the folder ships"
    )
    # DELETED with the two GPT files: the "(./gpt-instructions.txt)" and
    # "(./gpt-action-openapi.json)" link assertions, and the existence checks
    # beside them. Neither file is shipped any more.
    # Public "../services/..." resolves against the repo root; the dev-tree
    # equivalent is _REPO_ROOT / services/... The instruction file lives in
    # the same folder in both layouts.
    assert (_REPO_ROOT / "services/wheelhouse/knowledge/wheelhouse_help.md").is_file()
    assert _GEM_INSTRUCTIONS.is_file()
    # Every help-page anchor the README links must exist on the page.
    html = _HELP_PAGE.read_text(encoding="utf-8")
    for url in _SITE_ANCHOR_URLS:
        anchor = "#" + url.rsplit("#", 1)[1]
        assert url in content, f"llm/README.md dropped the {url} link"
        assert f'id="{anchor[1:]}"' in html, f"page lost the {anchor} target"


def test_llm_readme_documents_the_reference_document():
    """Two-file build (wh-helpdoc-migration.3.1): the command and config
    tables moved out of the guide into wheelhouse_reference.md, so the
    upload-based setup now needs both files and the README must link it.

    DELETED with the Action schema: the assertion that the builder section
    names getCommandReference, the GPT's second Action. There are no Actions
    to document; test_gem_instructions_name_no_gpt_action guards against the
    name coming back.
    """
    content = _LLM_README.read_text(encoding="utf-8")
    # The reference file is linked at its public-repo path (llm/.. ->
    # services/wheelhouse/knowledge/), like the help doc.
    assert "(../services/wheelhouse/knowledge/wheelhouse_reference.md)" in content, (
        "llm/README.md does not link the separate reference document; "
        "upload-based setups need it now that the tables left the guide"
    )
    assert (_REPO_ROOT / "services/wheelhouse/knowledge/wheelhouse_reference.md").is_file()


# The exact public-repo blob URL the help page must link. Substring
# checks are not enough: an href on the wrong host or a non-blob URL would
# still contain the repo-relative path, so this is matched against parsed
# href values.
_CANONICAL_HELP_URL = (
    "https://github.com/wheelhouse-project/Wheelhouse/blob/main/"
    "services/wheelhouse/knowledge/wheelhouse_help.md"
)

_SITE_ANCHOR_URLS = tuple(
    f"https://wheelhouse-project.org/help.html#llm-{provider}"
    for provider in ("chatgpt", "gemini", "claude", "perplexity")
)


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def test_landing_page_links_canonical_help_doc():
    """The help page's download link must be an actual href holding the
    exact canonical blob URL of the help document. The kit ships a command
    and setting reference beside it; this test pins only the guide's link."""
    hrefs = _hrefs(_HELP_PAGE.read_text(encoding="utf-8"))
    assert _CANONICAL_HELP_URL in hrefs, (
        f"help page is missing the canonical help-doc href. Present "
        f"hrefs to GitHub blobs: {[h for h in hrefs if 'blob' in h]}"
    )


def test_public_readme_help_kit_links_resolve():
    """The public README publishes the same help-kit links as the llm README:
    the help document, the llm folder, and the four help-page anchors.
    Each anchor URL must target an id that exists on the help page."""
    content = _PUBLIC_README.read_text(encoding="utf-8")
    for link in (
        "(./services/wheelhouse/knowledge/wheelhouse_help.md)",
        "(./llm/README.md)",
    ):
        assert link in content, f"public README dropped the {link} link"
    html = _HELP_PAGE.read_text(encoding="utf-8")
    for url in _SITE_ANCHOR_URLS:
        assert url in content, f"public README dropped the {url} link"
        fragment = url.rsplit("#", 1)[1]
        assert f'id="{fragment}"' in html, (
            f"help page lost the id the public README links: #{fragment}"
        )
