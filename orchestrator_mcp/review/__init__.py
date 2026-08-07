"""Multi-reviewer layer over the consultation path.

A review is N consultations sharing one approved payload, grouped under a
`review_id`. Nothing here talks to a CLI directly -- it composes `ConsultService`,
which already knows about leases, preflight, model-substitution refusal and the
one-envelope-for-every-outcome rule.
"""

from .contract import (
    CombinedFinding,
    DeleteApproval,
    DeletionResult,
    Finding,
    MaterialItem,
    RawReviewMaterial,
    ReviewerResult,
    ReviewerSnapshot,
    ReviewListing,
    ReviewMode,
    ReviewPlan,
    ReviewResponse,
    ReviewSummary,
    SecretHit,
)
from .service import ReviewService
from .store import ReviewStore

__all__ = [
    "CombinedFinding",
    "DeleteApproval",
    "DeletionResult",
    "Finding",
    "MaterialItem",
    "RawReviewMaterial",
    "ReviewListing",
    "ReviewMode",
    "ReviewPlan",
    "ReviewResponse",
    "ReviewService",
    "ReviewStore",
    "ReviewSummary",
    "ReviewerResult",
    "ReviewerSnapshot",
    "SecretHit",
]
