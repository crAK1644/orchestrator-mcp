"""Failure codes for the consultation path.

Separate from `ErrorCode` in the LiteLLM path on purpose: the failures are
different in kind. A consultation talks to a locally installed agent runtime, so
"not installed" and "logged out" are the common cases, and neither has an
equivalent on an HTTP endpoint.
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
    PROTOCOL_VALIDATION_FAILED = "protocol_validation_failed"
    WEB_SEARCH_UNAVAILABLE = "web_search_unavailable"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    NO_AGENT_AVAILABLE = "no_agent_available"
