"""Reading ``[ai.server] kind`` (wh-ai-kind-validation).

The setting says whether the AI server Wheelhouse talks to is on this machine
or out on the internet. Two spellings are meaningful: ``"local"`` and
``"cloud"``.

Why this is a module and not four comparisons
=============================================
It used to be four comparisons, and they did not agree. Two sites asked
"is it cloud?" and two asked "is it local?", so a value that was neither took
the local path in one place and the cloud path in another:

* ``service._build_server_provider`` -- ``== "cloud"``, so ``"remote"`` was
  local, and the endpoint got the warning meant for a home-run server.
* ``service.refresh_models`` -- ``== "cloud"``, so ``"remote"`` refreshed the
  model list.
* ``service._launch_background_probes`` -- ``== "local"``, so ``"remote"``
  never started the loop that does the refreshing.
* ``state_manager._get_available_ai_providers`` -- ``== "local"``, so
  ``"remote"`` showed the cloud menu.

The result of a typo was not the wrong behaviour but an incoherent one, and
nothing said so. Every one of those sites now reads through this function, so
a typo produces one behaviour and one complaint.

What is forgiven and what is not
================================
config.toml is edited by hand. ``"Cloud"``, ``" cloud"``, and ``"CLOUD"`` are
accepted in silence -- the meaning is not in doubt, and refusing them would be
pedantry with a log line attached. Anything else falls back to ``"local"``,
which is what a missing key already gives, and returns a complaint.

Falling back to local rather than cloud is not a privacy decision: the setting
does not choose where text is sent. ``base_url`` does that, and it is a
separate setting the user typed on purpose. ``kind`` only frames how Wheelhouse
talks to whatever is at that address. Local is the fallback because it is the
documented default, so a key that cannot be read behaves exactly like a key
that is not there.

The complaint is returned, not logged
=====================================
Only the caller knows whether this is the first read of the setting or the
four-hundredth. ``state_manager`` rereads it every time the tray menu is
rebuilt; logging in here would print the same line all day. ``AIService``
says it once at startup.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

LOCAL = "local"
CLOUD = "cloud"

#: The spellings that mean something, in the form they take in config.toml.
KNOWN_KINDS = (LOCAL, CLOUD)


def normalize_server_kind(raw: Any) -> Tuple[str, Optional[str]]:
    """Read the configured server kind.

    Returns ``(kind, complaint)``. ``kind`` is always ``LOCAL`` or ``CLOUD``.
    ``complaint`` is a sentence for the log when the value could not be read,
    and ``None`` when it could -- including for ``None`` itself, which is what
    a caller passes when the key is absent and is the documented default
    rather than a mistake.

    Never raises: a bad value in config.toml must not stop Wheelhouse starting.
    """
    if raw is None:
        return LOCAL, None

    # TOML returns a str only for a quoted value. kind = true and kind = 3 are
    # valid TOML that parse to a bool and an int, and str(True) is "True",
    # which would compare equal to nothing and fall through the branch below
    # with a complaint that quotes 'True' as though the user had typed it in
    # quotes. Say plainly that the quotes are missing instead.
    if not isinstance(raw, str):
        return LOCAL, (
            f"[ai.server] kind is {raw!r}, which is not text. Write "
            f'kind = "{LOCAL}" or kind = "{CLOUD}" -- with the quotes. '
            f"Using {LOCAL}."
        )

    cleaned = raw.strip().lower()
    if cleaned in KNOWN_KINDS:
        return cleaned, None

    return LOCAL, (
        f"[ai.server] kind is {raw!r}, which is neither {LOCAL!r} nor "
        f"{CLOUD!r}. Using {LOCAL}, which is what leaving the setting out "
        f"does. If your server is a hosted one, spell it {CLOUD!r}."
    )
