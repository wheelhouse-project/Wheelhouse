# Wheelhouse help for your LLM

The fastest path is the official
[Wheelhouse Assistant on ChatGPT](https://chatgpt.com/g/g-6a5ab92068d0819198db2a83135b9540-wheelhouse)
— one click, nothing to set up, and it always answers from the latest
help document. A free ChatGPT account is enough; if ChatGPT says you "do
not have access to GPT interactions", click **Sign up for free** in the
upper right corner (no credit card required).

Prefer your own setup? Wheelhouse's complete user guide is written so an
AI chat service can answer questions from it. Load it into the LLM you
already use — ChatGPT, Gemini, Claude, or Perplexity — and you get a
personal Wheelhouse support assistant that explains commands and settings
in plain language.

## The files you need

| File | What it is | What you do with it |
|------|------------|---------------------|
| [`wheelhouse_help.md`](../services/wheelhouse/knowledge/wheelhouse_help.md) | The complete Wheelhouse user guide | Upload it to your AI service as a knowledge file |
| [`wheelhouse_reference.md`](../services/wheelhouse/knowledge/wheelhouse_reference.md) | The full voice-command and configuration reference | Upload it alongside the guide so the assistant can answer detailed command and setting questions |

The guide covers what Wheelhouse is, getting started, and how each feature
works; the reference is the exhaustive list of every voice command and
configuration setting. Upload both so your assistant can answer both kinds
of question. The assistant behavior rules are embedded at the top of the
guide (its "Instructions for AI Assistant" section), so there is nothing to
paste. If a service refuses a `.md` upload, rename the file to `.txt` — the
content is plain text.

## Setup steps for each service

The step-by-step setup guides live on the Wheelhouse site, one section per
service:

- [ChatGPT — use a Project](https://wheelhouse-project.org/#llm-chatgpt)
- [Google Gemini — create a Gem](https://wheelhouse-project.org/#llm-gemini)
- [Claude — use a Project](https://wheelhouse-project.org/#llm-claude)
- [Perplexity — use a Project](https://wheelhouse-project.org/#llm-perplexity)

All four work on the service's free plan, with one caveat: Perplexity
documents file uploads inside a project only for its paid plans.

## For builders: the official GPT's files

This folder also ships the two files behind the official Wheelhouse
ChatGPT GPT, which fetches the current documentation live at answer time
instead of using an uploaded copy:

- [`gpt-instructions.txt`](./gpt-instructions.txt) — the GPT's
  instructions: fetch before answering (the command reference for
  command and configuration questions, the guide otherwise), and refuse
  to answer from memory when the fetch fails.
- [`gpt-action-openapi.json`](./gpt-action-openapi.json) — the GPT Action
  schema: two GETs of raw GitHub URLs, `getHelpDocument` for the guide and
  `getCommandReference` for the command-and-configuration reference.

You can reuse both to build your own live-fetching assistant on any
platform that can fetch a URL while answering.
