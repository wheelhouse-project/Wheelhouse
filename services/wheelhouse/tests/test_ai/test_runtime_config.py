"""Tests for the [ai.runtime] configuration section (wh-ai-runtime-config-section).

The two properties that matter here:

- The port is DERIVED from [ai.server] base_url, never stored in [ai.runtime].
  Storing it twice lets the launcher start a server on one port while the
  client talks to another, and nothing would report the mismatch.
- Validation never raises. A bad value disables the local runtime and leaves
  base_url pointing at whatever it pointed at, which is the pre-existing
  behaviour for an external server.
"""

import pytest

from ai.runtime_config import RuntimeConfig


def _raw(**overrides):
    """A valid [ai.runtime] table with overrides applied."""
    base = {
        "enabled": True,
        "model_path": r"C:\models\gemma.gguf",
        "binary_dir": r"C:\llama",
        "context_size": 8192,
        "gpu_layers": 99,
        "startup_timeout_seconds": 90,
    }
    base.update(overrides)
    return base


class TestThePortComesFromTheServerAddress:

    def test_port_is_read_from_base_url(self):
        cfg = RuntimeConfig.from_raw(_raw(), "http://127.0.0.1:8899/v1")
        assert cfg.port == 8899

    def test_a_different_base_url_moves_the_port(self):
        cfg = RuntimeConfig.from_raw(_raw(), "http://127.0.0.1:11434/v1")
        assert cfg.port == 11434

    def test_a_port_key_inside_the_section_is_ignored(self):
        """Writing a port in [ai.runtime] must not override base_url.

        If this ever passes the section's own value through, the launcher and
        the client can disagree about the address with nothing to catch it.
        """
        cfg = RuntimeConfig.from_raw(_raw(port=9999), "http://127.0.0.1:8899/v1")
        assert cfg.port == 8899

    def test_base_url_without_a_port_disables_the_runtime(self):
        cfg = RuntimeConfig.from_raw(_raw(), "https://example.com/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key == "ai.server.base_url"

    def test_an_empty_base_url_disables_the_runtime(self):
        cfg = RuntimeConfig.from_raw(_raw(), "")
        assert cfg.enabled is False
        assert cfg.invalid_key == "ai.server.base_url"

    def test_a_non_loopback_host_disables_the_runtime(self):
        """We start the server ourselves, so it can only be on this machine."""
        cfg = RuntimeConfig.from_raw(_raw(), "http://192.168.1.50:8899/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key == "ai.server.base_url"

    @pytest.mark.parametrize("base_url", [
        "http://[::1]:8899/v1",
        "http://[0:0:0:0:0:0:0:1]:8899/v1",
    ])
    def test_an_ipv6_loopback_address_disables_the_runtime(self, base_url):
        """The launcher can only serve the IPv4 loopback address.

        It starts the server with --host 127.0.0.1 and asks
        http://127.0.0.1:<port>/health whether it is ready. Accepting an IPv6
        address here would enable a runtime whose server binds one socket while
        the client connects to another; on Windows the two loopback stacks do
        not share a namespace, so the request fails against a server that is
        running perfectly well. Refusing the address says what this launcher
        can actually do, and the user keeps the external-server path for a
        server they start themselves on whatever address they like.
        """
        cfg = RuntimeConfig.from_raw(_raw(), base_url)
        assert cfg.enabled is False
        assert cfg.invalid_key == "ai.server.base_url"


class TestTheWholeAddressMustBeOneWeCanServe:
    """Every part of base_url, not only the host and the port.

    This validator exists to stop the launcher and the client disagreeing
    about the address. Checking the host and the port alone leaves several
    ways for them to disagree anyway, because the client builds its request
    URLs by appending to base_url: it posts to ``<base_url>/chat/completions``
    and asks ``<base_url>/models``. The launcher, meanwhile, always serves
    plain HTTP on 127.0.0.1 at the OpenAI v1 root.

    Each address below starts a server and then makes every spoken command
    fail, which is exactly the outcome the validator is supposed to prevent.
    Refusing it instead leaves the AI features off and says which key is
    wrong, and an external server at that address still works.
    """

    @pytest.mark.parametrize("base_url,why", [
        ("https://127.0.0.1:8899/v1",
         "the launcher serves plain HTTP, so the client would attempt TLS "
         "against a plaintext server"),
        ("ws://127.0.0.1:8899/v1",
         "not a scheme the client can post to at all"),
        ("http://127.0.0.1:8899",
         "no path: the client would post to /chat/completions, and the "
         "model-list fallback is disabled for a non-v1 address"),
        ("http://127.0.0.1:8899/",
         "the bare root, for the same reason"),
        ("http://127.0.0.1:8899/foo",
         "the client would post to /foo/chat/completions, which the server "
         "does not serve"),
        ("http://127.0.0.1:8899/v1/v1",
         "the client refuses a doubled /v1 outright"),
        ("http://127.0.0.1:0/v1",
         "port zero asks the operating system to choose a port, but both the "
         "health check and the client would still connect to port 0"),
        ("http://127.0.0.1:8899/v1?key=value",
         "the query would land in the middle of the built URL, before "
         "/chat/completions"),
        ("http://127.0.0.1:8899/v1#section",
         "the fragment would land in the middle of the built URL"),
        ("http://user:secret@127.0.0.1:8899/v1",
         "credentials in the address are not a thing we start a server with"),
    ])
    def test_an_address_the_launcher_cannot_serve_disables_the_runtime(
        self, base_url, why
    ):
        cfg = RuntimeConfig.from_raw(_raw(), base_url)
        assert cfg.enabled is False, f"should have been refused because {why}"
        assert cfg.invalid_key == "ai.server.base_url"

    @pytest.mark.parametrize("base_url", [
        "http://127.0.0.1:8899/v1",
        "http://localhost:8899/v1",
        # The client strips a trailing slash before appending, so this names
        # the same endpoint. Refusing it would fail a harmless address.
        "http://127.0.0.1:8899/v1/",
        "HTTP://127.0.0.1:8899/v1",
    ])
    def test_the_address_the_installer_writes_is_accepted(self, base_url):
        cfg = RuntimeConfig.from_raw(_raw(), base_url)
        assert cfg.enabled is True
        assert cfg.port == 8899

    def test_a_port_above_the_valid_range_disables_the_runtime(self):
        cfg = RuntimeConfig.from_raw(_raw(), "http://127.0.0.1:99999/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key == "ai.server.base_url"


class TestMissingAndDisabled:

    def test_an_absent_section_is_disabled_without_a_fault(self):
        cfg = RuntimeConfig.from_raw({}, "http://127.0.0.1:8899/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key is None

    def test_enabled_false_is_opt_out_not_a_fault(self):
        cfg = RuntimeConfig.from_raw(_raw(enabled=False), "http://127.0.0.1:8899/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key is None

    def test_a_non_table_section_is_disabled(self):
        cfg = RuntimeConfig.from_raw("nonsense", "http://127.0.0.1:8899/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key == "<not-a-table>"

    def test_missing_optional_keys_take_defaults(self):
        cfg = RuntimeConfig.from_raw(
            {"enabled": True, "model_path": "m.gguf", "binary_dir": "b"},
            "http://127.0.0.1:8899/v1",
        )
        assert cfg.enabled is True
        assert cfg.context_size == 8192
        assert cfg.gpu_layers == 99
        assert cfg.startup_timeout_seconds == 90

    def test_enabled_with_no_model_path_is_a_fault(self):
        cfg = RuntimeConfig.from_raw(
            {"enabled": True, "binary_dir": "b"}, "http://127.0.0.1:8899/v1"
        )
        assert cfg.enabled is False
        assert cfg.invalid_key == "model_path"

    def test_enabled_with_no_binary_dir_is_a_fault(self):
        cfg = RuntimeConfig.from_raw(
            {"enabled": True, "model_path": "m.gguf"}, "http://127.0.0.1:8899/v1"
        )
        assert cfg.enabled is False
        assert cfg.invalid_key == "binary_dir"


class TestBadValuesDisableRatherThanRaise:

    @pytest.mark.parametrize(
        "key,value",
        [
            ("context_size", 0),
            ("context_size", -1),
            ("context_size", "8192"),
            ("context_size", True),
            ("gpu_layers", -1),
            ("gpu_layers", "99"),
            ("gpu_layers", True),
            ("startup_timeout_seconds", 0),
            ("startup_timeout_seconds", -5),
            ("startup_timeout_seconds", True),
            ("model_path", 5),
            ("binary_dir", []),
            ("enabled", "yes"),
            ("enabled", 1),
        ],
    )
    def test_a_bad_value_disables_and_names_the_key(self, key, value):
        cfg = RuntimeConfig.from_raw(_raw(**{key: value}), "http://127.0.0.1:8899/v1")
        assert cfg.enabled is False
        assert cfg.invalid_key == key

    def test_gpu_layers_zero_means_processor_only_and_is_valid(self):
        cfg = RuntimeConfig.from_raw(_raw(gpu_layers=0), "http://127.0.0.1:8899/v1")
        assert cfg.enabled is True
        assert cfg.gpu_layers == 0

    def test_from_raw_never_raises_on_any_junk(self):
        for junk in (None, [], 5, "x", {"enabled": object()}):
            cfg = RuntimeConfig.from_raw(junk, "http://127.0.0.1:8899/v1")
            assert cfg.enabled is False
