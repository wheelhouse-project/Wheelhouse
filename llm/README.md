# Wheelhouse help for your LLM

The fastest path is the official
[Wheelhouse Assistant on Google Gemini](https://gemini.google.com/gem/1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A)
— one click, nothing to set up, and it always answers from the latest
documentation. Gemini asks you to sign in first. A Google account, an
Apple account, or an email address works, and the free tier is enough (no
credit card required).

Prefer your own setup? Wheelhouse's complete user guide is written so an
AI chat service can answer questions from it. Load it into the LLM you
already use — ChatGPT, Gemini, Claude, or Perplexity — and you get a
personal Wheelhouse support assistant that explains commands and settings
in plain language.

## The files you need

| File | What it is | What you do with it |
|------|------------|---------------------|
| [`wheelhouse_help.md`](../services/wheelhouse/knowledge/wheelhouse_help.md) | The Wheelhouse daily-use guide | Upload it to your AI service as a knowledge file |
| [`wheelhouse_install.md`](../services/wheelhouse/knowledge/wheelhouse_install.md) | Installation, updates, removal, and engine setup | Upload it alongside the guide and reference |
| [`wheelhouse_reference.md`](../services/wheelhouse/knowledge/wheelhouse_reference.md) | The full voice-command and configuration reference | Upload it alongside the guide so the assistant can answer detailed command and setting questions |

The guide covers what Wheelhouse is, getting started, and how each feature
works; the reference is the exhaustive list of every voice command and
configuration setting. Upload all three so your assistant can answer all kinds
of question. The assistant behavior rules are embedded at the top of the
guide (its "Instructions for AI Assistant" section), so there is nothing to
paste. If a service refuses a `.md` upload, rename the file to `.txt` — the
content is plain text.

## Setup steps for each service

The step-by-step setup guides live on the Wheelhouse site, one section per
service:

- [ChatGPT — use a Project](https://wheelhouse-project.org/help.html#llm-chatgpt)
- [Google Gemini — create a Gem](https://wheelhouse-project.org/help.html#llm-gemini)
- [Claude — use a Project](https://wheelhouse-project.org/help.html#llm-claude)
- [Perplexity — use a Project](https://wheelhouse-project.org/help.html#llm-perplexity)

All four work on the service's free plan, with one caveat: Perplexity
documents file uploads inside a project only for its paid plans.

## For builders: the official assistant's instructions

This folder also ships
[`gem-instructions.txt`](./gem-instructions.txt), the instruction text
behind the official Wheelhouse Assistant. It is longer and stricter than
the rules embedded in the help document. It tells the assistant to answer
Wheelhouse questions only from the three documents, to answer general
computing questions from its own knowledge, to end every Wheelhouse answer
with the document it read and the release that document describes, and
never to invent a voice command, a configuration key, or a default value.

Paste it into any assistant that accepts an instruction text, then attach
the three documents above. Only one phrase is Gemini's own: the file calls
the attached documents your "Knowledge section", which is what Gemini calls
them. Every rule in the file works the same on any assistant that can read
attached files.

The official assistant reads one Google Doc holding all three documents
joined together, and the Wheelhouse release rewrites that Doc. That is why
the official assistant always describes the current release. An assistant
built from uploaded copies describes the release you uploaded, which is
what the release line in every answer makes visible.
