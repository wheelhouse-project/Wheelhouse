# WheelHouse help for your LLM

WheelHouse's complete user guide is written so an AI chat service can answer
questions from it. Load it into the LLM you already use — ChatGPT, Gemini,
Claude, or Perplexity — and you get a personal WheelHouse support assistant
that explains commands and settings in plain language.

## The two files

| File | What it is | What you do with it |
|------|------------|---------------------|
| [`wheelhouse_help.md`](../services/wheelhouse/knowledge/wheelhouse_help.md) | The complete WheelHouse user guide | Upload it to your AI service as a knowledge file |
| [`assistant-instructions.txt`](./assistant-instructions.txt) | A short, provider-neutral instruction text | Paste it into your AI service's instructions field |

Pasting the instructions is recommended but optional: the same rules are
embedded at the top of the help document, so an assistant that only has the
uploaded file still behaves sensibly. If a service refuses a `.md` upload,
rename the file to `wheelhouse_help.txt` — the content is plain text.

`assistant-instructions.txt` is generated from the help document's embedded
"Instructions for AI Assistant" section. Don't edit it by hand; it is
regenerated whenever the help document changes.

## Setup steps for each service

The step-by-step setup guides live on the WheelHouse site, one section per
service:

- [ChatGPT — use a Project](https://wheelhouse-project.github.io/WheelHouse/#llm-chatgpt)
- [Google Gemini — create a Gem](https://wheelhouse-project.github.io/WheelHouse/#llm-gemini)
- [Claude — use a Project](https://wheelhouse-project.github.io/WheelHouse/#llm-claude)
- [Perplexity — use a Project](https://wheelhouse-project.github.io/WheelHouse/#llm-perplexity)

All four work on the service's free plan, with one caveat: Perplexity
documents file uploads inside a project only for its paid plans.
