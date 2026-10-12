"""Tests for update_install: the decision and the preparation of an update.

No test makes a network request. Every fetch is a fake passed in as an
argument, as in test_update_check.py. No test starts a process.
"""

import ast
import hashlib
import json
import logging
import re
import urllib.parse
from pathlib import Path

import pytest

import update_check
import update_install

TAG = "v1.2.3"
REPO_PREFIX = "https://github.com/wheelhouse-project/Wheelhouse/releases/download/"
SCRIPT_URL = REPO_PREFIX + TAG + "/install-wheelhouse.ps1"
TAG_URL = "https://api.github.com/repos/wheelhouse-project/Wheelhouse/releases/tags/" + TAG
SCRIPT = b"# installer script\r\nWrite-Host 'hi'\r\n"
SCRIPT_SHA = hashlib.sha256(SCRIPT).hexdigest()
# The development tree keeps the installer in scripts/release/public. The
# public release tree puts the same file at the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = _REPO_ROOT / "scripts" / "release" / "public"
if not RELEASE_DIR.is_dir():
    RELEASE_DIR = _REPO_ROOT


def _release(*, digest="absent", url=SCRIPT_URL, name="install-wheelhouse.ps1", size=len(SCRIPT)):
    asset = {"name": name, "browser_download_url": url}
    if digest != "absent":
        asset["digest"] = digest
    if size != "absent":
        asset["size"] = size
    return {
        "tag_name": TAG,
        "assets": [
            {
                "name": "Wheelhouse-Setup.exe",
                "browser_download_url": REPO_PREFIX + TAG + "/Wheelhouse-Setup.exe",
                "digest": "sha256:" + "0" * 64,
            },
            asset,
        ],
    }


class FakeGet:
    """A fetch that answers from a table and records each address."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, running_version):
        self.calls.append((url, running_version))
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict):
            return json.dumps(answer).encode()
        return answer


def _prepare_get(release, script=SCRIPT):
    return FakeGet({TAG_URL: release, SCRIPT_URL: script})


# --- constants are reused, not copied --------------------------------------


def test_the_error_type_is_the_one_update_check_defines():
    assert update_install.UpdateCheckError is update_check.UpdateCheckError


def test_the_limits_are_the_ones_update_check_defines():
    # Python shares small integers and short strings, so "is" cannot tell a
    # copied literal from an import. The check also reads the module source:
    # no module-level assignment may bind one of these names.
    assert update_install.MAX_RESPONSE_BYTES == update_check.MAX_RESPONSE_BYTES
    # One request function serves the check and the download. The identity
    # test is exact, because a function object is not shared by value. It
    # also proves that this module keeps no copy of the User-Agent header or
    # of the timeout, which now belong to that one function.
    assert update_install.http_get is update_check.http_get
    tree = ast.parse(Path(update_install.__file__).read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    for name in ("REQUEST_TIMEOUT_SECONDS", "MAX_RESPONSE_BYTES", "USER_AGENT_PREFIX"):
        assert name not in assigned


def test_the_asset_name_is_the_installer_script_name():
    assert update_install.INSTALLER_ASSET_NAME == "install-wheelhouse.ps1"
    # The release directory is part of this repository, so a missing file fails.
    assert (RELEASE_DIR / update_install.INSTALLER_ASSET_NAME).is_file()


# --- the address of one tag ------------------------------------------------


def test_the_release_address_names_the_tag_in_the_same_repository():
    assert update_install.release_url_for_tag(TAG) == TAG_URL
    assert update_install.release_url_for_tag(TAG).startswith(
        update_check.RELEASES_URL.removesuffix("latest")
    )


@pytest.mark.parametrize("bad", ["", "latest", "v1.2/../x", "v1.2.3?x=1", "1.2.3 ", None, 5])
def test_a_tag_that_is_not_a_version_is_refused(bad):
    with pytest.raises(update_check.UpdateCheckError):
        update_install.release_url_for_tag(bad)


@pytest.mark.parametrize(
    "address",
    [
        # Fails both halves of the check.
        pytest.param("https://example.com/other/path", id="neither-half"),
        # Ends in /releases/latest but has no /repos/ segment: only the
        # prefix half refuses it.
        pytest.param("https://api.github.com/other/releases/latest", id="no-repos-prefix"),
        # Starts with /repos/ but does not end in /releases/latest: only the
        # suffix half refuses it.
        pytest.param("https://api.github.com/repos/x/y/other", id="no-latest-suffix"),
    ],
)
def test_an_unexpected_releases_address_form_is_refused(monkeypatch, address):
    monkeypatch.setattr(update_install, "RELEASES_URL", address)
    with pytest.raises(update_check.UpdateCheckError, match="unexpected form"):
        update_install.find_installer_asset(_release(), TAG)


# --- finding the asset -----------------------------------------------------


def test_find_asset_returns_the_address_and_the_digest():
    asset = update_install.find_installer_asset(_release(digest="sha256:" + SCRIPT_SHA), TAG)
    assert asset.url == SCRIPT_URL
    assert asset.sha256 == SCRIPT_SHA


def test_find_asset_reads_the_size():
    asset = update_install.find_installer_asset(_release(), TAG)
    assert asset.size == len(SCRIPT)


@pytest.mark.parametrize("size", ["absent", None, "5", 5.0, True, 0, -1])
def test_find_asset_refuses_a_size_that_is_not_a_positive_integer(size):
    with pytest.raises(update_check.UpdateCheckError, match="size"):
        update_install.find_installer_asset(_release(size=size), TAG)


def test_find_asset_refuses_a_size_over_the_limit_and_names_both_numbers():
    too_big = update_check.MAX_RESPONSE_BYTES + 1
    with pytest.raises(update_check.UpdateCheckError) as raised:
        update_install.find_installer_asset(_release(size=too_big), TAG)
    assert str(too_big) in str(raised.value)
    assert str(update_check.MAX_RESPONSE_BYTES) in str(raised.value)


def test_find_asset_accepts_a_size_equal_to_the_limit():
    asset = update_install.find_installer_asset(
        _release(size=update_check.MAX_RESPONSE_BYTES), TAG
    )
    assert asset.size == update_check.MAX_RESPONSE_BYTES


def test_find_asset_without_a_digest_field_gives_no_digest():
    asset = update_install.find_installer_asset(_release(), TAG)
    assert asset.sha256 is None


def test_find_asset_with_a_null_digest_gives_no_digest():
    asset = update_install.find_installer_asset(_release(digest=None), TAG)
    assert asset.sha256 is None


def test_find_asset_reads_an_upper_case_digest():
    asset = update_install.find_installer_asset(
        _release(digest="sha256:" + SCRIPT_SHA.upper()), TAG
    )
    assert asset.sha256 == SCRIPT_SHA


@pytest.mark.parametrize(
    "digest",
    [
        "sha512:" + "a" * 128,
        "sha256:abc",
        "sha256:" + "g" * 64,
        "abc",
        "",
        7,
        ["x"],
        # A valid digest followed by more text: only a full match refuses it.
        "sha256:" + "a" * 64 + "zz",
        "sha256:" + "a" * 64 + "\n",
    ],
)
def test_find_asset_refuses_a_digest_it_cannot_read(digest):
    with pytest.raises(update_check.UpdateCheckError):
        update_install.find_installer_asset(_release(digest=digest), TAG)


@pytest.mark.parametrize(
    "release", [{}, {"assets": []}, {"assets": "x"}, {"assets": [1, None]}, []]
)
def test_find_asset_refuses_an_answer_without_the_script(release):
    with pytest.raises(update_check.UpdateCheckError):
        update_install.find_installer_asset(release, TAG)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/wheelhouse-project/Wheelhouse/releases/download/v1.2.3/install-wheelhouse.ps1",
        "https://evil.example/wheelhouse-project/Wheelhouse/releases/download/v1.2.3/install-wheelhouse.ps1",
        REPO_PREFIX + "v9.9.9/install-wheelhouse.ps1",
        "https://github.com/other/Wheelhouse/releases/download/v1.2.3/install-wheelhouse.ps1",
        REPO_PREFIX + "v1.2.3/other.ps1",
        None,
        5,
    ],
)
def test_find_asset_refuses_an_address_that_is_not_the_script_of_that_tag(url):
    with pytest.raises(update_check.UpdateCheckError):
        update_install.find_installer_asset(_release(url=url), TAG)


@pytest.mark.parametrize("tag", ["latest", "v1.2.3?x=1", ""])
def test_find_asset_refuses_a_tag_that_is_not_a_version_even_with_its_own_address(tag):
    # The asset address is the exact one for that tag text, so only the tag
    # check can refuse it. fetch_installer_asset refuses such a tag earlier,
    # so this calls find_installer_asset directly.
    url = REPO_PREFIX + urllib.parse.quote(tag, safe="") + "/install-wheelhouse.ps1"
    with pytest.raises(update_check.UpdateCheckError, match="not a version"):
        update_install.find_installer_asset(_release(url=url), tag)


# --- the request for one release -------------------------------------------


def test_fetch_asset_asks_for_the_tag_address_once():
    get = _prepare_get(_release())
    asset = update_install.fetch_installer_asset(TAG, "1.0.0", get=get)
    assert get.calls == [(TAG_URL, "1.0.0")]
    assert asset.url == SCRIPT_URL


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, size=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_default_get_sends_one_get_with_only_a_wheelhouse_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(update_install.urllib.request, "urlopen", fake_urlopen)
    assert update_install.http_get(TAG_URL, "1.0.0") == b"{}"
    request = seen["request"]
    assert request.full_url == TAG_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.header_items() == [("User-agent", "Wheelhouse/1.0.0")]
    assert seen["timeout"] == update_check.REQUEST_TIMEOUT_SECONDS


def test_default_get_wraps_network_errors(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(update_install.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(update_check.UpdateCheckError, match="offline"):
        update_install.http_get(TAG_URL, "1.0.0")


def test_default_get_refuses_an_answer_over_the_limit(monkeypatch):
    big = b"x" * (update_check.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        update_install.urllib.request, "urlopen", lambda request, timeout=None: _Response(big)
    )
    with pytest.raises(update_check.UpdateCheckError, match="larger than the limit"):
        update_install.http_get(TAG_URL, "1.0.0")


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[1]", b"null", b'"x"'])
def test_fetch_asset_refuses_an_answer_it_cannot_read(body):
    get = FakeGet({TAG_URL: body})
    with pytest.raises(update_check.UpdateCheckError):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_refuses_an_answer_over_the_limit_from_any_fetch():
    # The answer is a readable release followed by blanks, so nothing but the
    # limit can refuse it.
    padded = json.dumps(_release()).encode() + b" " * (update_check.MAX_RESPONSE_BYTES + 1)
    assert len(padded) > update_check.MAX_RESPONSE_BYTES
    get = FakeGet({TAG_URL: padded})
    with pytest.raises(update_check.UpdateCheckError, match="larger than the limit"):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_wraps_an_error_of_another_type_from_the_fetch():
    get = FakeGet({TAG_URL: RuntimeError("boom")})
    with pytest.raises(update_check.UpdateCheckError, match="boom"):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_does_not_catch_a_keyboard_interrupt():
    get = FakeGet({TAG_URL: KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_refuses_an_answer_nested_too_deeply_to_parse():
    deep = b"[" * 100_000 + b"]" * 100_000
    get = FakeGet({TAG_URL: deep})
    with pytest.raises(update_check.UpdateCheckError, match="not readable"):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_refuses_an_answer_that_is_not_bytes():
    get = FakeGet({TAG_URL: "{}"})
    with pytest.raises(update_check.UpdateCheckError, match="not bytes"):
        update_install.fetch_installer_asset(TAG, "1.0.0", get=get)


def test_fetch_asset_accepts_an_answer_given_as_a_bytearray():
    get = FakeGet({TAG_URL: bytearray(json.dumps(_release()).encode())})
    asset = update_install.fetch_installer_asset(TAG, "1.0.0", get=get)
    assert asset.url == SCRIPT_URL


# --- the download and the digest check -------------------------------------


def test_download_keeps_the_file_when_the_digest_matches(tmp_path):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, SCRIPT_SHA, len(SCRIPT))
    path = update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert path.read_bytes() == SCRIPT
    assert path.name == "install-wheelhouse.ps1"
    assert path.parent.parent == tmp_path
    assert path.parent.name.startswith("wheelhouse-update-")
    assert get.calls == [(SCRIPT_URL, "1.0.0")]


def test_download_raises_on_a_mismatch_and_leaves_no_file(tmp_path):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, "0" * 64, len(SCRIPT))
    with pytest.raises(update_check.UpdateCheckError, match="digest"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_without_a_digest_keeps_the_file_and_logs_one_line(tmp_path, caplog):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    with caplog.at_level(logging.INFO, logger="update_install"):
        path = update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert path.read_bytes() == SCRIPT
    lines = [r for r in caplog.records if "no digest" in r.getMessage().lower()]
    assert len(lines) == 1


def test_download_with_a_digest_logs_no_missing_digest_line(tmp_path, caplog):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, SCRIPT_SHA, len(SCRIPT))
    with caplog.at_level(logging.INFO, logger="update_install"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert not [r for r in caplog.records if "no digest" in r.getMessage().lower()]


def test_download_over_the_limit_leaves_no_file(tmp_path):
    big = b"x" * (update_check.MAX_RESPONSE_BYTES + 1)
    get = _prepare_get(_release(), script=big)
    # The asset size equals the body length, so the exact-length check passes
    # and only the limit can refuse the body. A caller can build this asset
    # directly, without find_installer_asset (which refuses such a size).
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(big))
    with pytest.raises(update_check.UpdateCheckError, match="larger than the limit"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_fetch_error_leaves_no_file(tmp_path):
    get = FakeGet({SCRIPT_URL: OSError("reset")})
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    with pytest.raises(update_check.UpdateCheckError):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_empty_body_is_refused(tmp_path):
    get = _prepare_get(_release(), script=b"")
    # Size 0 equals the empty body, so the exact-length check passes and only
    # the empty check can refuse it.
    asset = update_install.InstallerAsset(SCRIPT_URL, None, 0)
    with pytest.raises(update_check.UpdateCheckError, match="empty"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_to_a_folder_that_cannot_be_used_raises_the_one_error(tmp_path):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    missing = tmp_path / "no" / "such"
    with pytest.raises(update_check.UpdateCheckError):
        update_install.download_installer(asset, "1.0.0", get=get, parent=missing)


def test_download_removes_its_folder_when_the_file_cannot_be_saved(tmp_path, monkeypatch):
    def refuse(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    with pytest.raises(update_check.UpdateCheckError, match="disk full"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_two_downloads_use_two_different_files(tmp_path):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    first = update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    second = update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert first != second


@pytest.mark.parametrize("extra", [-1, 1])
def test_download_refuses_a_body_that_is_not_the_size_in_the_answer(tmp_path, extra):
    get = _prepare_get(_release())
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT) + extra)
    with pytest.raises(update_check.UpdateCheckError, match="size"):
        update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_accepts_a_script_given_as_a_bytearray(tmp_path):
    get = _prepare_get(_release(), script=bytearray(SCRIPT))
    asset = update_install.InstallerAsset(SCRIPT_URL, None, len(SCRIPT))
    path = update_install.download_installer(asset, "1.0.0", get=get, parent=tmp_path)
    assert path.read_bytes() == SCRIPT


@pytest.mark.parametrize("digest", ["absent", "sha256:" + SCRIPT_SHA])
def test_prepare_refuses_a_cut_download_with_or_without_a_digest(tmp_path, digest):
    cut = SCRIPT[: len(SCRIPT) // 2]
    get = _prepare_get(_release(digest=digest), script=cut)
    with pytest.raises(update_check.UpdateCheckError):
        update_install.prepare_update(TAG, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_shipped_installer_script_fits_under_the_download_limit():
    script = RELEASE_DIR / update_install.INSTALLER_ASSET_NAME
    assert script.is_file(), f"the installer script is missing: {script}"
    assert script.stat().st_size < update_check.MAX_RESPONSE_BYTES


# --- the whole preparation -------------------------------------------------


def test_prepare_returns_the_path_and_checks_the_digest(tmp_path):
    get = _prepare_get(_release(digest="sha256:" + SCRIPT_SHA))
    prepared = update_install.prepare_update(TAG, "1.0.0", get=get, parent=tmp_path)
    assert prepared.tag == TAG
    assert prepared.digest_checked is True
    assert prepared.script_path.read_bytes() == SCRIPT
    assert [url for url, _ in get.calls] == [TAG_URL, SCRIPT_URL]


def test_prepare_refuses_a_script_that_does_not_match_the_digest(tmp_path):
    get = _prepare_get(_release(digest="sha256:" + "1" * 64))
    with pytest.raises(update_check.UpdateCheckError):
        update_install.prepare_update(TAG, "1.0.0", get=get, parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_prepare_without_a_digest_says_so_in_the_result(tmp_path):
    get = _prepare_get(_release())
    prepared = update_install.prepare_update(TAG, "1.0.0", get=get, parent=tmp_path)
    assert prepared.digest_checked is False


def test_prepare_turns_an_ordinary_failure_into_the_one_error(tmp_path):
    get = FakeGet({TAG_URL: ValueError("bad")})
    with pytest.raises(update_check.UpdateCheckError):
        update_install.prepare_update(TAG, "1.0.0", get=get, parent=tmp_path)


def test_the_module_never_exits_the_process_or_starts_one():
    source = Path(update_install.__file__).read_text(encoding="utf-8")
    for word in ("sys.exit(", "os._exit(", "os.kill(", "subprocess", "Popen"):
        assert word not in source


# --- the parameters of the installer ---------------------------------------


class FakeConfig:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_parameters_pass_the_answers_of_the_current_install():
    config = FakeConfig(**{"stt.last_provider": "google_stt"})
    assert update_install.build_installer_parameters(config, auto_start=True) == [
        "-SttProvider", "google_stt", "-StartNow", "yes", "-AutoStart", "yes",
    ]


def test_parameters_pass_no_when_the_start_at_login_shortcut_is_absent():
    config = FakeConfig(**{"stt.last_provider": "parakeet_tdt"})
    assert update_install.build_installer_parameters(config, auto_start=False) == [
        "-SttProvider", "parakeet_tdt", "-StartNow", "yes", "-AutoStart", "no",
    ]


def test_parameters_read_the_shortcut_when_the_caller_gives_no_answer(monkeypatch):
    monkeypatch.setattr(update_install.parakeet_model, "startup_shortcut_exists", lambda: True)
    config = FakeConfig(**{"stt.last_provider": "distil_medium_en"})
    assert update_install.build_installer_parameters(config)[-2:] == ["-AutoStart", "yes"]
    monkeypatch.setattr(update_install.parakeet_model, "startup_shortcut_exists", lambda: False)
    assert update_install.build_installer_parameters(config)[-2:] == ["-AutoStart", "no"]


@pytest.mark.parametrize(
    "value", [None, "", "whisper", "PARAKEET_TDT", 5, ["google_stt"], "-Force"]
)
def test_parameters_leave_out_a_provider_the_installer_would_reject(value):
    config = FakeConfig(**{"stt.last_provider": value})
    params = update_install.build_installer_parameters(config, auto_start=False)
    assert params == ["-StartNow", "yes", "-AutoStart", "no"]


def test_parameters_log_one_warning_for_a_rejected_engine_name(caplog):
    config = FakeConfig(**{"stt.last_provider": "whisper"})
    with caplog.at_level(logging.WARNING, logger="update_install"):
        update_install.build_installer_parameters(config, auto_start=False)
    lines = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1
    assert "'whisper'" in lines[0].getMessage()


def test_parameters_log_no_warning_when_the_engine_is_absent_or_valid(caplog):
    with caplog.at_level(logging.WARNING, logger="update_install"):
        update_install.build_installer_parameters(FakeConfig(), auto_start=False)
        update_install.build_installer_parameters(
            FakeConfig(**{"stt.last_provider": "google_stt"}), auto_start=False
        )
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_parameters_pass_no_engine_when_the_config_has_no_engine_key():
    assert update_install.build_installer_parameters(FakeConfig(), auto_start=False) == [
        "-StartNow", "yes", "-AutoStart", "no",
    ]


def test_parameters_ask_the_config_for_the_provider_key_the_app_writes():
    asked = []

    class Config:
        def get(self, key, default=None):
            asked.append(key)
            return default

    update_install.build_installer_parameters(Config(), auto_start=False)
    assert asked == ["stt.last_provider"]


def test_parameters_match_the_installer_parameter_block():
    script = RELEASE_DIR / "install-wheelhouse.ps1"
    assert script.is_file(), f"the installer script is missing: {script}"
    text = script.read_text(encoding="utf-8-sig")
    block = text[text.index("param("):text.index("$ErrorActionPreference")]
    for name in ("StartNow", "AutoStart"):
        assert f"[string]${name}" in block
    # The ValidateSet line directly before [string]$SttProvider lists the
    # engines the installer accepts. The two sets must be equal.
    match = re.search(r"\[ValidateSet\(([^)]*)\)\]\s*\[string\]\$SttProvider", block)
    assert match, "no ValidateSet precedes [string]$SttProvider"
    installer_names = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert installer_names == set(update_install.VALID_STT_PROVIDERS)


# --- the engine of the running Wheelhouse (wh-release-update-check.1.3) ------


def _scalar_stt_config(tmp_path):
    """A real ConfigService loaded from a file with the scalar form
    ``stt = "whisper"``. Valid TOML: Wheelhouse loads it and keeps running,
    and get("stt.last_provider") reads through it as a missing key."""
    from config_service import ConfigService

    path = tmp_path / "config.toml"
    path.write_text('stt = "whisper"\n', encoding="utf-8")
    return ConfigService(str(path))


def test_a_scalar_stt_config_still_passes_the_running_engine(tmp_path):
    config = _scalar_stt_config(tmp_path)
    assert config.get("stt.last_provider", None) is None, "the scalar form must read as absent"
    params = update_install.build_installer_parameters(
        config, auto_start=False, running_engine="google_stt"
    )
    assert params == ["-SttProvider", "google_stt", "-StartNow", "yes", "-AutoStart", "no"]


def test_the_running_engine_wins_over_a_different_valid_last_provider():
    config = FakeConfig(**{"stt.last_provider": "parakeet_tdt"})
    params = update_install.build_installer_parameters(
        config, auto_start=False, running_engine="distil_medium_en"
    )
    assert params[:2] == ["-SttProvider", "distil_medium_en"]
    assert params.count("-SttProvider") == 1


@pytest.mark.parametrize("running", ["whisper", "", "PARAKEET_TDT", 5, ["google_stt"], "-Force"])
def test_an_invalid_running_engine_falls_back_to_a_valid_last_provider(running, caplog):
    config = FakeConfig(**{"stt.last_provider": "google_stt"})
    with caplog.at_level(logging.DEBUG, logger="update_install"):
        params = update_install.build_installer_parameters(
            config, auto_start=False, running_engine=running
        )
    assert params == ["-SttProvider", "google_stt", "-StartNow", "yes", "-AutoStart", "no"]
    lines = [r for r in caplog.records if r.name == "update_install"]
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert repr(running) in lines[0].getMessage()


def test_neither_source_valid_leaves_the_engine_out_and_logs_one_line(caplog):
    config = FakeConfig(**{"stt.last_provider": "whisper"})
    with caplog.at_level(logging.DEBUG, logger="update_install"):
        params = update_install.build_installer_parameters(
            config, auto_start=False, running_engine="nonsense"
        )
    assert params == ["-StartNow", "yes", "-AutoStart", "no"]
    lines = [r for r in caplog.records if r.name == "update_install"]
    assert len(lines) == 1
    assert "'nonsense'" in lines[0].getMessage()
    assert "'whisper'" in lines[0].getMessage()


def test_no_source_at_all_leaves_the_engine_out_and_logs_one_line(caplog):
    with caplog.at_level(logging.DEBUG, logger="update_install"):
        params = update_install.build_installer_parameters(FakeConfig(), auto_start=False)
    assert params == ["-StartNow", "yes", "-AutoStart", "no"]
    lines = [r for r in caplog.records if r.name == "update_install"]
    assert len(lines) == 1
