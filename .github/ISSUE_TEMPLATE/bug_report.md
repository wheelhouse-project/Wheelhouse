---
name: Bug report
about: Something WheelHouse did wrong, or failed to do
title: ""
labels: bug
assignees: ""
---

<!--
Thank you for the report. If you depend on hands-free input and this bug
blocks you, say so in the first line -- those reports get priority.

For suspected security or privacy problems (dictated text showing up
somewhere it shouldn't, another process reading transcripts), do NOT file
a public issue; follow SECURITY.md instead.
-->

**What happened**

A clear description of what went wrong.

**What you expected**

What should have happened instead.

**Steps to reproduce**

1. Say / do '...'
2. ...

**Speech engine**

Parakeet (default) / Distil-Whisper / Google Cloud STT

**Environment**

- Windows version (e.g. Windows 11 23H2):
- WheelHouse version (`VERSION` file in the install directory, or the release tag):
- The application you were dictating into or controlling, if relevant:

**Log excerpt**

The log files are in the installation directory (`wheelhouse.log`). By
default they contain no dictated content (see PRIVACY.md), so they are
safe to attach; trim to the minutes around the problem if the file is
large.

If the problem is about recognition quality or garbled dictation, a log
recorded with `LOG_TRANSCRIPTS = true` in `config.toml` helps a lot --
but only attach one if you are comfortable with the dictated content it
contains being public.

**Anything else**

Screenshots, the control/window it failed on, how often it happens.
