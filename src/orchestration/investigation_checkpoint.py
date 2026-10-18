"""Process-local investigation checkpoints: strict, pickle-free, and fail closed (ADR 0024, #136).

Investigation state (``packages.investigation.state``) is JSON-native, so its checkpoints never need
pickle or class revival. This module keeps them that way:

- ``build_investigation_serializer`` disables the pickle fallback **and** the permissive msgpack
  allowlist. LangGraph's default allowlist revives any class named in a checkpoint (falling back to
  ``model_construct``, which skips validation) and calls arbitrary constructors; the strict
  allowlist returns such payloads as plain data, which ``restore_state`` then rejects. It still
  revives LangGraph's own ``Interrupt`` for approval pauses. Legacy ``"json"`` blobs are strict
  too: an ``lc: 2`` constructor outside LangGraph's safe types comes back as a plain dict, and an
  ``lc: 1`` constructor outside LangChain's core allowlist raises.
- ``new_investigation_saver`` gives every investigation runtime its own ``InMemorySaver``. It must
  never share the question graph's saver, whose serializer keeps the pickle fallback for DataFrames.
- ``guarded_node`` validates a node's update with ``owned_update`` *before* LangGraph stores it, and
  replaces any exception with a code-only ``InvestigationNodeError``, because LangGraph stores a
  failing node's ``repr(exc)`` in the checkpoint. Only interrupts (approval pauses) pass through.
  A step that returns a ``Command`` is refused like any non-mapping update, and a raised
  ``ParentCommand`` (which would write another graph's state unvalidated) becomes a node error.
  Every guarded write also writes the reducer-less ``step_writer`` channel, so two guarded writers
  in one step make LangGraph refuse the step (``InvalidUpdateError``) and the thread unreadable:
  writers run one at a time, because each is validated only against the state before its step.
  The dispatcher's node is bound to the trusted gateway's ``BudgetLedger``; every change to
  budget usage must equal that ledger's snapshot, and is refused without one.
- ``load_investigation_state`` first checks the graph's checkpointer is a strict investigation
  saver (``is_strict_investigation_saver``), then reads the thread back and fully revalidates it.
  A permissive or unknown saver, a thread that cannot be read, a thread whose last step failed, or
  values that fail validation raise ``StateInvalid``; the investigation must then fail and never
  resume.

Checkpoints are process-local by design (ADR 0024 §9). Durable, encrypted checkpoints are #149.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphBubbleUp, ParentCommand

from packages.investigation.budget import BudgetLedger
from packages.investigation.state import (
    CHANNEL_OWNERS,
    STEP_WRITER_CHANNEL,
    InvestigationStateError,
    InvestigationStateView,
    StateInvalid,
    Writer,
    owned_update,
    restore_state,
)

NodeStep = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def build_investigation_serializer() -> JsonPlusSerializer:
    """No pickle, and no class revival beyond LangGraph's safe built-in types."""
    return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)


def new_investigation_saver() -> InMemorySaver:
    """A fresh process-local saver for investigation threads only."""
    return InMemorySaver(serde=build_investigation_serializer())


def is_strict_investigation_saver(saver: object) -> bool:
    """True only for a saver configured exactly as ``new_investigation_saver`` builds one.

    Exact types, so a subclass or wrapper cannot change how blobs are read. The serializer must
    have no pickle fallback, no extra msgpack or JSON allowlist, and no custom msgpack hook. A
    missing attribute (for example after a library change) counts as permissive.
    """
    if type(saver) is not InMemorySaver:
        return False
    serde = getattr(saver, "serde", None)
    return (
        type(serde) is JsonPlusSerializer
        and getattr(serde, "pickle_fallback", True) is False
        and getattr(serde, "_allowed_msgpack_modules", True) is None
        and getattr(serde, "_allowed_json_modules", True) is None
        and getattr(serde, "_custom_unpack_ext_hook", True) is False
    )


class InvestigationNodeError(RuntimeError):
    """The only exception an investigation node lets reach a checkpoint. Its message is a code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def guarded_node(
    owner: Writer, step: NodeStep, *, ledger: BudgetLedger | None = None
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Wrap ``step`` so its update is validated in the node and its failures are content-free.

    ``step`` receives the current raw state and returns the channels ``owner`` writes, as a plain
    mapping. Interrupts pass through unchanged, so approval pauses work. Commands do not: a returned
    ``Command`` is not a mapping and is refused (``empty_update``), and a raised ``ParentCommand``
    becomes a code-only node error, because neither would pass through ``owned_update``.

    The validated update is returned with ``step_writer`` set to ``owner``, so LangGraph refuses a
    step in which two guarded nodes write. ``ledger`` binds the budget writer to the trusted
    gateway's ledger when the graph is built; the step itself never supplies it.
    """
    if ledger is not None and (
        type(ledger) is not BudgetLedger or owner != CHANNEL_OWNERS["budget_usage"]
    ):
        raise ValueError("Only the budget writer may be bound, and only to a BudgetLedger.")

    def node(state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            validated = owned_update(owner, state, step(state), ledger=ledger)
            return {**validated, STEP_WRITER_CHANNEL: owner}
        except ParentCommand:
            raise InvestigationNodeError("command_refused") from None
        except GraphBubbleUp:
            raise
        except InvestigationStateError as exc:
            raise InvestigationNodeError(exc.code) from None
        except Exception:
            raise InvestigationNodeError("node_failed") from None

    return node


def load_investigation_state(graph: Any, config: Mapping[str, Any]) -> InvestigationStateView:
    """Check the saver is strict, read a thread's latest checkpoint, and revalidate it.

    Raises ``StateInvalid`` otherwise. The saver check runs before anything is deserialized.
    """
    if not is_strict_investigation_saver(getattr(graph, "checkpointer", None)):
        raise StateInvalid("checkpointer_not_strict")
    try:
        snapshot = graph.get_state(config)
    except Exception:
        # A poisoned thread (a stored write its reducer rejects), an unknown blob type such as a
        # pickle, or a corrupt payload: none of it may be trusted or resumed.
        raise StateInvalid("checkpoint_unreadable") from None
    if any(getattr(task, "error", None) is not None for task in snapshot.tasks):
        raise StateInvalid("step_failed")
    return restore_state(snapshot.values)
