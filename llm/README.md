# Wheelhouse help for your LLM

The fastest path is the official
[Wheelhouse Assistant](https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview)
— one click, nothing to set up, and it always answers from the latest
documentation. The Wheelhouse Assistant runs on Google's Gemini Notebook,
so you must sign in with a Google Account. The account is free, and Google
does not ask for a credit card. You can use an email address you already
have; a Gmail address is not necessary. To create an account, click
"Create account" on the Google sign-in page. If you are signed in with a
work or school account and the Assistant does not open, sign in with a
personal account instead.

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
| [`wheelhouse_reference.md`](../services/wheelhouse/knowledge/wheelhouse_reference.md) | The full action, notice, and configuration reference | Upload it alongside the guide so the assistant can answer detailed setting questions |

The guide covers what Wheelhouse is, getting started, how each feature
works, and every voice command; the reference is the exhaustive list of every
configuration setting, pattern action, and notice. Upload all three so your assistant can answer all kinds
of question. The assistant behavior rules are embedded at the top of the
guide (its "Instructions for AI Assistant" section), so there is nothing to
paste. If a service refuses a `.md` upload, rename the file to `.txt` — the
content is plain text.

## Setup steps for each service

The step-by-step setup guides live on the Wheelhouse site, one section per
service:

- [ChatGPT — use a Project](https://wheelhouse-project.org/help.html#llm-chatgpt)
- [Google Gemini — use a Notebook](https://wheelhouse-project.org/help.html#llm-gemini)
- [Claude — use a Project](https://wheelhouse-project.org/help.html#llm-claude)
- [Perplexity — use a Project](https://wheelhouse-project.org/help.html#llm-perplexity)

All four work on the service's free plan, with one caveat: Perplexity
documents file uploads inside a project only for its paid plans.

## For builders: the official assistant's instructions

This folder also ships
[`notebook-instructions.txt`](./notebook-instructions.txt), the instruction text
behind the official Wheelhouse Assistant. It is longer and stricter than
the rules embedded in the help document. It tells the assistant to answer
Wheelhouse questions only from the three documents, to answer general
computing questions from its own knowledge, to end every Wheelhouse answer
with the document it read and the release that document describes, and
never to invent a voice command, a configuration key, or a default value.

Paste it into any assistant that accepts an instruction text, then attach
the three documents above. Only one phrase is Gemini Notebook's own: the
file calls the attached documents the "sources" in your notebook, which is
what Gemini Notebook calls them. Every rule in the file works the same on
any assistant that can read attached files.

The official assistant reads one Google Doc holding all three documents
joined together. The project rewrites that Google Doc when it publishes a
release. An assistant built from uploaded copies describes the release you uploaded, which is
what the release line in every answer makes visible.
