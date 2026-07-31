"""The version string the About dialog shows.

There is no one place the version can be read from that works both in a source
checkout and in a build, so the helper asks whichever question fits the way
the program is running. Nothing but a line of dialog text depends on the
answer, so it reports "unknown" rather than raising.
"""

import sys
from unittest.mock import patch

from utils.app_version import UNKNOWN_VERSION, get_app_version


class TestASourceCheckout:

    def test_it_reads_the_version_file_at_the_top_of_the_repository(self):
        with patch("utils.app_version._version_from_repository_file",
                   return_value="1.2.3"):
            assert get_app_version() == "1.2.3"

    def test_it_does_not_ask_the_executable(self):
        """python.exe carries its own version, which is not this program's."""
        with patch("utils.app_version._version_from_repository_file",
                   return_value="1.2.3"), \
             patch("utils.app_version._version_from_executable") as from_exe:
            get_app_version()

        from_exe.assert_not_called()

    def test_a_missing_version_file_reports_unknown(self):
        with patch("utils.app_version._version_from_repository_file",
                   return_value=""):
            assert get_app_version() == UNKNOWN_VERSION

    def test_the_real_repository_file_is_where_the_helper_looks(self):
        """Binds the path, not just the reading of it.

        The helper counts directories upward from its own location. A file
        that moves, or a module that moves, silently turns the version into
        "unknown" without this.
        """
        from utils.app_version import _version_from_repository_file

        found = _version_from_repository_file()

        assert found
        assert found[0].isdigit()


class TestABuild:

    def test_it_reads_the_stamped_executable(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch("utils.app_version._version_from_executable",
                   return_value="1.0.5.0"):
            assert get_app_version() == "1.0.5.0"

    def test_it_does_not_look_for_a_repository_file(self):
        """A build has no repository around it to read from."""
        with patch.object(sys, "frozen", True, create=True), \
             patch("utils.app_version._version_from_executable",
                   return_value="1.0.5.0"), \
             patch("utils.app_version._version_from_repository_file") as from_repo:
            get_app_version()

        from_repo.assert_not_called()

    def test_an_unstamped_executable_reports_unknown(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch("utils.app_version._version_from_executable",
                   return_value=""):
            assert get_app_version() == UNKNOWN_VERSION


class TestNothingRaises:

    def test_a_read_that_fails_reports_an_empty_string(self):
        with patch("utils.app_version.Path.read_text",
                   side_effect=OSError("gone")):
            from utils.app_version import _version_from_repository_file

            assert _version_from_repository_file() == ""

    def test_a_version_resource_that_cannot_be_read_reports_an_empty_string(self):
        from utils.app_version import _version_from_executable

        with patch("ctypes.WinDLL", side_effect=OSError("no version.dll")):
            assert _version_from_executable() == ""
