"""Package guards for the LLM help kit and the official GPT files.

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
superseding decisions 8 and 9 of the 2026-07-15 packaging design. The llm/
folder instead ships the two files behind the official Wheelhouse ChatGPT
GPT -- `gpt-instructions.txt` and `gpt-action-openapi.json` (two GETs: the
help document and the command reference, each from its raw GitHub URL) -- so
anyone can also build their own live-fetching assistant from them.

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
# The project's support address. A markdown code span is the only form of it
# that survives ChatGPT: it renders as plain text, and no renderer turns it
# into a link. Every other form was tried on the live GPT and failed -- see
# test_gpt_instructions_never_give_the_support_address_as_bare_text.
_SUPPORT_ADDRESS = "help@wheelhouse-project.org"
_GPT_ADDRESS_CODE_SPAN = "`help@wheelhouse-project.org`"
_RAW_DOC_URL = (
    "https://raw.githubusercontent.com/wheelhouse-project/Wheelhouse/main/"
    "services/wheelhouse/knowledge/wheelhouse_help.md"
)
# Two-file build (wh-helpdoc-migration.3.1): the command-and-configuration
# reference ships as a separate document, fetched by the second Action.
_RAW_REFERENCE_URL = (
    "https://raw.githubusercontent.com/wheelhouse-project/Wheelhouse/main/"
    "services/wheelhouse/knowledge/wheelhouse_reference.md"
)
# operationId -> the raw URL its single GET must reassemble to.
_EXPECTED_OPERATIONS = {
    "getInstallGuide": "https://raw.githubusercontent.com/wheelhouse-project/Wheelhouse/main/services/wheelhouse/knowledge/wheelhouse_install.md",
    "getHelpDocument": _RAW_DOC_URL,
    "getCommandReference": _RAW_REFERENCE_URL,
}


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
        "The 2026-07-17 source-of-truth design retired the separate companion "
        "text; the behavior rules travel only inside the help document."
    )


def test_no_shipped_doc_references_assistant_instructions_txt():
    for path in (_LLM_README, _LANDING_PAGE, _PUBLIC_README):
        content = path.read_text(encoding="utf-8")
        assert "assistant-instructions" not in content, (
            f"{path.name} still references the retired "
            "assistant-instructions.txt; the behavior rules are embedded in "
            "the help document now."
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
    # The help document is read by more than the official GPT: people load it
    # into Gemini, Claude and Perplexity, and those assistants copy the
    # address out of it into their answers. A bare address copied into a
    # ChatGPT-style answer becomes a link that opens a browser tab with no
    # content, which is the defect that started this work. Instructing the
    # GPT not to copy the bare form fixes only the GPT; putting the code span
    # in the document fixes every reader of it.
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
    assert _GPT_ADDRESS_CODE_SPAN in text, (
        "the help document no longer gives the support address in a code span"
    )
    assert _SUPPORT_ADDRESS not in text.replace(_GPT_ADDRESS_CODE_SPAN, ""), (
        f"the help document gives {_SUPPORT_ADDRESS} as a bare address"
        " somewhere outside a code span. Fix the prose in"
        " services/wheelhouse/knowledge/helpdoc/sections/, then rebuild with"
        " scripts/release/build_helpdoc.py -- editing the built document"
        " directly fails the rebuild-and-compare check"
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
    # Fetch-first, on every question, answering only from what was fetched
    # (Design B, two-file build: the source is chosen by the question).
    # The ChatGPT builder REJECTS an instruction file over 8000 characters.
    # The file passed that limit at 12046 after five rounds of review fixes
    # and had to be rewritten. Guard the limit so prose can never grow past
    # it unnoticed again; the headroom below is deliberately small because a
    # large margin would invite the same growth.
    assert len(text) <= 8000, (
        f"gpt-instructions.txt is {len(text)} characters; the ChatGPT builder"
        " refuses anything over 8000"
    )
    assert (
        "FETCH FIRST, EVERY TIME: before answering any Wheelhouse question,"
        " fetch the current documentation and answer ONLY from what you just"
        " fetched." in norm
    ), "the fetch-before-every-answer grounding directive is missing or weakened"
    # Routing: command/config questions go to the reference Action; every
    # other Wheelhouse question goes to the guide Action.
    assert (
        "For a specific voice command or configuration setting (the exact"
        " wording, a config key or its default), call getCommandReference."
        in norm
    ), "the route-command/config-questions-to-the-reference directive is missing"
    assert (
        "For every other Wheelhouse question (what it is, getting started,"
        " hardware, speech engines, concepts, troubleshooting), call"
        " getHelpDocument." in norm
    ), "the route-other-questions-to-the-guide directive is missing"
    assert (
        "For installing, upgrading, uninstalling, or installer troubleshooting, call getInstallGuide."
        in norm
    ), "installation questions must fetch the separate installation guide"
    # Ignore the structural markers visible in the raw markdown.
    assert (
        "The documents contain HTML comment markers such as"
        " <!-- install-doc:start -->; ignore them and never mention them."
        in norm
    ), "the ignore-HTML-comments directive is missing or weakened"
    # Fetch-failure rule: admit the documentation is unreachable AND refuse
    # to answer from memory, in the same directive.
    assert (
        "IF THE FETCH FAILS: tell the user you cannot reach the current"
        " Wheelhouse documentation, and do NOT answer Wheelhouse questions"
        " from memory." in norm
    ), "the fetch-failure refusal directive is missing or weakened"
    assert "https://wheelhouse-project.org/" in text
    assert "https://github.com/wheelhouse-project/Wheelhouse" in text
    # Version disclosure: each document names the release it describes in its
    # "Generated ... for the vX.Y.Z release" footer line. The guide's footer
    # also has a "Wheelhouse version" line, which in development copies holds
    # an internal build identifier (for example backup/dev/20251127-...). The
    # guide's own embedded instructions say the same
    # (services/wheelhouse/knowledge/wheelhouse_help.md, "Instructions for AI
    # Assistant"); this file once named the wrong line and contradicted them
    # (wh-gpt-version-line-wrong).
    assert (
        "If an answer could depend on the Wheelhouse version, say which"
        " release the documentation describes" in norm
    ), "the version-disclosure directive is missing or inverted"
    assert (
        "read it from each document's \"Generated\" footer line"
        ' ("for the vX.Y.Z release").' in norm
    ), "the directive no longer names the Generated footer line as the source"
    # The public export stamps that line with the release number
    # (scripts/release/manifest.toml, the wheelhouse_help.md [[sanitize]]
    # stamp), and the GPT reads the public copy, so the reason clause must
    # hold for both copies: only development copies carry the identifier.
    assert (
        'Ignore the guide\'s "Wheelhouse version" line; in development'
        " copies it holds an internal build identifier." in norm
    ), "the directive no longer tells the GPT to ignore the build identifier"
    # Email. The address form, the order a help answer puts its options in,
    # and what the GPT may say about ChatGPT's Send button are pinned in the
    # three tests below this one.
    assert (
        "THE SUPPORT EMAIL ADDRESS: never write it as bare text, and never"
        " write a mailto: link to it." in norm
    ), "the no-bare-address, no-mailto-link rule is missing or weakened"
    # The answer policy splits by question kind. Wheelhouse facts stay
    # grounded in the two documents, then the repository, then a refusal.
    # Everything else -- Windows, microphones, speech recognition in
    # general, ordinary computer questions -- is answered from the model's
    # own knowledge and must never draw the refusal script.
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
    # fetch-search-refuse path and end in "I don't have information about
    # that" when the model could have answered it. This branch used to spell
    # out each fetch outcome; the 8000-character rewrite reduced it to the
    # two rules that carry the behavior, and the deleted detail is covered by
    # the fetch-the-other-document transition and IF THE FETCH FAILS, both
    # pinned below.
    assert (
        "If you cannot tell which kind a question is, fetch and look." in norm
    ), "the cannot-classify fallback is missing"
    assert (
        "If none of the documents covers it, answer from your own knowledge and"
        " say so; do not use the refusal script, which is for questions you"
        " have established are about Wheelhouse." in norm
    ), "the cannot-classify miss no longer routes away from the refusal script"
    assert (
        "Refusing is the wrong answer to a question you can answer." in norm
    ), "the do-not-refuse-what-you-know directive is missing or weakened"
    # The audience split describes who is asking, not what may be answered
    # from memory. Without this scope line a current user's Windows question
    # draws "answer from the documentation you fetched".
    assert (
        "a question of the other kind is answered from your own knowledge"
        " whoever asks" in norm
    ), "the audience section is no longer scoped to Wheelhouse questions"
    # The repository search applies to Wheelhouse questions only. Without
    # that scope a general question ("why does my microphone cut out in
    # Windows") sends the GPT searching the source code for it.
    assert (
        "If you have fetched all three documents and none answers a question"
        " about Wheelhouse itself, search the Wheelhouse GitHub repository"
        " (https://github.com/wheelhouse-project/Wheelhouse)" in norm
    ), "the search-the-repository fallback directive is missing or unscoped"
    # The search is a step after reading both documents, never a substitute
    # for reading them. A failed fetch leaves the GPT unable to judge whether
    # what it finds in the repository is in the user's release.
    assert (
        "only after you have read all three; a failed fetch does not permit it"
        in norm
    ), "the search-only-after-reading-both precondition is missing or weakened"
    # The same precondition on the FETCH FIRST pointer, which is where a
    # model reading top to bottom meets the search step first.
    assert (
        "If you have read all three documents and none answers a question about"
        " Wheelhouse itself, the GitHub repository search below is the only"
        " other place a Wheelhouse fact may come from" in norm
    ), "the FETCH FIRST pointer no longer requires both documents to be read"
    # Requiring both documents to have been read created a state with no exit:
    # the chosen document is fetched, comes back, and does not answer, while
    # the other was never attempted. Neither the search nor the refusal nor
    # the fetch-failure block applies. The transition to the other document
    # is what closes it.
    assert (
        "When the document you chose does not answer the question, fetch the"
        " remaining documents before doing anything else" in norm
    ), "the fetch-the-other-document transition is missing"
    assert (
        "If a fetch fails, take IF THE FETCH FAILS." in norm
    ), "the fetch-failure route out of the two-document transition is missing"
    # A question with a separable general part belongs to the mixed-question
    # rule, not the cannot-classify branch. Without this precedence, the file's
    # own mixed example ("my microphone keeps cutting out while I dictate")
    # reaches the branch, and a document hit there would collapse it into a
    # Wheelhouse-only answer that drops the general half.
    # The precedence must be limited to a part the model can answer WITHOUT
    # knowing anything about Wheelhouse. Its earlier form fired on any question
    # that merely "has a part" outside Wheelhouse, which let a real question --
    # "which Windows microphone setting lets Wheelhouse hear me" -- be answered
    # from memory, and answering it asserts which permission governs
    # Wheelhouse's audio capture. That is a Wheelhouse fact, and FETCH FIRST
    # forbids supplying one from memory.
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
        "Fetch that fact; never supply it from memory." in norm
    ), "a Wheelhouse fact a general answer depends on may come from memory"
    assert (
        "Do not search the GitHub repository instead:" in norm
    ), "the fetch-failure block no longer closes off the repository search"
    # A failed fetch stops Wheelhouse answers and nothing else. Without this
    # line the fetch-failure block reads as a general refusal, and a user
    # whose guide fetch failed gets nothing for the Windows half of their
    # question either.
    assert (
        "A failed fetch stops Wheelhouse answers only; keep answering every"
        " other kind of question from your own knowledge." in norm
    ), "the fetch-failure block is no longer limited to Wheelhouse answers"
    # The repository holds work the documented release does not: unmerged
    # changes, rejected issues, speculative discussions, and merged code that
    # has not shipped. All of them are wrong answers to "how does Wheelhouse
    # behave today", not just the first two.
    assert (
        "never present unreleased work as current behavior: an unmerged"
        " change, an open or rejected issue, a discussion proposing"
        " something, or merged code that has not shipped" in norm
    ), "the do-not-quote-unreleased-work caution is missing or narrowed"
    assert (
        "If the repository does not answer it either, say:" in norm
    ), "the refusal script no longer reads as the step after the search"
    assert (
        "Use that refusal only for questions about Wheelhouse itself;"
        " anything outside Wheelhouse is answered from your own knowledge."
        in norm
    ), "the refusal script is no longer limited to Wheelhouse questions"
    # Order matters: a refusal script placed before the search directive
    # would let the GPT give up without ever searching.
    assert norm.index(
        "search the Wheelhouse GitHub repository"
    ) < norm.index("If the repository does not answer it either, say:"), (
        "the refusal script comes before the repository search; the GPT"
        " would refuse without searching"
    )
    # The paste target is ChatGPT's instructions field: keep it plain ASCII
    # so nothing mangles in transit.
    assert text.isascii(), "gpt-instructions.txt must be plain ASCII"


def test_gpt_instructions_never_give_the_support_address_as_bare_text():
    # Four observations on the live GPT, in order. The first three are from
    # 2026-08-02, the fourth from 2026-08-03:
    #   1. A bare address renders as a link to a blank browser page.
    #   2. A markdown link with a mailto: destination does not open a mail
    #      program either. ChatGPT stops it at an "External site" dialog
    #      showing the whole percent-encoded URI, and its "Open link" button
    #      opens a browser tab with no content.
    #   3. The ChatGPT draft card's Send button does send the message.
    #   4. The draft card is ChatGPT's OWN email feature reacting to
    #      draft-shaped text, not its rendering of the mailto link: in the
    #      2026-08-03 answer the card and the link appeared as separate
    #      elements, and clicking the link ("Open a pre-addressed email") did
    #      nothing. That settles the question the 2026-08-02 fix left open and
    #      makes both mailto links dead weight, so they are gone.
    # A code span is the only form that survives: it renders as plain text and
    # no renderer turns it into a link.
    text = _GPT_INSTRUCTIONS.read_text(encoding="utf-8")
    assert _GPT_ADDRESS_CODE_SPAN in text, (
        "the code-span form of the support address is missing; it is the only"
        " form that reliably works in ChatGPT"
    )
    # No mailto: URI in any form. The earlier version of this test pinned two
    # exact link strings, which would have stayed green against a third link
    # written differently. Banning the scheme outright cannot be sidestepped.
    # The word appears exactly once in the file, in the sentence forbidding
    # it; a second occurrence is a link. Counting rather than subtracting the
    # known sentence keeps the check independent of where the line wraps.
    assert text.count("mailto:") == 1, (
        f'"mailto:" appears {text.count("mailto:")} times in'
        " gpt-instructions.txt; it belongs only in the sentence forbidding"
        " it. Clicking a mailto link in a ChatGPT answer opens a browser tab"
        " with no content -- verified twice on the live GPT, with two"
        " different link forms"
    )
    assert "](mailto:" not in text, (
        "gpt-instructions.txt builds a markdown link with a mailto:"
        " destination again; that is the exact form that was verified dead"
    )
    # Every literal occurrence of the address must sit inside a code span.
    # Removing the code span leaves no bare address anywhere in the file --
    # including the refusal script the GPT is told to say word for word,
    # which is where the first broken link came from.
    assert _SUPPORT_ADDRESS not in text.replace(_GPT_ADDRESS_CODE_SPAN, ""), (
        f"gpt-instructions.txt still gives {_SUPPORT_ADDRESS} as a bare"
        " address somewhere outside a code span; ChatGPT turns a bare address"
        " into a link that opens a blank page"
    )
    norm = " ".join(text.split())
    # The fetched help document gives the address as bare text, and the GPT
    # reads that document on every Wheelhouse question. Copying the address
    # out of it is the exact path the first broken link came down. Inverting
    # this one sentence leaves every other check in this file green, so it
    # needs an assertion of its own. Found by mutation 19/53 surviving.
    assert (
        "Never write it from memory or out of the fetched documentation."
        in norm
    ), (
        "the GPT may now write the support address from memory or copy it out"
        " of the fetched documentation, where it appears as bare text"
    )
    # The refusal script is the answer a user is most likely to act on, and
    # it used to carry a mailto link. It now carries the code span.
    assert (
        "You can email the developer at `help@wheelhouse-project.org`, or"
        " reach them at the Wheelhouse GitHub page:" in norm
    ), "the refusal script no longer gives the address as a code span"


def test_gpt_instructions_do_not_lead_a_help_answer_with_an_email_draft():
    # Observed on the live GPT 2026-08-03, answering "How can I get help?":
    # the GPT wrote a subject line and a message body, ChatGPT turned that
    # layout into an email draft card, and the card became the first and
    # largest thing in the answer -- as though sending mail were the only way
    # to get help. The in-app Help and the GitHub page were one line of prose
    # underneath it. So the fix is about ORDER, not only about which form the
    # address takes: the draft is written only when the user asks for it.
    norm = " ".join(_GPT_INSTRUCTIONS.read_text(encoding="utf-8").split())
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
        "Write it only inside a code span, copied character for character"
        f" from this line: {_GPT_ADDRESS_CODE_SPAN}" in norm
    ), "the address block no longer gives a literal address to copy"
    # The trigger for the card is the subject-line-plus-body layout. Naming
    # the trigger is what makes the rule actionable; without it the GPT is
    # told not to cause an effect whose cause it does not know.
    assert (
        "Do NOT write out a subject line and a message body unless the user"
        " takes up that offer: ChatGPT turns that layout into an email card"
        in norm
    ), "the rule no longer names what makes ChatGPT build the draft card"


def test_gpt_instructions_warn_that_the_draft_card_has_no_recipient():
    # Observed 2026-08-03: ChatGPT built the draft card with its Recipients
    # field EMPTY, directly under a working Send button. Pressing Send would
    # have sent the message to nobody, and nothing in the answer said so.
    # The instructions cannot populate that field -- it belongs to ChatGPT's
    # own email feature -- so the GPT warns the user to fill it in instead.
    norm = " ".join(_GPT_INSTRUCTIONS.read_text(encoding="utf-8").split())
    assert (
        "warn that ChatGPT's draft card often leaves its Recipients field"
        " empty, so they must paste that address into it before pressing"
        " Send." in norm
    ), (
        "the empty-Recipients warning is gone; the user is left with a Send"
        " button that would send the message to nobody"
    )
    # The warning is only useful if the answer has already said which address
    # to paste, in the form that survives ChatGPT.
    assert (
        "Say first, in a sentence, that it goes to"
        f" {_GPT_ADDRESS_CODE_SPAN}" in norm
    ), "the draft no longer names the address it should be sent to"


def test_gpt_instructions_pin_the_drafted_message_content():
    # What the GPT actually writes into the message. Each rule here is one a
    # mutation can delete on its own, so each gets its own assertion rather
    # than one assertion over the whole block.
    norm = " ".join(_GPT_INSTRUCTIONS.read_text(encoding="utf-8").split())
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
    # ChatGPT's card has a working Send button, so anything the GPT writes
    # into the draft can leave the user's machine on one click.
    assert (
        "Never put passwords, tokens, medical or financial details in the"
        " message." in norm
    ), "the rule keeping secrets out of the drafted message is missing"


def test_gpt_instructions_do_not_deny_that_the_send_button_sends():
    # Verified 2026-08-02: the Send button on ChatGPT's draft card really
    # does send the message. The file used to tell the GPT it had no mailbox
    # access and that a mailto link "sends nothing on its own", which put the
    # words "sends nothing" directly above a button that sends. Denying that
    # a real Send button works is worse than saying nothing: a user who
    # believes it takes no other action.
    norm = " ".join(_GPT_INSTRUCTIONS.read_text(encoding="utf-8").split())
    assert (
        "Never say the message will not be sent: when ChatGPT shows the draft"
        " in a card with a Send button, that button really does send it."
        in norm
    ), "the file denies, or no longer corrects, that the Send button sends"
    # Reading and searching a mailbox is a separate capability from sending,
    # and the GPT still has neither on its own. Keep that half of the rule.
    assert (
        "Never claim you can read or search the user's mailbox." in norm
    ), "the no-mailbox-reading rule is missing"
    # The old blanket claim must not come back with it.
    assert "no mailbox access" not in norm, (
        "the blanket no-mailbox-access claim is back; ChatGPT's draft card"
        " can send mail, so the claim is false and misleads the user"
    )
    assert "sends nothing" not in norm, (
        'the file says "sends nothing" again; on a ChatGPT draft card the'
        " Send button does send"
    )


def test_gpt_action_schema_gets_both_raw_docs():
    # Two-file build (wh-helpdoc-migration.3.1): the single Action schema
    # now defines TWO GETs -- getHelpDocument (the guide) and
    # getCommandReference (the separate command-and-config reference) -- so
    # the GPT can route command/config questions to the reference and every
    # other question to the guide (Design B).
    schema = json.loads(_GPT_ACTION_SCHEMA.read_text(encoding="utf-8"))
    assert str(schema.get("openapi", "")) == "3.1.0", (
        "gpt-action-openapi.json must declare OpenAPI 3.1.0 exactly: the"
        " ChatGPT Actions editor requires a 3.1 schema, so a downgrade to"
        " 3.0.x would be rejected when the Action is configured"
    )
    servers = [s["url"] for s in schema.get("servers", [])]
    assert len(servers) == 1, f"expected exactly one server, got {servers}"
    paths = schema.get("paths", {})
    assert len(paths) == 3, f"expected exactly three paths, got {list(paths)}"
    by_operation = {}
    for path, item in paths.items():
        assert list(item.keys()) == ["get"], (
            f"each path must define exactly one GET, got {list(item.keys())}"
        )
        get = item["get"]
        by_operation[get.get("operationId")] = (path, get)
    assert set(by_operation) == set(_EXPECTED_OPERATIONS), (
        f"expected operations {set(_EXPECTED_OPERATIONS)}, got {set(by_operation)}"
    )
    for operation, expected_url in _EXPECTED_OPERATIONS.items():
        path, get = by_operation[operation]
        assert servers[0].rstrip("/") + path == expected_url, (
            f"{operation}: server url + path must reassemble to {expected_url}"
        )
        # The response contract: exactly one 200 response returning the raw
        # markdown as a text/plain string. Without these checks, deleting the
        # responses object or swapping the media type would leave the Action
        # declaring a contract ChatGPT no longer receives.
        responses = get.get("responses", {})
        assert list(responses.keys()) == ["200"], (
            f"{operation}: expected exactly one 200 response, got {list(responses)}"
        )
        content = responses["200"].get("content", {})
        assert list(content.keys()) == ["text/plain"], (
            f"{operation}: the 200 response must declare exactly text/plain,"
            f" got {list(content)}"
        )
        assert content["text/plain"].get("schema", {}).get("type") == "string", (
            f"{operation}: the text/plain schema type must be 'string' so the"
            " Action hands ChatGPT the raw markdown document"
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


def test_llm_readme_documents_reference_and_second_action():
    """Two-file build (wh-helpdoc-migration.3.1): the command and config
    tables moved out of the guide into wheelhouse_reference.md, so the
    upload-based setup now needs both files and the builder section must
    document the second Action (getCommandReference)."""
    content = _LLM_README.read_text(encoding="utf-8")
    # The reference file is linked at its public-repo path (llm/.. ->
    # services/wheelhouse/knowledge/), like the help doc.
    assert "(../services/wheelhouse/knowledge/wheelhouse_reference.md)" in content, (
        "llm/README.md does not link the separate reference document; "
        "upload-based setups need it now that the tables left the guide"
    )
    assert (_REPO_ROOT / "services/wheelhouse/knowledge/wheelhouse_reference.md").is_file()
    # The builder section names the second Action operation.
    assert "getCommandReference" in content, (
        "llm/README.md does not document the second GPT Action"
    )


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
    exact canonical blob URL of the help document. The kit ships a command
    and setting reference beside it; this test pins only the guide's link."""
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
