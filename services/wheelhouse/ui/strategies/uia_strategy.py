"""UIAStrategy -- the Phase 1 element-finding strategy (wh-agd2v).

The v5 design positions ``ElementFinder`` as the coordinator and ``UIAStrategy``
as the one find-strategy it drives in Phase 1 (later phases add ``OCRStrategy``
and ``CompositeStrategy``). In Phase 1 the strategy is a thin seam over the
coordinator: it delegates ``find(query, foreground)`` straight to the
:class:`~ui.element_finder.ElementFinder`, which owns the UIA walk, the browser
DOM corrections, the confidence scorer, the clear-winner rule, and the
WalkSnapshot storage.

Keeping the strategy as an explicit object (rather than calling the finder
directly from UIActionHandler) preserves the v5 module layout and the seam a
later ``CompositeStrategy`` will slot into, without duplicating any logic. The
authoritative spec is docs/plans/2026-05-21-voice-element-clicking-design-v5.md,
sections "Key types" and "Module layout".

This strategy does NOT click anything (the ``ClickExecutor`` is a later slice,
wh-mzpvx) and reads no config -- the thresholds live on the ``ElementFinder``
it was constructed with.
"""

from __future__ import annotations

from ui.element_finder import ElementFinder, FindResult, ForegroundContext
from ui.element_types import ElementQuery


class UIAStrategy:
    """Phase 1 find-strategy: delegate to the ElementFinder coordinator."""

    def __init__(self, finder: ElementFinder) -> None:
        self._finder = finder

    def find(
        self, query: ElementQuery, foreground: ForegroundContext
    ) -> FindResult:
        """Find the best element for ``query`` in the focused window.

        Delegates to :meth:`ElementFinder.find`, which performs the UIA walk,
        applies browser DOM corrections for Chromium-family processes
        (query_has_role=False so all three fold rules are reachable), scores
        and filters via the confidence scorer, runs the clear-winner rule,
        stores the WalkSnapshot, and returns the decide Outcome together with
        the full Input-local WalkSnapshot and the plain-data
        WalkSnapshotSummary. Does NOT click.
        """
        return self._finder.find(query, foreground)


__all__ = ["UIAStrategy"]
