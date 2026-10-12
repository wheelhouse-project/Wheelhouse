"""Prepare an update: find, download and check the installer of one release.

This is the first half of "Update now" (wh-release-update-check, acceptance
item 6). It decides which script to run and makes the script ready. It does
not start the script, it does not close Wheelhouse, and it does not touch the
interface. A later stage calls these functions and then starts the installer
through the wait-for-exit path in ``stt/parakeet_model.py``.

Steps:

1. ``fetch_installer_asset`` asks GitHub for the release of one exact tag
   (not "latest") and reads the address and the SHA-256 digest of the
   installer script asset.
2. ``download_installer`` saves the script in a new temporary folder and
   checks its SHA-256 against the digest when the answer carries one.
3. ``build_installer_parameters`` lists the installer parameters that carry
   the answers of the current install.

``prepare_update`` runs steps 1 and 2 in order.

Every failure raises ``UpdateCheckError``. Nothing here ends or signals a
process. These functions block: call them from a worker thread.

Privacy contract: each request is a plain GET. It carries only a User-Agent
that names Wheelhouse and its version, as the update check does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from stt import parakeet_model
from update_check import (
    MAX_RESPONSE_BYTES,
    RELEASES_URL,
    UpdateCheckError,
    http_get,
    parse_version,
)

logger = logging.getLogger(__name__)

# The release asset is the public installer script. The name is the one the
# Setup program and the one-line command already use.
INSTALLER_ASSET_NAME = parakeet_model.INSTALLER_FILENAME

# crewcut: a copy of the ValidateSet of -SttProvider in
# scripts/release/public/install-wheelhouse.ps1. The installer is a
# PowerShell file, so this process cannot import the list. A test holds the
# two lists together. Remove the copy by having the installer keep the
# current engine when -SttProvider is left out.
VALID_STT_PROVIDERS = ("parakeet_tdt", "google_stt", "distil_medium_en")

# The config key that holds the speech engine of the current install.
STT_PROVIDER_KEY = "stt.last_provider"

_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-fA-F]{64})")

_RELEASES_SUFFIX = "/releases/latest"


@dataclass(frozen=True)
class InstallerAsset:
    """Where the installer script is and what the download must be.

    ``sha256`` is 64 lower-case hex digits, or None when the release answer
    carries no digest. ``size`` is the exact length in bytes that the release
    answer gives for the script. It is the proof that a download is complete.
    """

    url: str
    sha256: Optional[str]
    size: int


@dataclass(frozen=True)
class PreparedUpdate:
    """A downloaded installer script that is ready to run."""

    script_path: Path
    tag: str
    digest_checked: bool


# --- addresses -------------------------------------------------------------


def _repo_path() -> str:
    """``owner/name`` of the repository that RELEASES_URL names."""
    path = urllib.parse.urlsplit(RELEASES_URL).path
    if not (path.startswith("/repos/") and path.endswith(_RELEASES_SUFFIX)):
        raise UpdateCheckError("the releases address has an unexpected form")
    return path[len("/repos/"):-len(_RELEASES_SUFFIX)]


def _checked_tag(tag: object) -> str:
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise UpdateCheckError(f"release tag is not a version: {tag!r}")
    return tag


def release_url_for_tag(tag: object) -> str:
    """The address of the release of ``tag`` in the repository of the check."""
    tag = _checked_tag(tag)
    base = RELEASES_URL[: -len("latest")]
    return base + "tags/" + urllib.parse.quote(tag, safe="")


def _expected_asset_url(tag: str) -> str:
    return (
        f"https://github.com/{_repo_path()}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/{INSTALLER_ASSET_NAME}"
    )


# --- the request -----------------------------------------------------------


def _get(get: Callable[[str, str], bytes], url: str, running_version: str) -> bytes:
    """Call the fetch; give back bytes within the limit or raise the one error.

    A fetch passed in by a caller can raise any exception. This turns each
    ordinary exception into ``UpdateCheckError``. It does not catch
    ``KeyboardInterrupt`` or ``SystemExit``.
    """
    try:
        body = get(url, running_version)
    except UpdateCheckError:
        raise
    except Exception as exc:
        raise UpdateCheckError(f"request failed: {exc}") from exc
    if not isinstance(body, (bytes, bytearray)):
        raise UpdateCheckError("answer is not bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("answer is larger than the limit")
    return bytes(body)


# --- the release answer ----------------------------------------------------


def _read_digest(value: object) -> Optional[str]:
    """The hex digest in a ``sha256:<hex>`` field; None when there is no field.

    A field that is present but not in that form raises, because an
    unreadable digest must not pass as "no digest".
    """
    if value is None:
        return None
    if isinstance(value, str):
        match = _DIGEST_PATTERN.fullmatch(value)
        if match:
            return match.group(1).lower()
    raise UpdateCheckError(f"digest is not in the form sha256:<hex>: {value!r}")


def _read_size(value: object) -> int:
    """The size in the ``size`` field; raises when it is not a usable size."""
    # bool is a subclass of int, so the type test excludes it by name.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise UpdateCheckError(f"asset size is not a positive integer: {value!r}")
    if value > MAX_RESPONSE_BYTES:
        raise UpdateCheckError(
            f"asset size {value} is larger than the download limit {MAX_RESPONSE_BYTES}"
        )
    return value


def find_installer_asset(release: object, tag: str) -> InstallerAsset:
    """Find the installer script asset in a release answer.

    The download address must have the exact form of the address of the
    script in the release of ``tag`` in the repository of the check. This
    function refuses any other address. It does not check where the request
    goes after that: ``urlopen`` follows redirects, and GitHub asset addresses
    need that.

    The asset must carry ``size``, a positive integer that is not over
    ``MAX_RESPONSE_BYTES``. Without ``size``, nothing proves that a download
    is complete, so the asset is refused.
    """
    if not isinstance(release, dict):
        raise UpdateCheckError("answer is not a release")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateCheckError("answer has no asset list")
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == INSTALLER_ASSET_NAME:
            break
    else:
        raise UpdateCheckError(f"release has no {INSTALLER_ASSET_NAME} asset")
    url = asset.get("browser_download_url")
    if url != _expected_asset_url(_checked_tag(tag)):
        raise UpdateCheckError(f"installer address is not the release address: {url!r}")
    return InstallerAsset(
        url=url,
        sha256=_read_digest(asset.get("digest")),
        size=_read_size(asset.get("size")),
    )


def fetch_installer_asset(
    tag: str,
    running_version: str,
    *,
    get: Callable[[str, str], bytes] = http_get,
) -> InstallerAsset:
    """Ask GitHub for the release of ``tag`` and return its installer asset."""
    body = _get(get, release_url_for_tag(tag), running_version)
    try:
        release = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise UpdateCheckError(f"answer is not readable: {exc}") from exc
    return find_installer_asset(release, tag)


# --- the download ----------------------------------------------------------


def download_installer(
    asset: InstallerAsset,
    running_version: str,
    *,
    get: Callable[[str, str], bytes] = http_get,
    parent: Optional[Path] = None,
) -> Path:
    """Save the script in a new temporary folder and check its digest.

    Returns the path of the file. The folder is new for each call and is
    named by ``tempfile.mkdtemp`` in ``parent`` (the system temporary folder
    when ``parent`` is None). The file keeps the name of the installer.
    The length of the bytes must equal ``asset.size``. When it does not, or
    when the asset has a digest and the bytes do not match it,
    ``UpdateCheckError`` is raised. Both checks run before the folder is
    made, so a refused download leaves no folder. When the asset has no
    digest, the file is kept and one line in the log says that no digest was
    checked. The caller owns the file after a success.
    """
    body = _get(get, asset.url, running_version)
    if not body:
        raise UpdateCheckError("the installer download is empty")
    if len(body) != asset.size:
        raise UpdateCheckError(
            f"the installer download is {len(body)} bytes; "
            f"the release answer gives size {asset.size}"
        )
    if asset.sha256 is not None:
        actual = hashlib.sha256(body).hexdigest()
        if actual != asset.sha256:
            raise UpdateCheckError(
                "the installer does not match the digest in the release answer"
            )
    folder: Optional[str] = None
    try:
        folder = tempfile.mkdtemp(prefix="wheelhouse-update-", dir=parent)
        path = Path(folder) / INSTALLER_ASSET_NAME
        path.write_bytes(body)
    except OSError as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise UpdateCheckError(f"could not save the installer: {exc}") from exc
    if asset.sha256 is None:
        logger.info("update_install: the release answer has no digest, so none was checked")
    return path


def prepare_update(
    tag: str,
    running_version: str,
    *,
    get: Callable[[str, str], bytes] = http_get,
    parent: Optional[Path] = None,
) -> PreparedUpdate:
    """Fetch the release of ``tag``, download its installer, check the digest."""
    asset = fetch_installer_asset(tag, running_version, get=get)
    path = download_installer(asset, running_version, get=get, parent=parent)
    return PreparedUpdate(script_path=path, tag=tag, digest_checked=asset.sha256 is not None)


# --- the installer parameters ----------------------------------------------


def build_installer_parameters(
    config_service: Any,
    *,
    auto_start: Optional[bool] = None,
    running_engine: Optional[str] = None,
) -> list[str]:
    """The installer parameters that carry the answers of the current install.

    The list has the same form as the call in
    ``stt.parakeet_model.build_launch_command``: ``-SttProvider`` with the
    engine in ``stt.last_provider``, ``-StartNow yes``, and ``-AutoStart``
    with yes when the start-at-login shortcut exists. ``auto_start`` None
    reads whether the shortcut exists now. The engine is the first valid
    one of these: ``running_engine`` (the engine that started this run, which
    the caller reads from the runtime record), then ``stt.last_provider``.
    A name counts as valid when it is a string in VALID_STT_PROVIDERS. The
    runtime record comes first because a config whose ``stt`` key holds a
    plain string, such as ``stt = "whisper"``, is loaded and run by
    Wheelhouse but reads as no ``stt.last_provider`` at all. When no
    candidate is valid, ``-SttProvider`` is left out and one line goes to
    the log (a warning that names each rejected name, or an info line when
    both sources are absent). The installer then has no engine name and asks
    the user to choose one at its prompt. A hand-edited config can reach
    this case. When a valid engine is found after a rejected name, one
    warning names the rejected name. ``-AiMode`` is left out, because the
    installer default ("keep") keeps the AI settings. Nothing is started.
    """
    params: list[str] = []
    rejected: list[Any] = []
    engine: Optional[str] = None
    # The config is read only when the runtime record gave no valid engine.
    for read in (lambda: running_engine, lambda: config_service.get(STT_PROVIDER_KEY, None)):
        candidate = read()
        if isinstance(candidate, str) and candidate in VALID_STT_PROVIDERS:
            engine = candidate
            break
        if candidate is not None:
            rejected.append(candidate)
    names = ", ".join(repr(name) for name in rejected)
    if engine is not None:
        params += ["-SttProvider", engine]
        if rejected:
            logger.warning("update_install: engine %s not usable, passed %r", names, engine)
    elif rejected:
        logger.warning("update_install: engine %s not passed to the installer", names)
    else:
        logger.info("update_install: no engine name known, none passed to the installer")
    if auto_start is None:
        auto_start = parakeet_model.startup_shortcut_exists()
    params += ["-StartNow", "yes", "-AutoStart", "yes" if auto_start else "no"]
    return params
