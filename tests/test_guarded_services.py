"""INV-1 (#134): the extracted guarded SQL and document-research services, tested directly."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pytest
from langgraph.checkpoint.memory import InMemorySaver

import src.orchestration.langgraph_orchestrator as orchestrator_module
from src.agents.rag_agent import RAGAgentError
from src.agents.sql_agent import SQLAgentError
from src.documents.retriever import RetrievalResult
from src.documents.vector_store import RetrievedChunk
from src.llm.base import LLMGenerationError, LLMResponse
from src.llm.fallback import FallbackLLMClient
from src.llm.observability import current_purpose
from src.orchestration.document_research import DocumentResearchService, SourceRef
from src.orchestration.guarded_sql import MAX_ERROR_CHARS, MAX_SQL_CORRECTIONS, GuardedSQLService
from src.orchestration.langgraph_orchestrator import (
    QuestionOrchestrator,
    build_checkpoint_serializer,
)
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError
from src.storage.sqlite_store import SQLiteStore, StoredTable
from tests.test_orchestrator_characterization import (
    HYBRID_QUESTION,
    VERIFIED_SQL,
    PolicyRetriever,
    ScriptedLLM,
)
from tests.test_utils import isolated_database_path


@dataclass
class RecordingLLM:
    text: str = "SELECT merchant FROM uploaded_data"
    provider: str = "fake"
    model: str = "fake-model"
    configured: bool = True
    fail: bool = False
    purposes: list[str | None] = field(default_factory=list)

    def generate(self, prompt: str) -> LLMResponse:
        self.purposes.append(current_purpose())
        if self.fail:
            raise LLMGenerationError("provider unavailable")
        return LLMResponse(text=self.text, model=self.model, provider=self.provider)


def _table(name: str = "services", **columns: list[object]) -> StoredTable:
    data = columns or {"merchant": ["A", "B", "A"], "amount": [10.0, 30.0, 20.0]}
    return SQLiteStore(isolated_database_path(name)).save_dataframe(pd.DataFrame(data))


def _service(table: StoredTable | None = None) -> GuardedSQLService:
    return GuardedSQLService(table or _table(), RecordingLLM())


# --- GuardedSQLService ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE uploaded_data",
        "SELECT 1; DELETE FROM uploaded_data",
        "PRAGMA table_info(uploaded_data)",
    ],
    ids=["write", "multi-statement", "pragma"],
)
def test_validate_applies_the_shared_read_only_guard(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        _service().validate(sql)


def test_edited_sql_passes_through_the_same_guard() -> None:
    service = _service()
    assert service.validate("SELECT merchant FROM uploaded_data") == (
        "SELECT merchant FROM uploaded_data"
    )
    with pytest.raises(UnsafeQueryError):
        service.validate("SELECT merchant FROM uploaded_data; DROP TABLE uploaded_data")


POLICY = {
    "predicates": [
        {
            "field": "amount",
            "operator": "gt",
            "value": 20,
            "unit": "currency",
            "source": 1,
            "null_behavior": "exclude",
        }
    ],
    "predicate_join": "all",
}


@pytest.mark.parametrize(
    ("sql", "status", "unjustified"),
    [
        ('SELECT merchant FROM uploaded_data WHERE "amount" > 20', "structurally_verified", 0),
        ('SELECT merchant FROM uploaded_data WHERE "amount" >= 20', "mismatch", 0),
        (
            'SELECT merchant FROM uploaded_data WHERE "amount" > 20 AND "merchant" = \'A\'',
            "mismatch",
            1,
        ),
    ],
    ids=["verified", "wrong-operator", "unjustified-filter"],
)
def test_verify_policy_enforces_document_predicates(
    sql: str, status: str, unjustified: int
) -> None:
    service = _service()
    verification = service.verify_policy(
        service.validate(sql), POLICY, question="Which transactions violate the policy?"
    )
    assert verification.status == status
    assert verification.unjustified_predicate_count == unjustified


@pytest.mark.parametrize(
    "sql",
    ["SELECT name FROM sqlite_master", "SELECT load_extension('x') FROM uploaded_data"],
    ids=["schema-table", "unapproved-function"],
)
def test_execute_refuses_tables_and_functions_outside_the_allowlist(sql: str) -> None:
    outcome = _service().execute(sql, corrections_used=0)
    assert outcome.status == "refused"
    assert isinstance(outcome.error, UnsafeQueryError)
    assert outcome.dataframe is None


def test_execute_reads_only_the_bound_database() -> None:
    own = _table("own", merchant=["A"], amount=[1.0])
    _table("other", merchant=["B", "C"], amount=[2.0, 3.0])
    outcome = _service(own).execute("SELECT merchant FROM uploaded_data", corrections_used=0)
    assert outcome.dataframe is not None
    assert outcome.dataframe["merchant"].tolist() == ["A"]


def test_execute_succeeds_with_a_bounded_result() -> None:
    outcome = _service().execute("SELECT merchant, amount FROM uploaded_data", corrections_used=0)
    assert outcome.status == "succeeded"
    assert outcome.dataframe is not None
    assert len(outcome.dataframe) == 3
    assert outcome.elapsed_seconds >= 0


def test_timeouts_are_refused_never_retryable() -> None:
    def time_out(*_args: object) -> pd.DataFrame:
        raise QueryTimeoutError("deadline")

    service = GuardedSQLService(_table(), RecordingLLM(), executor=time_out)
    outcome = service.execute("SELECT 1", corrections_used=0)
    assert outcome.status == "refused"
    assert isinstance(outcome.error, QueryTimeoutError)


def test_database_errors_allow_exactly_one_bounded_correction() -> None:
    def fail(*_args: object) -> pd.DataFrame:
        raise RuntimeError("no such column: " + "x" * 2_000)

    service = GuardedSQLService(_table(), RecordingLLM(), executor=fail)
    first = service.execute("SELECT x FROM uploaded_data", corrections_used=0)
    assert first.status == "retryable"
    assert len(first.error_text) == MAX_ERROR_CHARS
    second = service.execute("SELECT x FROM uploaded_data", corrections_used=MAX_SQL_CORRECTIONS)
    assert second.status == "exhausted"
    assert isinstance(second.error, RuntimeError)
    with pytest.raises(ValueError):
        service.execute("SELECT 1", corrections_used=-1)


def test_execute_reports_but_never_retries() -> None:
    calls = 0

    def fail(*_args: object) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    service = GuardedSQLService(_table(), RecordingLLM(), executor=fail)
    service.execute("SELECT 1", corrections_used=0)
    assert calls == 1


def test_generation_keeps_purpose_labels_and_extracts_sql() -> None:
    llm = RecordingLLM(text="```sql\nSELECT merchant FROM uploaded_data\n```")
    service = GuardedSQLService(_table(), llm)
    prompt = service.prompt("Show merchants")
    sql, raw = service.generate(prompt)
    correction = service.correction_prompt(prompt, sql, "no such column")
    service.generate(correction, "sql_correction")
    assert sql == "SELECT merchant FROM uploaded_data"
    assert "```sql" in raw
    assert "The first query failed" in correction
    assert llm.purposes == ["sql_generation", "sql_correction"]


def test_generation_uses_the_provider_fallback_chain_unchanged() -> None:
    primary = RecordingLLM(fail=True, provider="primary")
    secondary = RecordingLLM(text="SELECT merchant FROM uploaded_data", provider="secondary")
    chain = FallbackLLMClient([primary, secondary])
    # The chain exposes provider/model as read-only properties, which the structural LLMClient
    # protocol types as settable; the runtime contract (generate) is identical.
    service = GuardedSQLService(_table(), chain)  # type: ignore[arg-type]
    sql, _raw = service.generate(service.prompt("Show merchants"))
    assert sql == "SELECT merchant FROM uploaded_data"
    assert primary.purposes == secondary.purposes == ["sql_generation"]


# --- DocumentResearchService ------------------------------------------------------------------


class Retriever:
    def __init__(self, chunks: list[RetrievedChunk], rejected: int = 0) -> None:
        self.chunks = chunks
        self.rejected = rejected
        self.top_k: list[int] = []

    def retrieve(self, question: str, top_k: int = 4) -> RetrievalResult:
        self.top_k.append(top_k)
        return RetrievalResult(
            question=question,
            chunks=self.chunks,
            candidates_considered=len(self.chunks) + self.rejected,
            candidates_rejected_by_distance=self.rejected,
        )


POLICY_CHUNK = RetrievedChunk(
    text="Transactions over 20 dollars require review. Contact jane.doe@example.com.",
    metadata={"filename": "policy.pdf", "page_number": 2, "chunk_index": 5},
    distance=0.1,
    relevance_score=0.87,
)


def test_research_returns_verified_citations_and_content_free_sources() -> None:
    retriever = Retriever([POLICY_CHUNK])
    llm = RecordingLLM(text="Transactions over 20 dollars require review. Source 1")
    outcome = DocumentResearchService(retriever, llm).research("What needs review?")
    assert outcome.answer.cited_source_numbers == [1]
    assert "VERIFIED_REFERENCE" in outcome.answer.evidence_statuses
    assert not outcome.answer.invalid_citations
    assert outcome.sources == (
        SourceRef(
            number=1, filename="policy.pdf", page_number=2, chunk_index=5, relevance_score=0.87
        ),
    )
    assert retriever.top_k == [4]
    assert llm.purposes == ["rag_answer"]


def test_research_flags_invalid_citations_instead_of_trusting_them() -> None:
    llm = RecordingLLM(text="Everything needs review. Source 3")
    outcome = DocumentResearchService(Retriever([POLICY_CHUNK]), llm).research("What?")
    assert outcome.answer.invalid_citations == [3]
    assert "INVALID_REFERENCE" in outcome.answer.evidence_statuses


def test_research_redacts_pii_in_the_answer() -> None:
    llm = RecordingLLM(text="Ask jane.doe@example.com. Source 1")
    outcome = DocumentResearchService(Retriever([POLICY_CHUNK]), llm).research("Who?")
    assert "jane.doe@example.com" not in outcome.answer.answer


def test_research_without_relevant_passages_refuses_with_counts() -> None:
    llm = RecordingLLM()
    service = DocumentResearchService(Retriever([], rejected=3), llm)
    with pytest.raises(RAGAgentError) as refused:
        service.research("What needs review?")
    assert refused.value.candidates_rejected_by_distance == 3
    assert llm.purposes == []  # no model call without evidence


def test_research_rejects_a_non_positive_top_k() -> None:
    with pytest.raises(ValueError):
        DocumentResearchService(Retriever([]), RecordingLLM(), top_k=0)


# --- orchestrator compatibility exception -----------------------------------------------------


def test_resuming_an_approval_without_a_table_fails_safely_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intentional INV-1 change: this used to reach ``assert self.stored_table is not None`` in
    ``_execute_sql_node`` (an unhandled AssertionError, or AttributeError under ``-O``). It now
    raises the handled ``SQLAgentError`` before any SQL runs. The API cannot reach this state;
    the test pins the safer library behaviour.
    """
    saver = InMemorySaver(serde=build_checkpoint_serializer())
    llm = ScriptedLLM(sql=VERIFIED_SQL)
    pending = QuestionOrchestrator(
        llm_client=llm,
        stored_table=_table("approval-missing-table"),
        document_retriever=PolicyRetriever(),
        checkpointer=saver,
    ).answer(HYBRID_QUESTION, require_sql_approval=True, thread_id="missing-table")
    assert pending.status == "pending_approval"

    executed: list[str] = []
    monkeypatch.setattr(
        orchestrator_module, "execute_sql", lambda _table, sql: executed.append(sql)
    )
    without_table = QuestionOrchestrator(
        llm_client=llm,
        stored_table=None,
        document_retriever=PolicyRetriever(),
        checkpointer=saver,
    )
    with pytest.raises(SQLAgentError, match="No uploaded table is available"):
        without_table.resume_approval("missing-table", decision="approve")
    assert executed == []
