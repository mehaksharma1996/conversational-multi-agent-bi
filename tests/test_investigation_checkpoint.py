"""INV-3 (#136): investigation checkpoints are pickle-free, tamper-resistant, and fail closed."""

from __future__ import annotations

import contextlib
import json
import pickle
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import ormsgpack
import pandas as pd
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import InvalidUpdateError, ParentCommand
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from packages.investigation import RefusalCode
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLedger, BudgetRequest
from packages.investigation.gateway import GatewayRefusal, authorize_state
from packages.investigation.state import (
    REDUCERS,
    InvestigationState,
    StateInvalid,
    StateUpdateRejected,
    is_json_native,
    owned_update,
    restore_state,
)
from src.orchestration.investigation_checkpoint import (
    InvestigationNodeError,
    build_investigation_serializer,
    guarded_node,
    is_strict_investigation_saver,
    load_investigation_state,
    new_investigation_saver,
)
from src.orchestration.langgraph_orchestrator import build_checkpoint_serializer
from tests.investigation_fixtures import (
    SCOPE,
    FixedClock,
    LiveAuthority,
    evidence_record,
    investigation_authority,
    running_state,
    task_record,
)

CANARY = "CANARY-select-secret-from-customers"
CALLED: list[str] = []

Step = tuple[str, Any, Callable[[Mapping[str, Any]], Mapping[str, Any]]]


def record_call(value: str) -> str:
    """A constructor a tampered checkpoint might name; the strict serializer must never call it."""
    CALLED.append(value)
    return value


def _approved_run_steps() -> list[Step]:
    """Writers taking an investigation from intake to a started task with evidence."""
    target = running_state()
    record = evidence_record("trend")
    return [
        ("intake", "intake", lambda s: {"state_version": 1, "scope": target["scope"]}),
        ("planning", "lifecycle", lambda s: {"status": "planning"}),
        ("plan", "supervisor", lambda s: {"plan": target["plan"]}),
        ("propose", "lifecycle", lambda s: {"status": "awaiting_plan_approval"}),
        ("approve", "approval", lambda s: {"plan_approval": target["plan_approval"]}),
        ("queue", "lifecycle", lambda s: {"status": "queued"}),
        ("run", "lifecycle", lambda s: {"status": "running"}),
        ("register", "dispatcher", lambda s: {"tasks": target["tasks"]}),
        ("start", "dispatcher", lambda s: {"tasks": {"trend": task_record("trend", "running", 1)}}),
        ("collect", "dispatcher", lambda s: {"evidence": {record["id"]: record}}),
    ]


def _graph(
    steps: list[Step], *, guard: bool = True, ledger: BudgetLedger | None = None
) -> tuple[Any, Any]:
    """Chain ``steps``; dispatcher nodes are bound to ``ledger`` when one is given."""
    graph = StateGraph(InvestigationState)
    previous = START
    for name, owner, step in steps:
        bound = ledger if owner == "dispatcher" else None
        graph.add_node(name, guarded_node(owner, step, ledger=bound) if guard else step)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    saver = new_investigation_saver()
    return graph.compile(checkpointer=saver), saver


def _usage_step() -> tuple[Step, BudgetLedger]:
    """A dispatcher step recording one reservation, and the ledger it must be bound to."""
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    ledger.authorize(BudgetRequest(sql_executions=1))
    usage = ledger.usage().model_dump(mode="json")
    return ("usage", "dispatcher", lambda s: {"budget_usage": usage}), ledger


def _config(thread: str = "tenant_a:ws_1:inv_1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


def _stored(saver: Any) -> list[tuple[str, bytes]]:
    """Every serialized value the saver holds: channel blobs and pending writes."""
    blobs = list(saver.blobs.values())
    writes = [entry[2] for per_task in saver.writes.values() for entry in per_task.values()]
    return blobs + writes


def _latest_blob_key(saver: Any, channel: str) -> Any:
    keys = [key for key in saver.blobs if key[2] == channel]
    return max(keys, key=lambda key: str(key[3]))


# --- serializer ---------------------------------------------------------------------------------


def test_the_investigation_serializer_never_pickles_and_is_not_the_question_graph_serializer() -> (
    None
):
    serde = build_investigation_serializer()
    assert serde.pickle_fallback is False
    assert build_checkpoint_serializer().pickle_fallback is True  # unchanged simple-question graph
    first, second = new_investigation_saver(), new_investigation_saver()
    assert first is not second and first.serde is not second.serde


def test_non_json_objects_cannot_be_checkpointed() -> None:
    serde = build_investigation_serializer()
    with pytest.raises(TypeError):
        serde.dumps_typed(pd.DataFrame({"churned": [1]}))
    with pytest.raises(NotImplementedError):
        serde.loads_typed(("pickle", pickle.dumps({"rows": [1]})))


def test_tampered_objects_are_returned_as_inert_data_never_revived_or_called() -> None:
    serde = build_investigation_serializer()
    pydantic_ext = ormsgpack.packb(
        ormsgpack.Ext(
            5,
            ormsgpack.packb(
                (
                    "packages.investigation.budget",
                    "BudgetUsage",
                    {"model_calls": -1},
                    "model_validate_json",
                )
            ),
        )
    )
    revived = serde.loads_typed(("msgpack", pydantic_ext))
    assert revived == {"model_calls": -1} and type(revived) is dict
    callable_ext = ormsgpack.packb(
        ormsgpack.Ext(0, ormsgpack.packb((__name__, "record_call", "pwned")))
    )
    CALLED.clear()
    assert serde.loads_typed(("msgpack", callable_ext)) == "pwned"
    assert CALLED == []


def _json_constructor(lc: int, path: list[str], **fields: Any) -> tuple[str, bytes]:
    """A legacy ``"json"`` blob asking the reviver to construct ``path`` (as older savers wrote)."""
    return ("json", json.dumps({"lc": lc, "type": "constructor", "id": path, **fields}).encode())


_TRUSTED_STATE_CLASSES = [
    ["packages", "investigation", "budget", "BudgetUsage"],
    ["packages", "investigation", "state", "InvestigationScope"],
    ["packages", "investigation", "state", "InvestigationStateView"],
]


@pytest.mark.parametrize("path", _TRUSTED_STATE_CLASSES)
def test_json_constructor_payloads_never_revive_trusted_investigation_objects(
    path: list[str],
) -> None:
    serde = build_investigation_serializer()
    revived = serde.loads_typed(_json_constructor(2, path, kwargs={"model_calls": -1}))
    assert type(revived) is dict  # inert data, which ``restore_state`` rejects
    with pytest.raises(ValueError):
        serde.loads_typed(_json_constructor(1, path, kwargs={"model_calls": -1}))


@pytest.mark.parametrize("lc", [1, 2])
@pytest.mark.parametrize(
    "fields", [{"args": ["pwned"]}, {"kwargs": {"value": "pwned"}}, {"method": "record_call"}]
)
def test_json_constructor_payloads_never_call_code(lc: int, fields: dict[str, Any]) -> None:
    serde = build_investigation_serializer()
    CALLED.clear()
    with contextlib.suppress(ValueError):
        serde.loads_typed(_json_constructor(lc, [*__name__.split("."), "record_call"], **fields))
    assert CALLED == []


@pytest.mark.parametrize("path", [*_TRUSTED_STATE_CLASSES, [*__name__.split("."), "record_call"]])
def test_a_json_constructor_checkpoint_fails_closed_on_load(path: list[str]) -> None:
    usage, ledger = _usage_step()
    app, saver = _graph([*_approved_run_steps(), usage], ledger=ledger)
    app.invoke({}, _config())
    for lc in (1, 2):
        saver.blobs[_latest_blob_key(saver, "budget_usage")] = _json_constructor(
            lc, path, args=["pwned"], kwargs={"model_calls": -1}
        )
        CALLED.clear()
        with pytest.raises(StateInvalid):
            load_investigation_state(app, _config())
        assert CALLED == []


# --- the saver behind a loaded graph must be the strict investigation saver ----------------------


def _compiled_with(checkpointer: Any) -> Any:
    graph = StateGraph(InvestigationState)
    graph.add_node("intake", guarded_node("intake", lambda s: {"state_version": 1}))
    graph.add_edge(START, "intake")
    graph.add_edge("intake", END)
    return graph.compile(checkpointer=checkpointer)


class _SubclassedSaver(InMemorySaver):
    pass


@pytest.mark.parametrize(
    "checkpointer",
    [
        None,
        InMemorySaver(),  # LangGraph's default serializer: permissive msgpack allowlist
        InMemorySaver(serde=build_checkpoint_serializer()),  # the question graph's pickle saver
        InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False)),
        InMemorySaver(
            serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=True)
        ),
        InMemorySaver(
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_msgpack_modules=None,
                allowed_json_modules=[("packages", "investigation", "budget")],
            )
        ),
        InMemorySaver(
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_msgpack_modules=[("packages.investigation.budget", "BudgetUsage")],
            )
        ),
        _SubclassedSaver(serde=build_investigation_serializer()),
    ],
)
def test_loading_refuses_a_permissive_or_incompatible_saver(checkpointer: Any) -> None:
    app = _compiled_with(checkpointer)
    with pytest.raises(StateInvalid) as caught:
        load_investigation_state(app, _config())
    assert caught.value.code == "checkpointer_not_strict"


def test_a_saver_whose_serializer_was_loosened_after_creation_is_refused() -> None:
    saver = new_investigation_saver()
    assert is_strict_investigation_saver(saver)
    saver.serde.pickle_fallback = True
    assert not is_strict_investigation_saver(saver)
    swapped = new_investigation_saver()
    swapped.serde = build_checkpoint_serializer()
    assert not is_strict_investigation_saver(swapped)


def test_the_saver_check_runs_before_any_blob_is_read() -> None:
    app, saver = _graph(_approved_run_steps())
    app.invoke({}, _config())
    saver.serde = JsonPlusSerializer(pickle_fallback=True)
    saver.blobs[_latest_blob_key(saver, "tasks")] = (
        "pickle",
        pickle.dumps(_PickleCanary()),
    )
    CALLED.clear()
    with pytest.raises(StateInvalid) as caught:
        load_investigation_state(app, _config())
    assert caught.value.code == "checkpointer_not_strict"
    assert CALLED == []


class _PickleCanary:
    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return (record_call, ("unpickled",))


# --- round trip ---------------------------------------------------------------------------------


def test_state_round_trips_through_the_checkpointer_as_json_without_pickle() -> None:
    app, saver = _graph(_approved_run_steps())
    app.invoke({}, _config())
    view = load_investigation_state(app, _config())
    assert view.status == "running"
    assert view.tasks["trend"].status == "running"
    assert len(view.evidence) == 1

    stored = _stored(saver)
    assert stored and all(kind != "pickle" for kind, _ in stored)
    for key, blob in saver.blobs.items():
        if key[2] in REDUCERS and blob[0] != "empty":
            assert is_json_native(saver.serde.loads_typed(blob)), key[2]


# --- tampering ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "blob"),
    [
        (
            "budget_usage",
            (
                "msgpack",
                ormsgpack.packb(
                    ormsgpack.Ext(
                        5,
                        ormsgpack.packb(
                            (
                                "packages.investigation.budget",
                                "BudgetUsage",
                                {"model_calls": -1},
                                "model_validate_json",
                            )
                        ),
                    )
                ),
            ),
        ),
        (
            "tasks",
            (
                "msgpack",
                ormsgpack.packb(ormsgpack.Ext(0, ormsgpack.packb((__name__, "record_call", "x")))),
            ),
        ),
        ("tasks", ("pickle", pickle.dumps({"trend": {"status": "succeeded"}}))),
        ("scope", ("msgpack", ormsgpack.packb({"tenant_id": "tenant_b"}))),
    ],
)
def test_tampered_checkpoints_fail_closed_on_load(channel: str, blob: tuple[str, bytes]) -> None:
    steps = _approved_run_steps()
    usage, ledger = _usage_step()
    if channel == "budget_usage":
        steps.append(usage)
    app, saver = _graph(steps, ledger=ledger)
    app.invoke({}, _config())
    saver.blobs[_latest_blob_key(saver, channel)] = blob
    CALLED.clear()
    with pytest.raises(StateInvalid):
        load_investigation_state(app, _config())
    assert CALLED == []


# --- partial failure and content-free errors ---------------------------------------------------


def test_a_rejected_update_never_reaches_the_checkpoint_and_the_thread_fails_closed() -> None:
    record = evidence_record("segments")  # that task never started
    steps = [
        *_approved_run_steps(),
        (
            "bad",
            "dispatcher",
            lambda s: {
                "tasks": {"segments": task_record("segments", "running", 1)},
                "evidence": {
                    "ev_" + "f" * 32: {**record, "id": "ev_" + "f" * 32, "task_id": "pricing"}
                },
            },
        ),
    ]
    app, saver = _graph(steps)
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert caught.value.code == "evidence_attempt"
    # The last good state is still readable, but a thread whose step failed never resumes.
    assert app.get_state(_config()).values["tasks"]["segments"]["status"] == "pending"
    with pytest.raises(StateInvalid) as failed:
        load_investigation_state(app, _config())
    assert failed.value.code == "step_failed"


def test_an_unguarded_reducer_rejection_poisons_the_thread_and_load_fails_closed() -> None:
    steps = [
        *_approved_run_steps(),
        ("regress", "dispatcher", lambda s: {"tasks": {"trend": task_record("trend")}}),
    ]
    app, _ = _graph(steps, guard=False)
    with pytest.raises(StateUpdateRejected):
        app.invoke({}, _config())
    with pytest.raises(StateInvalid) as caught:
        load_investigation_state(app, _config())
    # S7: an unguarded graph is refused before its (poisoned) thread is even read.
    assert caught.value.code == "graph_not_guarded"


@pytest.mark.parametrize(
    "failure",
    [
        ValueError(f"SELECT * FROM customers WHERE note = '{CANARY}'"),
        KeyError(CANARY),
        RuntimeError(f"/uploads/{CANARY}.pdf"),
    ],
)
def test_node_exceptions_reach_the_checkpoint_only_as_codes(failure: Exception) -> None:
    def explode(state: Mapping[str, Any]) -> Mapping[str, Any]:
        raise failure

    app, saver = _graph([*_approved_run_steps(), ("explode", "dispatcher", explode)])
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert str(caught.value) == "node_failed"
    assert caught.value.__cause__ is None
    for _, payload in _stored(saver):
        assert CANARY.encode() not in payload


def test_non_json_updates_are_refused_inside_the_node() -> None:
    frame = pd.DataFrame({"churned": [1]})
    app, saver = _graph(
        [*_approved_run_steps(), ("leak", "dispatcher", lambda s: {"budget_usage": frame})]
    )
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert caught.value.code == "not_json"
    assert all(kind != "pickle" for kind, _ in _stored(saver))


def test_writers_cannot_write_channels_they_do_not_own_through_the_graph() -> None:
    app, _ = _graph(
        [*_approved_run_steps(), ("sneak", "dispatcher", lambda s: {"status": "completed"})]
    )
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert caught.value.code == "foreign_channel"


# --- LangGraph control flow -----------------------------------------------------------------------


def test_interrupts_pass_through_guarded_nodes_and_checkpoint_without_pickle() -> None:
    def pause(state: Mapping[str, Any]) -> Mapping[str, Any]:
        decision = interrupt({"kind": "sql_approval"})
        if decision != "approve":
            raise ValueError("not approved")
        return {"tasks": {"segments": task_record("segments", "running", 1)}}

    app, saver = _graph([*_approved_run_steps(), ("pause", "dispatcher", pause)])
    paused = app.invoke({}, _config())
    assert paused.get("__interrupt__")
    assert all(kind != "pickle" for kind, _ in _stored(saver))
    app.invoke(Command(resume="approve"), _config())
    assert load_investigation_state(app, _config()).tasks["segments"].status == "running"


def test_commands_cannot_bypass_owned_update() -> None:
    target = running_state()

    def returns_command(state: Mapping[str, Any]) -> Any:
        return Command(update={"status": "completed"})

    def raises_parent_command(state: Mapping[str, Any]) -> Any:
        raise ParentCommand(Command(graph=Command.PARENT, update={"scope": target["scope"]}))

    for step, code in (
        (returns_command, "empty_update"),
        (raises_parent_command, "command_refused"),
    ):
        app, _ = _graph([*_approved_run_steps(), ("command", "dispatcher", step)])
        with pytest.raises(InvestigationNodeError) as caught:
            app.invoke({}, _config())
        assert caught.value.code == code


# --- sequential writers: no two guarded writes in one step ---------------------------------------


def _fan_out(parallel: list[Step]) -> tuple[Any, Any]:
    """Run the approved steps up to a running investigation, then ``parallel`` in one step."""
    graph = StateGraph(InvestigationState)
    previous = START
    for name, owner, step in _approved_run_steps()[:-2]:  # running, tasks registered
        graph.add_node(name, guarded_node(owner, step))
        graph.add_edge(previous, name)
        previous = name
    for name, owner, step in parallel:
        graph.add_node(name, guarded_node(owner, step))
        graph.add_edge(previous, name)
        graph.add_edge(name, END)
    saver = new_investigation_saver()
    return graph.compile(checkpointer=saver), saver


_FINISH: Step = ("finish", "lifecycle", lambda s: {"status": "completed"})
_START_TASK: Step = (
    "start",
    "dispatcher",
    lambda s: {"tasks": {"trend": task_record("trend", "running", 1)}},
)


def test_each_writer_alone_accepts_what_the_combined_step_must_not() -> None:
    # Validated against the same running state, each update passes on its own. Applied together
    # they would leave a completed investigation with a newly started task, which restore cannot
    # tell from legitimate closing bookkeeping (tasks still running when the status became
    # terminal). Only refusing the shared step prevents it.
    state = running_state()
    assert owned_update("lifecycle", state, {"status": "completed"})
    assert owned_update(
        "dispatcher", state, {"tasks": {"trend": task_record("trend", "running", 1)}}
    )
    combined = {**state, "status": "completed"}
    combined["tasks"] = {**state["tasks"], "trend": task_record("trend", "running", 1)}
    assert restore_state(combined).status == "completed"


@pytest.mark.parametrize(
    "parallel",
    [
        [_FINISH, _START_TASK],  # a terminal transition racing a task start
        [
            ("cancel", "lifecycle", lambda s: {"status": "cancelled"}),
            (
                "start",
                "dispatcher",
                lambda s: {"tasks": {"trend": task_record("trend", "running", 1)}},
            ),
        ],
        [  # writers on unrelated channels are refused too: the constraint is per step, not per pair
            (
                "start",
                "dispatcher",
                lambda s: {"tasks": {"trend": task_record("trend", "running", 1)}},
            ),
            # Re-asserting the write-once plan is valid alone; it still makes a second writer.
            ("replan", "supervisor", lambda s: {"plan": s["plan"]}),
        ],
    ],
    ids=["completed+start", "cancelled+start", "unrelated-channels"],
)
def test_two_guarded_writers_in_one_step_fail_closed(parallel: list[Step]) -> None:
    app, _ = _fan_out(parallel)
    with pytest.raises(InvalidUpdateError):
        app.invoke({}, _config())
    with pytest.raises(StateInvalid) as caught:
        load_investigation_state(app, _config())
    assert caught.value.code == "checkpoint_unreadable"


def test_the_same_writes_in_consecutive_steps_are_checked_against_each_other() -> None:
    finish_name, finish_owner, finish = _FINISH
    start_name, start_owner, start = _START_TASK
    app, _ = _graph(
        [
            *_approved_run_steps()[:-2],
            (finish_name, finish_owner, finish),
            (start_name, start_owner, start),
        ]
    )
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert caught.value.code == "investigation_terminal"


def test_every_guarded_write_names_its_writer() -> None:
    app, _ = _graph(_approved_run_steps())
    app.invoke({}, _config())
    view = load_investigation_state(app, _config())
    assert view.step_writer == "dispatcher"


# --- the dispatcher's budget writes are bound to the gateway's ledger -----------------------------


def test_only_the_budget_writer_can_be_bound_and_only_to_a_real_ledger() -> None:
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    with pytest.raises(ValueError):
        guarded_node("lifecycle", lambda s: {"status": "completed"}, ledger=ledger)
    lookalike: Any = type("Lookalike", (), {"usage": ledger.usage})()
    with pytest.raises(ValueError):
        guarded_node("dispatcher", lambda s: {}, ledger=lookalike)


def _settlement_graph(settle: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Any:
    """Start a call under the gateway ledger, cancel the run, then let ``settle`` write budget."""
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    reservation = ledger.authorize(BudgetRequest(model_calls=3, input_tokens=300, output_tokens=60))

    def reserve(state: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"budget_usage": ledger.usage().model_dump(mode="json")}

    def cancel(state: Mapping[str, Any]) -> Mapping[str, Any]:
        # S5's job, simulated: on the terminal status the gateway closes and settles its ledger.
        ledger.close("cancelled")
        ledger.settle(reservation, input_tokens=120, output_tokens=40)
        return {"status": "cancelled"}

    graph = StateGraph(InvestigationState)
    previous = START
    for name, node in [
        *((name, guarded_node(owner, step)) for name, owner, step in _approved_run_steps()[:-2]),
        ("reserve", guarded_node("dispatcher", reserve, ledger=ledger)),
        ("cancel", guarded_node("lifecycle", cancel)),
        ("settle", guarded_node("dispatcher", settle, ledger=ledger)),
    ]:
        graph.add_node(name, node)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph.compile(checkpointer=new_investigation_saver()), ledger


def test_a_terminal_settlement_through_the_graph_must_match_the_bound_ledger() -> None:
    holder: dict[str, BudgetLedger] = {}

    def genuine(state: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"budget_usage": holder["ledger"].usage().model_dump(mode="json")}

    app, holder["ledger"] = _settlement_graph(genuine)
    app.invoke({}, _config())
    view = load_investigation_state(app, _config())
    assert view.budget_usage is not None and view.budget_usage.closed_reason == "cancelled"
    assert view.budget_usage.outstanding_reservations == 0

    def forged(state: Mapping[str, Any]) -> Mapping[str, Any]:
        usage = holder["ledger"].usage().model_dump(mode="json")
        return {"budget_usage": {**usage, "input_tokens": 149_000, "output_tokens": 24_000}}

    app, holder["ledger"] = _settlement_graph(forged)
    with pytest.raises(InvestigationNodeError) as caught:
        app.invoke({}, _config())
    assert caught.value.code == "settlement_unverified"


def test_threads_are_isolated() -> None:
    app, _ = _graph(_approved_run_steps())
    app.invoke({}, _config("tenant_a:ws_1:inv_1"))
    with pytest.raises(StateInvalid):
        load_investigation_state(app, _config("tenant_b:ws_9:inv_1"))


def test_a_well_formed_foreign_scope_restores_but_is_refused_by_the_authority_binding() -> None:
    app, saver = _graph(_approved_run_steps())
    app.invoke({}, _config())
    foreign = {**SCOPE, "tenant_id": "tenant_b"}
    saver.blobs[_latest_blob_key(saver, "scope")] = saver.serde.dumps_typed(foreign)
    view = load_investigation_state(app, _config())  # structurally valid on its own
    assert view.scope is not None and view.scope.tenant_id == "tenant_b"
    live = LiveAuthority()
    with pytest.raises(GatewayRefusal) as caught:
        authorize_state(
            investigation_authority(),
            app.get_state(_config()).values,
            live=live,
            clock=FixedClock(datetime(2026, 10, 17, 12, tzinfo=UTC)),
        )
    assert caught.value.code is RefusalCode.SCOPE_MISMATCH
    assert live.calls == []
