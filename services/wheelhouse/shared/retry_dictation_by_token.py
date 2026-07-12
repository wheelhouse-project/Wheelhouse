"""retry_dictation_by_token IPC contract (wh-wt82).

Defines the Logic -> Input request that fires when the user clicks
"Try it anyway" on a text-target rejection toast (wh-9weum, Phase 4).
The schema is the contract for the override path the wh-x4mv.2
round-2 review settled on, with a two-cache split:

  * Input owns the ``correlation_token -> original_text`` cache.
  * Logic owns the ``correlation_token -> tuple`` cache (process,
    class, control_type) plus the verified-retry counter.

Privacy contract: the request payload does NOT include dictation
text. Only the correlation_token threads the round trip across
processes. Text crosses processes only as the clipboard write that
ClipboardOnlyStrategy performs at retry time, and only inside the
Input process's own work.

Transport: WheelHouseApp.send_request("retry_dictation_by_token",
params={...}) puts an action dict on the Logic -> Input shared
memory channel. The Input process resolves the request and returns
a response dict that ``RetryDictationByTokenResponse.from_dict``
parses.

Response shape:
  * ``status="success"`` -- Input found the token, ran
    ClipboardOnlyStrategy, and reports the strategy's
    ``retry_outcome`` ("verified" or "unverified", per
    ``ui.strategies.base.InsertionResult``).
  * ``status="token_expired"`` -- the token was known but its TTL
    has elapsed; this is NOT an IPC error. Logic forwards it to the
    GUI as a one-line follow-up toast.
  * ``status="unknown_token"`` -- the token was never seen. Treated
    identically to ``token_expired`` by the GUI; kept distinct so
    log surfaces can tell a stale-cache miss from an out-of-cache
    miss.

The two non-success statuses do not carry a ``retry_outcome``: no
strategy ran. The schema enforces that pairing so a malformed
sender cannot smuggle a phantom retry_outcome past consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from services.wheelhouse.shared.correlation_token import (
    validate_correlation_token,
)


ACTION_NAME = "retry_dictation_by_token"

OVERRIDE_CLIPBOARD_ONLY = "clipboard_only"

ALLOWED_OVERRIDE_STRATEGIES: frozenset[str] = frozenset({OVERRIDE_CLIPBOARD_ONLY})

STATUS_SUCCESS = "success"
STATUS_TOKEN_EXPIRED = "token_expired"
STATUS_UNKNOWN_TOKEN = "unknown_token"

ALLOWED_STATUSES: frozenset[str] = frozenset(
    {STATUS_SUCCESS, STATUS_TOKEN_EXPIRED, STATUS_UNKNOWN_TOKEN}
)

# Mirror the values in services/wheelhouse/ui/strategies/base.py
# InsertionResult.retry_outcome that this response can carry. The
# success status of this contract maps straight from
# ClipboardOnlyStrategy's outcome; the schema deliberately does not
# accept "n/a" because no strategy ran means no success status.
RETRY_OUTCOME_VERIFIED = "verified"
RETRY_OUTCOME_UNVERIFIED = "unverified"

ALLOWED_RETRY_OUTCOMES: frozenset[str] = frozenset(
    {RETRY_OUTCOME_VERIFIED, RETRY_OUTCOME_UNVERIFIED}
)


class RetryDictationByTokenSchemaError(ValueError):
    """Raised on a malformed retry_dictation_by_token request or response.

    Logic and Input process consumers should catch this and degrade
    gracefully (log + drop, or log + return an unknown_token
    response), per wh-uf54 (IPC schema validation and graceful
    degradation for new events).
    """


@dataclass(frozen=True)
class RetryDictationByTokenRequest:
    """Logic -> Input request payload for the override retry path."""

    correlation_token: str
    override_strategy: str

    def __post_init__(self) -> None:
        # wh-9weum.1.3: validate uuid4 shape rather than just str.
        validate_correlation_token(
            self.correlation_token,
            field_name="correlation_token",
            error_class=RetryDictationByTokenSchemaError,
        )
        # wh-9weum.1.2: type-check before membership so a malformed
        # unhashable value (e.g. a list) raises the schema error
        # instead of TypeError, which safe_parse does not catch.
        if not isinstance(self.override_strategy, str):
            raise RetryDictationByTokenSchemaError(
                f"override_strategy must be a str, got "
                f"{type(self.override_strategy).__name__}"
            )
        if self.override_strategy not in ALLOWED_OVERRIDE_STRATEGIES:
            raise RetryDictationByTokenSchemaError(
                f"override_strategy {self.override_strategy!r} is not allowed; "
                f"must be one of {sorted(ALLOWED_OVERRIDE_STRATEGIES)!r}"
            )

    def to_action_payload(self) -> dict[str, Any]:
        """Serialize to the WheelHouseApp.send_request action shape."""

        return {
            "action": ACTION_NAME,
            "params": {
                "correlation_token": self.correlation_token,
                "override_strategy": self.override_strategy,
            },
        }

    @classmethod
    def from_action_payload(cls, payload: Any) -> "RetryDictationByTokenRequest":
        """Parse and validate a wire-format action payload."""

        if not isinstance(payload, Mapping):
            raise RetryDictationByTokenSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        if "action" not in payload:
            raise RetryDictationByTokenSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise RetryDictationByTokenSchemaError(
                f"action {payload['action']!r} does not match {ACTION_NAME!r}"
            )

        if "params" not in payload:
            raise RetryDictationByTokenSchemaError(
                "payload missing required key 'params'"
            )
        params = payload["params"]
        if not isinstance(params, Mapping):
            raise RetryDictationByTokenSchemaError(
                f"params must be a mapping, got {type(params).__name__}"
            )

        for required in ("correlation_token", "override_strategy"):
            if required not in params:
                raise RetryDictationByTokenSchemaError(
                    f"params missing required field {required!r}"
                )

        # The dataclass __post_init__ validates correlation_token
        # shape (uuid4) and override_strategy type + allowlist; we
        # let it raise.
        return cls(
            correlation_token=params["correlation_token"],
            override_strategy=params["override_strategy"],
        )


@dataclass(frozen=True)
class RetryDictationByTokenResponse:
    """Input -> Logic response for a retry_dictation_by_token request."""

    status: str
    retry_outcome: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        # wh-9weum.1.2: type-check before membership so a malformed
        # unhashable status (e.g. a list) raises the schema error
        # instead of TypeError, which safe_parse does not catch.
        if not isinstance(self.status, str):
            raise RetryDictationByTokenSchemaError(
                f"status must be a str, got {type(self.status).__name__}"
            )
        if self.status not in ALLOWED_STATUSES:
            raise RetryDictationByTokenSchemaError(
                f"status {self.status!r} is not allowed; "
                f"must be one of {sorted(ALLOWED_STATUSES)!r}"
            )
        if self.status == STATUS_SUCCESS:
            if self.retry_outcome is None:
                raise RetryDictationByTokenSchemaError(
                    "retry_outcome is required when status is 'success'"
                )
            # wh-9weum.1.2: same type-then-membership pattern.
            if not isinstance(self.retry_outcome, str):
                raise RetryDictationByTokenSchemaError(
                    f"retry_outcome must be a str, got "
                    f"{type(self.retry_outcome).__name__}"
                )
            if self.retry_outcome not in ALLOWED_RETRY_OUTCOMES:
                raise RetryDictationByTokenSchemaError(
                    f"retry_outcome {self.retry_outcome!r} is not allowed; "
                    f"must be one of {sorted(ALLOWED_RETRY_OUTCOMES)!r}"
                )
        else:
            if self.retry_outcome is not None:
                raise RetryDictationByTokenSchemaError(
                    f"retry_outcome must be None when status is {self.status!r}; "
                    f"got {self.retry_outcome!r}"
                )
        if not isinstance(self.reason, str):
            raise RetryDictationByTokenSchemaError(
                f"reason must be a str, got {type(self.reason).__name__}"
            )

    @classmethod
    def success(cls, retry_outcome: str) -> "RetryDictationByTokenResponse":
        """Build a success response carrying ClipboardOnlyStrategy's outcome."""

        return cls(status=STATUS_SUCCESS, retry_outcome=retry_outcome)

    @classmethod
    def token_expired(cls, reason: str = "") -> "RetryDictationByTokenResponse":
        """Build a token_expired response (token known but TTL elapsed)."""

        return cls(status=STATUS_TOKEN_EXPIRED, retry_outcome=None, reason=reason)

    @classmethod
    def unknown_token(cls, reason: str = "") -> "RetryDictationByTokenResponse":
        """Build an unknown_token response (token never cached)."""

        return cls(status=STATUS_UNKNOWN_TOKEN, retry_outcome=None, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format response dict."""

        return {
            "status": self.status,
            "retry_outcome": self.retry_outcome,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RetryDictationByTokenResponse":
        """Parse and validate a wire-format response dict."""

        if not isinstance(payload, Mapping):
            raise RetryDictationByTokenSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        if "status" not in payload:
            raise RetryDictationByTokenSchemaError(
                "payload missing required key 'status'"
            )
        retry_outcome = payload.get("retry_outcome", None)
        reason = payload.get("reason", "")
        return cls(
            status=payload["status"],
            retry_outcome=retry_outcome,
            reason=reason,
        )
