"""Owner tokens cannot collide across the sources that share one dialog.

A1 of wh-dialog-ownership-token requires that the AI request, the
provider start, the GUI startup raise and the WebSocket completion
cannot produce the same token. A bare integer could not give that: two
integer counters already exist and can diverge --
``RemoteSTTLauncher._current_launch_generation`` and
``WebSocketManager._stt_client_generations`` -- so launch 2 and AI
request 2 would be one value. The source prefix is what makes the
guarantee structural, and these tests are what hold it there.
"""

import pytest

from shared.dialog_owner import (
    STARTUP_OWNER,
    launch_owner_token,
    next_ai_owner_token,
)


class TestTokensCannotCollideAcrossSources:
    def test_a_launch_token_names_its_source(self):
        assert launch_owner_token(2) == "stt:2"

    def test_a_caller_with_no_launch_names_no_operation(self):
        # None keeps its old meaning: this message names no operation.
        # The dialog treats that as a claim like any other, so it
        # matches only a dialog no operation owns.
        assert launch_owner_token(None) is None

    def test_an_ai_token_names_its_source(self):
        assert next_ai_owner_token().startswith("ai:")

    def test_the_startup_token_names_its_source(self):
        assert STARTUP_OWNER.startswith("gui:")

    @pytest.mark.parametrize("generation", [0, 1, 2, 17])
    def test_a_launch_never_collides_with_an_ai_request(self, generation):
        # The collision a bare integer would have allowed: launch 2 and
        # AI request 2 sharing one value, so either could dismiss the
        # other's dialog.
        assert launch_owner_token(generation) != next_ai_owner_token()

    @pytest.mark.parametrize("generation", [0, 1, 2, 17])
    def test_a_launch_never_collides_with_the_startup_plaque(self, generation):
        assert launch_owner_token(generation) != STARTUP_OWNER

    def test_an_ai_request_never_collides_with_the_startup_plaque(self):
        assert next_ai_owner_token() != STARTUP_OWNER

    def test_two_ai_requests_never_share_a_token(self):
        # Without this, an AI request that finishes late would dismiss a
        # later request's dialog -- the same defect one source in, and
        # the reason the token is not a constant.
        assert next_ai_owner_token() != next_ai_owner_token()

    def test_many_ai_requests_never_share_a_token(self):
        tokens = [next_ai_owner_token() for _ in range(200)]
        assert len(set(tokens)) == 200
