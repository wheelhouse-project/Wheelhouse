"""OpenAIProvider -- communicates with any OpenAI-compatible API.

Supports OpenAI, Gemini (via compatibility endpoint), Groq, Together,
Mistral, and any service implementing the /v1/chat/completions spec.
The base_url parameter selects the endpoint.

Phase A of the thin-client redesign (design 2026-06-04, spec 5.1/5.1a):
chat() now returns a structured ChatResult instead of a bare string, the
provider grows a real HTTP is_available() probe and a list_models()
method, the chat request timeout is driven by a constructor parameter
(wired from ai.server.timeout_s in the service layer), and the
cloud-only reasoning_effort field is dropped entirely. The old code path
in AIService.start() is still active this phase -- nothing is removed yet.
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from utils.redact import redact_transcript

log = logging.getLogger(__name__)

# Reachability / list probes use a short, separate timeout so a black-holed
# server cannot stall a probe for the full chat timeout (design section 4).
_PROBE_TIMEOUT_S = 5

# Default chat request timeout when the service layer does not pass one.
_DEFAULT_CHAT_TIMEOUT_S = 60


class ChatStatus(Enum):
    """Outcome of a chat() call.

    OK              -- success with non-empty text.
    EMPTY           -- success but the model returned no content.
    CANCELLED       -- caller set cancel_requested; result discarded intentionally.
    MODEL_NOT_FOUND -- the server reports the chosen model is missing
                       (HTTP 404, or a 400/404 whose body names the model).
    HTTP_ERROR      -- any other non-2xx HTTP response.
    TRANSPORT_ERROR -- connection error or timeout (server unreachable).
    """

    OK = "ok"
    EMPTY = "empty"
    CANCELLED = "cancelled"
    MODEL_NOT_FOUND = "model_not_found"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class ChatResult:
    """Structured result of a chat() call.

    Replaces the old bare-string return so callers can tell a dead server
    from a renamed model from an empty answer, and can read the truncation
    signal (finish_reason == "length") that the old "" return discarded.

    __bool__ deliberately raises TypeError (spec finding 2.5): a dataclass
    instance is truthy by default, so an overlooked ``if result:`` would
    silently always take the truthy branch. Raising turns any missed
    truthiness site into a loud failure that names the fix (.ok / .status).
    """

    status: ChatStatus
    text: str = ""
    finish_reason: Optional[str] = None
    status_code: Optional[int] = None

    @property
    def ok(self) -> bool:
        """True only on a successful response with text."""
        return self.status is ChatStatus.OK

    @property
    def outcome(self) -> str:
        """The status enum's string value (ok/empty/model_not_found/...)."""
        return self.status.value

    @property
    def truncated(self) -> bool:
        """True when the response was cut off by the token budget."""
        return self.finish_reason == "length"

    @property
    def exhausted_reasoning(self) -> bool:
        """True when the server answered 200 but the WHOLE token budget was
        spent without emitting any content -- the signature of a reasoning
        model (qwen3*, deepseek-r1*, ...) whose hidden thinking consumed
        max_tokens before any answer text (wh-ai-reasoning-model-empty).
        Plain empties (no length signal) stay False."""
        return self.status is ChatStatus.EMPTY and self.finish_reason == "length"

    def __bool__(self) -> bool:  # noqa: D401 - intentional guard
        raise TypeError(
            "ChatResult is not truthy; check result.ok (or result.status) "
            "instead of using the ChatResult directly in a boolean context."
        )


def _model_not_found_in_body(data) -> bool:
    """Heuristic: does an error body name a missing/renamed model?

    Uses exact multi-word phrases only -- the former loose co-occurrence
    ("model" in text and "not found" in text) was dropped because it
    matched unrelated 4xx messages such as
    ``"Required parameter 'model' not found in request"`` and classified
    them as MODEL_NOT_FOUND instead of HTTP_ERROR.

    Ollama's OpenAI-compat endpoint uses a split phrase that embeds the
    model name between "model" and "not found", e.g.
    ``model "qwen3.5:9b" not found, try pulling it first``.  The
    substring "model not found" is therefore absent; catch it via the
    suffix "not found, try pulling" which is Ollama-specific.  wh-ay6h.6.6
    """
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            text = " ".join(
                str(error.get(k, "")) for k in ("message", "code", "type")
            )
        else:
            text = str(error)
    else:
        text = str(data)
    text = text.lower()
    return (
        "model not found" in text
        or "does not exist" in text
        or "no such model" in text
        or "not found, try pulling" in text
    )


class OpenAIProvider:
    """AI provider for OpenAI-compatible cloud and local APIs."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        think: bool = False,
        timeout_s: int = _DEFAULT_CHAT_TIMEOUT_S,
        is_cloud: bool = False,
    ):
        self._api_key = api_key or os.environ.get("WHEELHOUSE_AI_API_KEY", "")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._think = think
        self._timeout_s = timeout_s
        # True when [ai.server].kind is "cloud". A cloud provider legitimately
        # uses a non-/v1 path (Google's Gemini root is /v1beta/openai/), so the
        # non-/v1 warning -- which only matters for the local Ollama /api/tags
        # fallback -- is suppressed for it (finding 1.4).
        self._is_cloud = is_cloud
        self._session: Optional[aiohttp.ClientSession] = None
        # One-time warning latches (each fires at most once per instance).
        self._warned_http_non_local = False
        self._warned_non_v1 = False

        # URL convention guard: a doubled /v1 means the config program (or a
        # caller) appended /v1 to a base that already had it. This always
        # produces a 404 on every request, so fail loudly at construction.
        if self._base_url.endswith("/v1/v1"):
            raise ValueError(
                f"base_url ends in '/v1/v1' (doubled /v1): {base_url!r}. "
                "The base_url is the OpenAI v1 root and must contain /v1 "
                "exactly once."
            )

        # Construction-time one-time warning: an http (not https) endpoint
        # that is not localhost sends the API key in the clear (spec s13).
        if self._base_url.startswith("http://") and not self._is_local_endpoint():
            self._warn_http_non_local()

    def _get_session(self) -> aiohttp.ClientSession:
        """Lazy-create and return the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _is_local_endpoint(self) -> bool:
        """Check if base_url points to a local server.

        Parses the URL and compares the hostname exactly against known local
        addresses. Substring matching (e.g. 'localhost' in base_url) is
        intentionally avoided: a hostname like 'notlocalhost.example' or
        '127.0.0.1.example' would otherwise bypass the http warning
        (wh-ay6h.3.2).
        """
        try:
            hostname = urlparse(self._base_url).hostname or ""
        except Exception:
            hostname = ""
        return hostname in ("localhost", "127.0.0.1", "0.0.0.0")

    def _warn_http_non_local(self) -> None:
        """Emit the http-non-localhost warning at most once."""
        if not self._warned_http_non_local:
            self._warned_http_non_local = True
            log.warning(
                "AI base_url %s uses plain http and is not localhost; the API "
                "key and prompts are sent unencrypted. Use https for remote "
                "endpoints.",
                self._base_url,
            )

    def _models_endpoints(self) -> tuple[str, Optional[str]]:
        """Return (primary, fallback) URLs for the model-list / reachability
        probe.

        The primary is always <base_url>/models. The Ollama /api/tags
        fallback is returned ONLY when base_url actually ended in /v1 -- so
        the stripped host root corresponds to a base the chat path can also
        reach (design section 3, finding 1.5). When base_url does not end in
        /v1 there is no fallback (None), and a one-time non-/v1 warning fires.
        """
        primary = f"{self._base_url}/models"
        if self._base_url.endswith("/v1"):
            host_root = self._base_url[: -len("/v1")]
            return primary, f"{host_root}/api/tags"
        # base_url is non-/v1: no /api/tags fallback, because a successful tags
        # probe on an address whose chat path 404s would populate the model list
        # while every chat silently fails. Warn once for LOCAL endpoints only:
        # the /v1 convention and the Ollama fallback are local-server concepts,
        # and a cloud provider legitimately uses a non-/v1 path (Gemini's root is
        # /v1beta/openai/), so the warning would be misleading noise on every
        # cloud-AI startup (finding 1.4).
        if not self._is_cloud and not self._warned_non_v1:
            self._warned_non_v1 = True
            log.warning(
                "AI base_url %s does not end in '/v1'; it is expected to be "
                "the OpenAI v1 root. The Ollama /api/tags fallback is "
                "disabled for this address.",
                self._base_url,
            )
        return primary, None

    @staticmethod
    def _parse_models_payload(data) -> list[str]:
        """Extract model ids/names from a /models or /api/tags payload."""
        ids: list[str] = []
        if isinstance(data, dict):
            # OpenAI /models -> {"data": [{"id": ...}, ...]}
            entries = data.get("data")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        ident = entry.get("id") or entry.get("name")
                        if ident:
                            ids.append(str(ident))
                    elif isinstance(entry, str):
                        ids.append(entry)
            # Ollama /api/tags -> {"models": [{"name": ...}, ...]}
            tags = data.get("models")
            if isinstance(tags, list):
                for entry in tags:
                    if isinstance(entry, dict):
                        ident = entry.get("name") or entry.get("model") or entry.get("id")
                        if ident:
                            ids.append(str(ident))
                    elif isinstance(entry, str):
                        ids.append(entry)
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    ident = entry.get("id") or entry.get("name")
                    if ident:
                        ids.append(str(ident))
                elif isinstance(entry, str):
                    ids.append(entry)
        return ids

    async def list_models(self) -> list[str]:
        """GET <base_url>/models, returning the model ids.

        For an Ollama address, fall back to GET <host-root>/api/tags ONLY
        when base_url actually ended in /v1 (the finding-1.5 guard). Returns
        an empty list on any failure -- never raises.
        """
        headers = self._probe_headers()
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S)
        primary, fallback = self._models_endpoints()

        try:
            session = self._get_session()
            async with session.get(primary, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        log.warning(
                            "list_models primary probe: 200 response has non-JSON body "
                            "(proxy error page?), status=%s -- trying fallback",
                            resp.status,
                        )
                    else:
                        return self._parse_models_payload(data)
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.debug("list_models primary probe failed: %s", e)
        except Exception as e:  # noqa: BLE001 - never raise out of list_models
            log.debug("list_models primary probe unexpected error: %s", e)

        if fallback is None:
            return []

        try:
            session = self._get_session()
            async with session.get(fallback, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        log.warning(
                            "list_models /api/tags fallback: 200 response has non-JSON body "
                            "(proxy error page?), status=%s",
                            resp.status,
                        )
                    else:
                        return self._parse_models_payload(data)
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.debug("list_models /api/tags fallback failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("list_models /api/tags fallback unexpected error: %s", e)
        return []

    def _probe_headers(self) -> dict:
        """Headers for GET probes (auth only when a key is present)."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def chat(self, messages: list[dict], max_tokens: int = 500) -> ChatResult:
        """POST /chat/completions with messages array.

        Returns a structured ChatResult. The chat request timeout is driven
        by the timeout_s constructor parameter (wired from ai.server.timeout_s
        in the service layer). Only standard OpenAI fields are sent -- the
        cloud-only reasoning_effort field is dropped entirely (it broke LAN
        endpoints and no targeted endpoint needs it).
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        try:
            session = self._get_session()
            timeout = aiohttp.ClientTimeout(total=self._timeout_s)
            async with session.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout,
            ) as resp:
                # Parse JSON best-effort: a non-JSON body (HTML 500, proxy
                # error page, plain-text 404) must not mask the real HTTP
                # status. Read status first; attempt JSON parse regardless of
                # content-type, but catch parse failures so a non-JSON non-2xx
                # response is still returned as HTTP_ERROR with the real status
                # code (wh-ay6h.3.1).
                if resp.status != 200:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if data is not None:
                        if isinstance(data, dict):
                            error_msg = data.get("error", {})
                            if isinstance(error_msg, dict):
                                error_msg = error_msg.get("message", "Unknown error")
                        else:
                            error_msg = str(data)[:200]
                    else:
                        try:
                            raw = await resp.text()
                        except Exception:
                            raw = ""
                        error_msg = raw[:200] if raw else "non-JSON response"
                    log.warning(
                        "OpenAI API error: status=%s, message=%s",
                        resp.status,
                        error_msg,
                    )
                    if resp.status in (400, 404) and _model_not_found_in_body(data):
                        # Only classify as MODEL_NOT_FOUND when BOTH the status
                        # is 400/404 AND the body confirms a missing model. A
                        # bare 404 (wrong route, nginx proxy error, doubled /v1
                        # path) must not be misreported as a renamed model; a
                        # 5xx with a model-phrase body (some proxy upstreams)
                        # stays HTTP_ERROR.
                        return ChatResult(
                            status=ChatStatus.MODEL_NOT_FOUND,
                            status_code=resp.status,
                        )
                    return ChatResult(
                        status=ChatStatus.HTTP_ERROR,
                        status_code=resp.status,
                    )
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    log.warning(
                        "OpenAI 200 response has non-JSON body (proxy error page?), status=%s",
                        resp.status,
                    )
                    return ChatResult(status=ChatStatus.EMPTY, status_code=resp.status)
                if not isinstance(data, dict):
                    # The body of a 200 fix-text response contains the
                    # corrected rendering of captured user text; redact it
                    # like every other transcript-class value (wh-797.17.5).
                    log.warning(
                        "Unexpected response type %s: %s",
                        type(data).__name__,
                        redact_transcript(str(data)[:200]),
                    )
                    return ChatResult(status=ChatStatus.EMPTY, status_code=resp.status)
                choices = data.get("choices", [])
                if not choices:
                    log.warning("OpenAI returned no choices. keys=%s", list(data.keys()))
                    return ChatResult(status=ChatStatus.EMPTY, status_code=resp.status)
                choice = choices[0]
                if not isinstance(choice, dict):
                    log.warning(
                        "Unexpected choice type %s: %s",
                        type(choice).__name__,
                        redact_transcript(str(choice)[:200]),
                    )
                    return ChatResult(status=ChatStatus.EMPTY, status_code=resp.status)
                finish_reason = choice.get("finish_reason")
                content = choice.get("message", {}).get("content", "") or ""
                if not content:
                    return ChatResult(
                        status=ChatStatus.EMPTY,
                        finish_reason=finish_reason,
                        status_code=resp.status,
                    )
                return ChatResult(
                    status=ChatStatus.OK,
                    text=content,
                    finish_reason=finish_reason,
                    status_code=resp.status,
                )
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            log.warning("OpenAI chat failed: %s", e)
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        except aiohttp.ClientError as e:
            log.warning("OpenAI chat transport error: %s", e)
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        except Exception as e:
            log.error("OpenAI chat unexpected error: %s", e)
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)

    async def is_available(self) -> bool:
        """Real reachability probe: GET <base_url>/models == 200.

        Uses the short probe timeout. Applies the same /v1-strip + /api/tags
        fallback as list_models (the fallback runs only when a trailing /v1
        was actually stripped). Returns True iff a probe returns HTTP 200.
        Never raises.
        """
        headers = self._probe_headers()
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S)
        primary, fallback = self._models_endpoints()

        try:
            session = self._get_session()
            async with session.get(primary, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.debug("is_available primary probe failed: %s", e)
        except Exception as e:  # noqa: BLE001 - never raise out of is_available
            log.debug("is_available primary probe unexpected error: %s", e)

        if fallback is None:
            return False

        try:
            session = self._get_session()
            async with session.get(fallback, headers=headers, timeout=timeout) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.debug("is_available /api/tags fallback failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("is_available /api/tags fallback unexpected error: %s", e)
        return False

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
