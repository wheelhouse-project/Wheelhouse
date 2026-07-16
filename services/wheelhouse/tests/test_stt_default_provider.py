"""Guard: the STT provider fallback default matches the shipped template.

wh-stt-fallback-default-google. Two read-time fallbacks for
``stt.last_provider`` (main.py _switch_stt_provider and state_manager
_get_current_stt_provider) once hard-coded ``"google_stt"``. That cloud
provider needs a Google account. The shipped default in config.toml.example is
the local, offline ``parakeet_tdt`` provider, which needs no account. If the
config key were ever absent, a "google_stt" fallback would silently point a
no-account user at a broken cloud provider.

These fallbacks now use config_service.DEFAULT_STT_PROVIDER. This test keeps
that constant equal to the value config.toml.example actually ships, so the two
can never drift apart again.
"""

import tomllib
from pathlib import Path

from services.wheelhouse.config_service import DEFAULT_STT_PROVIDER

_EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.toml.example"


def _shipped_last_provider() -> str:
    with open(_EXAMPLE_CONFIG, "rb") as f:
        data = tomllib.load(f)
    return data["stt"]["last_provider"]


def test_default_matches_shipped_example():
    """The fallback default equals the provider config.toml.example ships."""
    assert DEFAULT_STT_PROVIDER == _shipped_last_provider()


def test_default_is_the_local_offline_provider():
    """Sanity: the default is the local Parakeet provider, not a cloud one."""
    assert DEFAULT_STT_PROVIDER == "parakeet_tdt"
