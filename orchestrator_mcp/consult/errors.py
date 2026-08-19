"""Failure codes for the consultation path.

A consultation talks to a locally installed agent runtime, so "not installed" and
"logged out" are the common cases -- neither of which has an equivalent on an HTTP
endpoint, which is why this vocabulary is its own and not a provider's.
"""

from __future__ import annotations

from enum import Enum


class ConsultErrorCode(str, Enum):
    """Closed set, so callers branch on a value instead of matching substrings."""

    AGENT_NOT_INSTALLED = "agent_not_installed"
    CONNECTION_REQUIRED = "connection_required"
    CONFIGURED_MODEL_UNAVAILABLE = "configured_model_unavailable"
    AGENT_UNAVAILABLE = "agent_unavailable"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_BUSY = "session_busy"
    SESSION_TARGET_MISMATCH = "session_target_mismatch"
    # A workflow owns this consultation, and the public tool may not resume it. Its
    # own code rather than a target mismatch, because the refusal is about who is
    # asking and not about which agent is bound: the workflow path relaxes the
    # host-runtime exclusion to an execution identity, and a public resume must not
    # inherit that.
    WORKFLOW_OWNED_SESSION = "workflow_owned_session"
    PROTOCOL_VALIDATION_FAILED = "protocol_validation_failed"
    WEB_SEARCH_UNAVAILABLE = "web_search_unavailable"
    TRANSPORT_ERROR = "transport_error"
    # No attempt was made. A reviewer reserved before its batch and never launched
    # has this rather than `TRANSPORT_ERROR`, which claims a transport failed.
    NOT_STARTED = "not_started"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    NO_AGENT_AVAILABLE = "no_agent_available"
    # A configured spend ceiling was already crossed when this request was made.
    # Its own code because the caller's move is neither a retry nor a reroute: it is
    # to raise the ceiling or accept that the work stops here.
    SPEND_LIMIT_REACHED = "spend_limit_reached"
