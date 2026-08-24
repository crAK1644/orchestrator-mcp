"""Regression coverage for delegated workflow synthesis."""

import json

from .test_workflow_service import (  # noqa: F401 -- fixtures
    FINDINGS,
    _to_synthesis,
    build,
    finding_ids,
    repo,
    summary_with,
)


async def test_delegated_synthesis_receives_review_findings_without_reviewer_prose(
    build, repo
):
    service = await build(bindings={"synthesize": {"agent": "flash"}})
    marker = "REVIEWER PROSE MUST NOT BE RE-SENT"
    workflow_id = await _to_synthesis(service, repo, findings=f"{marker}\n\n{FINDINGS}")
    ids = await finding_ids(service, workflow_id)

    service.adapters["flash"].answers = [json.dumps(summary_with("fixed", ids))]
    planned = await service.plan_step(workflow_id, "synthesize")

    assert planned.error is None, planned.error
    assert planned.preview is not None
    assert "review_outcome" in planned.preview.inputs

    step_id, token = planned.preview.step_id, planned.preview.confirm_token
    response = await service.run_step(workflow_id, step_id, token)

    assert response.error is None, response.error
    assert response.status == "completed"
    sent = service.adapters["flash"].prompts[-1]
    assert "the scanner drops the last token" in sent
    assert ids[0] in sent
    assert marker not in sent
