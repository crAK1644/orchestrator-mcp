"""The service that composes a consultation, with the CLI replaced by a stub adapter.

The adapters are tested against real subprocesses elsewhere. What is being proved
here is the composition: that a consultation is created once and resumed after
that, that a failure is an envelope and never an exception, that a failed turn is
still recorded, and above all that an unavailable agent stops the consultation
rather than quietly becoming a different one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator_mcp.consult.adapters.base import (
    AdapterError,
    AdapterResult,
    AgentStatus,
)
from orchestrator_mcp.consult.config import ConsultConfig
from orchestrator_mcp.consult.contract import ConsultationContent, SourceMode
from orchestrator_mcp.consult.errors import ConsultErrorCode
from orchestrator_mcp.consult.service import ConsultService
from orchestrator_mcp.contract import Usage

from .conftest import consult_block

ANSWER = ConsultationContent(
    answer="blue",
    assumptions=[],
    uncertainties=[],
    follow_up_questions=[],
    sources=[],
)


class StubAdapter:
    """Records what it was asked to do and answers with whatever it was given."""

    def __init__(
        self,
        runtime: str = "codex",
        status: AgentStatus | None = None,
        error=None,
        verified: bool = False,
        content: ConsultationContent = ANSWER,
    ) -> None:
        self.runtime = runtime
        self._status = status
        self._error = error
        self._verified = verified
        self._content = content
        self.calls: list[tuple[str, str | None, SourceMode, int]] = []
        self.prompts: list[str] = []
        self.preflights = 0

    def connect_command(self, agent):
        return f"{agent.command} login"

    async def preflight(self, agent):
        self.preflights += 1
        return self._status or AgentStatus(agent.agent_id, installed=True, authenticated=True)

    async def start(self, agent, prompt, source_mode, session_id=None):
        self.calls.append(("start", session_id, source_mode, prompt.turn))
        self.prompts.append(prompt.full_text)
        return self._answer(session_id or "native-1")

    async def resume(self, agent, native_session_id, prompt, source_mode):
        self.calls.append(("resume", native_session_id, source_mode, prompt.turn))
        self.prompts.append(prompt.full_text)
        return self._answer(native_session_id)

    def _answer(self, native: str) -> AdapterResult:
        if self._error:
            raise self._error
        return AdapterResult(
            content=self._content,
            native_session_id=native,
            model_used="gpt-5.6-sol",
            model_verified=self._verified,
            raw_output='{"answer": "blue"}',
            usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )


class StubService(ConsultService):
    def __init__(self, *args, adapter: StubAdapter, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stub = adapter

    def adapter(self, agent):
        return self.stub


@pytest.fixture
def service_factory(tmp_path, host_claude):
    async def build(adapter: StubAdapter | None = None, host: str = host_claude, **overrides):
        config = ConsultConfig(
            **consult_block(database_path=str(tmp_path / "consultations.sqlite3"), **overrides)
        )
        return await StubService(config, host, adapter=adapter or StubAdapter()).open()

    return build


# --- the happy path ---------------------------------------------------------


async def test_a_first_consultation_creates_a_session_and_answers(service_factory):
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="what colour is the sky")

    assert response.ok and response.error is None
    assert response.content.answer == "blue"
    assert response.consultation_id is not None
    # Host is claude, so the codex agent is the only eligible one.
    assert response.route.agent_id == "codex-sol"
    assert response.usage.total_tokens == 12


@pytest.mark.parametrize("verified", [True, False])
async def test_the_envelope_says_whether_the_model_was_checked(service_factory, verified):
    """`route.model` is the same string either way -- the configured name, or the one a
    runtime confirmed. Without this flag a caller cannot tell "Sol answered" from
    "we asked for Sol and nobody said", which is the same as having no check."""
    service = await service_factory(StubAdapter(verified=verified))
    response = await service.consult(capability="coding", prompt="what colour is the sky")

    assert response.route.model == "gpt-5.6-sol"
    assert response.route.model_verified is verified


async def test_the_returned_id_continues_the_same_conversation(service_factory):
    adapter = StubAdapter()
    service = await service_factory(adapter)
    first = await service.consult(capability="coding", prompt="q1")
    second = await service.consult(
        capability="coding", prompt="q2", consultation_id=first.consultation_id
    )

    assert second.consultation_id == first.consultation_id
    assert [c[0] for c in adapter.calls] == ["start", "resume"]
    # The turn number is what tells the target this is a continuation.
    assert [c[3] for c in adapter.calls] == [1, 2]


async def test_the_session_id_we_assign_is_the_consultation_id(service_factory):
    adapter = StubAdapter()
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q")
    assert adapter.calls[0][1] == str(response.consultation_id)


async def test_auto_resolves_to_document_when_context_is_supplied(service_factory):
    adapter = StubAdapter()
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q", context="material")

    assert response.source_mode_used is SourceMode.DOCUMENT
    assert adapter.calls[0][2] is SourceMode.DOCUMENT


async def test_auto_resolves_to_model_without_context(service_factory):
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="q")
    assert response.source_mode_used is SourceMode.MODEL


async def test_the_turn_is_recorded_in_full(service_factory):
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="q", context="material")

    record = await service.get_consultation(response.consultation_id)
    (turn,) = record.turns
    assert turn["prompt"] == "q"
    assert turn["source_mode"] == "document"
    assert '"answer": "blue"' in turn["answer"]
    assert record.native_session_bound
    assert record.routing[0]["selected_agent"] == "codex-sol"


async def test_plain_consult_sends_original_material_and_stores_only_a_scrubbed_copy(
    service_factory,
):
    secret = "sk-this-is-a-secret-value"
    content = ANSWER.model_copy(update={"answer": f"the answer echoed {secret}"})
    adapter = StubAdapter(content=content)
    service = await service_factory(adapter)

    response = await service.consult(
        capability="coding", prompt=f"inspect {secret}", context=f"source={secret}"
    )
    record = await service.get_consultation(response.consultation_id)

    assert secret in adapter.prompts[0]
    assert response.content.answer.endswith(secret)
    assert secret not in record.model_dump_json()
    assert "[redacted]" in record.model_dump_json()


async def test_the_stored_record_does_not_hand_back_the_native_session_id(service_factory):
    """It is the consulted CLI's handle on a live session, and nothing outside this
    server has a use for it."""
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="q")
    record = await service.get_consultation(response.consultation_id)
    assert "native-1" not in record.model_dump_json()


# --- failures are envelopes -------------------------------------------------


async def test_an_unknown_capability_is_a_request_error_with_no_session(service_factory):
    service = await service_factory()
    response = await service.consult(capability="astrology", prompt="q")

    assert response.ok is False
    assert response.error.code is ConsultErrorCode.INVALID_REQUEST
    assert response.consultation_id is None
    assert response.content is None


async def test_document_mode_without_context_is_a_protocol_failure(service_factory):
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="q", source_mode="document")
    assert response.error.code is ConsultErrorCode.PROTOCOL_VALIDATION_FAILED


async def test_an_unknown_consultation_id_is_session_not_found(service_factory):
    """Never silently a new conversation: the caller asked to continue one."""
    service = await service_factory()
    response = await service.consult(capability="coding", prompt="q", consultation_id=uuid4())
    assert response.error.code is ConsultErrorCode.SESSION_NOT_FOUND


async def test_a_session_cannot_be_pointed_at_another_agent(service_factory):
    service = await service_factory()
    first = await service.consult(capability="coding", prompt="q")
    response = await service.consult(
        capability="coding",
        prompt="q2",
        consultation_id=first.consultation_id,
        target_agent="claude-opus",
    )
    assert response.error.code is ConsultErrorCode.SESSION_TARGET_MISMATCH


async def test_a_turn_in_flight_blocks_the_next_one(service_factory):
    service = await service_factory()
    first = await service.consult(capability="coding", prompt="q")

    async with service.store.lease(first.consultation_id):
        response = await service.consult(
            capability="coding", prompt="q2", consultation_id=first.consultation_id
        )
    assert response.error.code is ConsultErrorCode.SESSION_BUSY


async def test_no_eligible_agent_is_reported_not_routed_around(service_factory):
    """Host is claude, and the only other agent scores nothing for this capability."""
    service = await service_factory(
        agents={"codex-sol": {"runtime": "codex", "command": "codex", "model": "m"}}
    )
    response = await service.consult(capability="coding", prompt="q")

    assert response.error.code is ConsultErrorCode.NO_AGENT_AVAILABLE
    assert response.consultation_id is None


async def test_an_adapter_failure_becomes_an_envelope_and_a_recorded_turn(service_factory):
    adapter = StubAdapter(error=AdapterError(ConsultErrorCode.TIMEOUT, "the CLI never answered"))
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q")

    assert response.ok is False and response.content is None
    assert response.error.code is ConsultErrorCode.TIMEOUT

    record = await service.get_consultation(response.consultation_id)
    assert record.turns[0]["error_code"] == "timeout"
    assert record.turns[0]["answer"] is None


async def test_a_protocol_violation_is_never_dressed_up_as_an_answer(service_factory):
    adapter = StubAdapter(
        error=AdapterError(ConsultErrorCode.PROTOCOL_VALIDATION_FAILED, "tried to run a command")
    )
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q")
    assert (response.ok, response.content) == (False, None)


# --- unavailable agents -----------------------------------------------------


async def test_an_uninstalled_agent_stops_the_consultation(service_factory):
    adapter = StubAdapter(
        status=AgentStatus("codex-sol", installed=False, authenticated=False, detail="not on PATH")
    )
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q")

    assert response.error.code is ConsultErrorCode.AGENT_NOT_INSTALLED
    assert response.error.agent_id == "codex-sol"
    assert adapter.calls == []  # never invoked


async def test_a_logged_out_agent_asks_the_user_to_connect_it(service_factory):
    adapter = StubAdapter(
        status=AgentStatus("codex-sol", installed=True, authenticated=False, detail="not logged in")
    )
    service = await service_factory(adapter)
    response = await service.consult(capability="coding", prompt="q")

    assert response.error.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert response.error.required_action.command == "codex login"
    assert response.error.required_action.retry_after_connection is True


async def test_an_unavailable_agent_is_never_swapped_for_another(service_factory):
    """Two claude agents, the higher-scoring one logged out. The answer is 'connect
    it', not a quiet answer from the other one."""
    service = await service_factory(
        StubAdapter(status=AgentStatus("a", installed=True, authenticated=False, detail="out")),
        host="codex",
        agents={
            "claude-primary": {"runtime": "claude", "command": "claude", "model": "opus",
                               "priority": 1, "scores": {"coding": 95}},
            "claude-backup": {"runtime": "claude", "command": "claude", "model": "sonnet",
                              "priority": 2, "scores": {"coding": 90}},
        },
    )
    response = await service.consult(capability="coding", prompt="q")

    assert response.error.code is ConsultErrorCode.CONNECTION_REQUIRED
    assert response.error.agent_id == "claude-primary"


# --- listing ----------------------------------------------------------------


async def test_listing_marks_the_host_runtime_and_does_not_probe_it(service_factory):
    service = await service_factory()
    listing = await service.list_agents()

    by_id = {a.agent_id: a for a in listing.agents}
    assert listing.host_runtime == "claude"
    assert by_id["claude-opus"].excluded_as_host is True
    # Not probed: a subprocess launched to answer a question already settled.
    assert by_id["claude-opus"].installed is None
    assert by_id["codex-sol"].installed is True
    assert by_id["codex-sol"].scores["coding"] == 90


async def test_listing_can_skip_the_status_checks(service_factory):
    service = await service_factory()
    listing = await service.list_agents(check=False)
    assert all(a.installed is None for a in listing.agents)


async def test_a_filtered_listing_refuses_unknown_agent_ids(service_factory):
    service = await service_factory()
    with pytest.raises(ValueError, match="unknown agent id"):
        await service.list_agents(check=False, agent_ids=["nobody"])


# --- the preflight cache ----------------------------------------------------


async def test_one_consultation_probes_the_cli_once(service_factory):
    """Turn five of a live conversation used to pay a subprocess to re-ask a question
    whose answer changes when a human runs a login command."""
    adapter = StubAdapter()
    service = await service_factory(adapter)

    first = await service.consult(capability="coding", prompt="q1")
    await service.consult(capability="coding", prompt="q2", consultation_id=first.consultation_id)
    await service.consult(capability="coding", prompt="q3", consultation_id=first.consultation_id)

    assert adapter.preflights == 1


async def test_a_failed_preflight_is_never_cached(service_factory):
    """The asymmetry the cache is built around.

    A failure hands the caller a `required_action` login command. The retry after
    the user runs it must reach the CLI, or the cache becomes the reason the agent
    stays unusable for the rest of its TTL."""
    adapter = StubAdapter(
        status=AgentStatus("codex-sol", installed=True, authenticated=False, detail="log in")
    )
    service = await service_factory(adapter)

    first = await service.consult(capability="coding", prompt="q1")
    assert first.error.code is ConsultErrorCode.CONNECTION_REQUIRED

    await service.consult(capability="coding", prompt="q2")
    assert adapter.preflights == 2


async def test_a_turn_that_disproves_the_cache_clears_it(service_factory):
    """A CLI can be logged out between the probe and the run."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    await service.consult(capability="coding", prompt="q1")
    assert adapter.preflights == 1

    adapter._error = AdapterError(ConsultErrorCode.CONNECTION_REQUIRED, "signed out")
    failed = await service.consult(capability="coding", prompt="q2")
    assert failed.error.code is ConsultErrorCode.CONNECTION_REQUIRED
    # Served from cache, so still one probe -- and then dropped, so the next call
    # asks the CLI rather than answering `ready` for the rest of the TTL.
    assert adapter.preflights == 1

    adapter._error = None
    await service.consult(capability="coding", prompt="q3")
    assert adapter.preflights == 2


async def test_a_zero_ttl_restores_a_probe_per_turn(service_factory):
    adapter = StubAdapter()
    service = await service_factory(adapter, preflight_ttl_s=0)

    first = await service.consult(capability="coding", prompt="q1")
    await service.consult(capability="coding", prompt="q2", consultation_id=first.consultation_id)

    assert adapter.preflights == 2


async def test_an_expired_entry_is_probed_again(service_factory, monkeypatch):
    adapter = StubAdapter()
    service = await service_factory(adapter, preflight_ttl_s=1)

    now = [1000.0]
    monkeypatch.setattr("orchestrator_mcp.consult.service.time.monotonic", lambda: now[0])
    await service.consult(capability="coding", prompt="q1")
    now[0] += 2.0
    await service.consult(capability="coding", prompt="q2")

    assert adapter.preflights == 2


async def test_listing_agents_always_probes(service_factory):
    """`list_agents(check=True)` *is* the probe.

    A dashboard refresh answered from memory would report a login state nobody
    checked, under a timestamp that says somebody did."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    await service.consult(capability="coding", prompt="q1")
    assert adapter.preflights == 1

    await service.list_agents(check=True)
    await service.list_agents(check=True)

    # One per listing, on top of the turn's own: claude-opus is the host and is
    # never probed.
    assert adapter.preflights == 3


async def test_a_cache_hit_records_no_status_check(service_factory):
    """`agent_status_checks` means "we probed", which is what the dashboard reads it
    as. A row for a probe that did not happen would make that column a guess."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    first = await service.consult(capability="coding", prompt="q1")
    await service.consult(capability="coding", prompt="q2", consultation_id=first.consultation_id)

    rows = await service.store._run(
        lambda: service.store._db.execute(
            "SELECT COUNT(*) FROM agent_status_checks WHERE agent_id = ?", ("codex-sol",)
        ).fetchone()
    )
    assert rows[0] == 1


async def test_editing_an_agent_invalidates_its_entry(service_factory):
    """Keyed on command and model, not the id alone: an agent repointed at another
    CLI in the dashboard must not be answered from the old one's probe."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    await service.consult(capability="coding", prompt="q1")

    service.config.agents["codex-sol"].model = "gpt-5.6-pro"
    await service.consult(capability="coding", prompt="q2")

    assert adapter.preflights == 2


async def test_a_probe_that_comes_back_not_ready_evicts_the_cached_ready(service_factory):
    """A real probe is newer evidence than whatever is cached.

    `list_agents(check=True)` takes the uncached path, so it can observe an agent
    logged out while a `ready` from before the logout is still inside its TTL. The
    turn after that must not be answered from the stale entry."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    await service.consult(capability="coding", prompt="q1")
    assert adapter.preflights == 1

    adapter._status = AgentStatus(
        "codex-sol", installed=True, authenticated=False, detail="log in"
    )
    await service.list_agents(check=True)

    answer = await service.consult(capability="coding", prompt="q2")
    assert answer.error.code is ConsultErrorCode.CONNECTION_REQUIRED
    # Three: the first turn, the listing, and the turn that had to re-probe rather
    # than read the entry the listing disproved.
    assert adapter.preflights == 3


async def test_repointing_an_agent_at_another_runtime_invalidates_its_entry(service_factory):
    """Runtime decides which adapter does the probing, so it decides the answer."""
    adapter = StubAdapter()
    service = await service_factory(adapter)
    await service.consult(capability="coding", prompt="q1")

    service.config.agents["codex-sol"].runtime = "opencode"
    await service.consult(capability="coding", prompt="q2")

    assert adapter.preflights == 2
