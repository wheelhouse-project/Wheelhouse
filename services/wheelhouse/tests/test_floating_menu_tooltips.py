"""Hover comments on the floating button's menu (wh-audio-suppression-floating-menu).

This class has its own file because it was written before the implementation
and had to stay red while the exact wording was an open decision with the
project owner. In ``tests/test_gui.py`` it would have sat beside
``TestTheTwoMenusOfferTheSameEntries``, which the floating button's "Audio
Suppression" entry turned green in that commit; keeping the two together would
have forced a red test into that commit. The wording was settled on 2026-09-16
and the tooltips ship in the same commit as this file, so the three tests
below pass. wh-audio-suppression-auto later removed the "Audio Suppression"
entry from both menus.

``menu_manager`` is imported rather than copied: the guards in this class
(``len(entries) >= 12``, both submenu titles) and the label-order comparison
in ``test_gui.py`` have to be looking at the same menu, and a second copy of
the fixture could drift from the first without either file failing.
"""

from tests.test_gui import menu_manager  # noqa: F401  (pytest fixture)


class TestEveryQtMenuEntryHasAHoverComment:
    """Every entry in the floating button's menu explains itself on hover.

    "Non-empty" proves nothing here. Qt answers ``toolTip()`` with the
    action's own text when no tooltip was ever set, and PySide6 turns
    ``setToolTip("")`` into the same fallback. Measured on PySide6 6.11.0 /
    Qt 6.11.0 in this checkout's venv:

        QAction("Speech Enabled").toolTip()                 -> 'Speech Enabled'
        QAction("Teach WheelHouse your voice...").toolTip()  -> 'Teach WheelHouse your voice'
        QAction("&Help").toolTip()                           -> 'Help'

    So the default is the text with "..." removed, "&" removed, and the
    result trimmed -- which also means a test asserting only
    ``toolTip() != text()`` would pass today on the ellipsis entry and prove
    nothing about it. Each assertion below compares against the default Qt
    itself produces for that exact text, asked of a throwaway QAction rather
    than copied from Qt's rule, so a later Qt change cannot make the test
    lenient without being noticed.
    """

    def _default_tooltip(self, text):
        """What Qt returns from toolTip() when no tooltip was ever set."""
        from PySide6.QtGui import QAction
        return QAction(text).toolTip()

    def _entries(self, menu):
        """Every action of the menu that is not a separator.

        A separator carries no entry of its own: ``isSeparator()`` is True and
        its text is empty (measured), so a tooltip on one would mean nothing.
        """
        return [action for action in menu.actions() if not action.isSeparator()]

    def _submenus(self, menu):
        """Submenu title -> submenu, for every entry that opens one."""
        return {action.text(): action.menu() for action in self._entries(menu)
                if action.menu() is not None}

    def _without_hover_comments(self, actions):
        """(text, toolTip) for every action with no hover comment of its own."""
        return [(action.text(), action.toolTip()) for action in actions
                if not action.toolTip()
                or action.toolTip() == self._default_tooltip(action.text())]

    def test_every_top_level_entry_explains_itself(self, menu_manager):
        menu = menu_manager._create_menu(is_tray_menu=False)
        entries = self._entries(menu)
        assert len(entries) >= 12, ("guard: the menu built its entries",
                                    [a.text() for a in entries])
        assert self._without_hover_comments(entries) == []

    def test_every_submenu_entry_explains_itself(self, menu_manager):
        menu = menu_manager._create_menu(is_tray_menu=False)
        submenus = self._submenus(menu)
        assert sorted(submenus) == ["AI Model", "STT Provider"], sorted(submenus)
        missing = {}
        for title, submenu in submenus.items():
            entries = self._entries(submenu)
            assert entries, f"guard: {title} built no children"
            missing[title] = self._without_hover_comments(entries)
        assert missing == {"STT Provider": [], "AI Model": []}

    def test_the_menus_let_the_hover_comments_be_seen(self, menu_manager):
        """A tooltip nobody can see is not a hover comment.

        A QMenu hides its actions' tooltips unless ``setToolTipsVisible(True)``
        is called on it; False is the default, measured on Qt 6.11.0 for both
        a top-level QMenu and a submenu. Each of the three menus needs the
        call, so each is named in the comparison.
        """
        menu = menu_manager._create_menu(is_tray_menu=False)
        submenus = self._submenus(menu)
        assert sorted(submenus) == ["AI Model", "STT Provider"], sorted(submenus)
        visible = {"the menu itself": menu.toolTipsVisible()}
        for title, submenu in submenus.items():
            visible[title] = submenu.toolTipsVisible()
        assert visible == {"the menu itself": True,
                           "STT Provider": True, "AI Model": True}


class TestTheAiModelEntriesNoOtherTestBuilds:
    """The three AI Model entries the rest of the suite never builds.

    ``menu_manager`` sets ``ai_providers_available`` to two real model names and
    ``ai_provider`` to one of them (tests/test_gui.py:2773-2774), so three
    entries of the AI Model submenu are never built anywhere in the suite: the
    "AI not configured" placeholder, the "AI disabled" placeholder, and the
    "(configured: X -- not available)" entry that appears when the configured
    model is missing from the list the server offers. Nothing else proves those
    three carry a hover comment at all.

    These three tests pin the exact text, where the class above deliberately
    compares against the default Qt itself produces instead. The reason is that
    a copy of a neighbour's text would pass a not-the-default check, and a
    swapped text is the defect this coverage exists to catch: one if/elif/else
    chooses the label and the hover comment together (gui.py:4084-4096), so the
    two can be paired wrongly. The cost is stated rather than hidden: a reworded
    text fails these three tests and the new text must be written here too.

    ``ai_provider`` is None in the two placeholder tests on purpose. The
    absent-model branch runs when ``ai_provider is not None and ai_provider not
    in ai_providers_available`` (gui.py:4107-4111), so a non-None value there
    would add a fourth entry and the comparison would no longer be exact.
    """

    def _ai_model_entries(self, manager):
        """Entry text -> hover comment, for every AI Model submenu entry."""
        menu = manager._create_menu(is_tray_menu=False)
        submenus = {action.text(): action.menu() for action in menu.actions()
                    if action.menu() is not None}
        assert "AI Model" in submenus, sorted(submenus)
        return {action.text(): action.toolTip()
                for action in submenus["AI Model"].actions()
                if not action.isSeparator()}

    def test_the_unconfigured_placeholder_explains_itself(self, menu_manager):
        menu_manager.ai_providers_available = ["__ai_unconfigured__"]
        menu_manager.ai_provider = None
        assert self._ai_model_entries(menu_manager) == {
            "AI not configured":
                "The settings file names no AI server, so Wheelhouse can offer"
                " no model.",
        }

    def test_the_disabled_placeholder_explains_itself(self, menu_manager):
        menu_manager.ai_providers_available = ["__ai_disabled__"]
        menu_manager.ai_provider = None
        assert self._ai_model_entries(menu_manager) == {
            "AI disabled": "The settings file switches the AI features off.",
        }

    def test_the_absent_configured_model_explains_itself(self, menu_manager):
        """The entry that names a configured model no list entry offers.

        The hover comment says only that the model is not among the models on
        offer, and it says nothing about why. Two different situations build
        this entry: the server answered with a list that lacks the configured
        model, and the settings file switched the AI features off or named no
        server at all, which puts one sentinel in the list while
        ``_get_current_ai_provider`` still returns the configured model
        (gui.py:1652-1693). A sentence naming the server would be wrong in the
        second situation, so David chose wording that is true in both
        (finding wh-audio-suppression-floating-menu.1.2, item 6 option four).

        The entry's own label is not pinned. It is built from
        ``_get_ai_provider_display_name``, which falls back to
        ``provider.replace("_", " ").title()`` for a name it does not know
        (gui.py:3608-3612), so pinning the label would test that helper rather
        than the hover comment. The prefix identifies the entry instead.
        """
        menu_manager.ai_providers_available = ["gpt-oss-20b", "qwen3-coder-30b"]
        menu_manager.ai_provider = "a-model-the-server-dropped"
        entries = self._ai_model_entries(menu_manager)
        absent = [text for text in entries if text.startswith("(configured:")]
        assert len(absent) == 1, sorted(entries)
        assert entries[absent[0]] == (
            "The settings file names this model. It is not among the models"
            " on offer."
        )
        # The two real entries keep their own text while that branch runs.
        others = {text: comment for text, comment in entries.items()
                  if text != absent[0]}
        assert set(others.values()) == {
            "Use this model for the AI features."
        }, others
