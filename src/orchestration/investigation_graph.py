"""The only registration interface for investigation graphs (ADR 0024, #136 S7).

An investigation graph is built through ``InvestigationGraphBuilder``, never with ``StateGraph``
directly. The builder applies the INV-3 boundary by construction:

- every node is a ``guarded_node``: a step can only *propose* an update, which ``owned_update``
  validates before LangGraph stores it, and the builder has no way to register a raw node;
- every budget writer (the dispatcher) is bound to the trusted gateway's own ``BudgetLedger``, so
  recorded usage always equals the gateway's ledger snapshot; a step cannot choose another ledger;
- the graph always compiles with a fresh strict, pickle-free investigation saver;
- the compiled graph is checked with ``require_guarded_graph`` before it is returned, and
  ``load_investigation_state`` checks it again before reading any checkpoint.

Edges and conditional edges route between registered steps; they never write state.

No investigation graph is wired into a route, the API, or the question graph yet. INV-4 (the
Supervisor) and INV-5 (the specialists and dispatcher) must build theirs here.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from packages.investigation.state import CHANNEL_OWNERS, InvestigationState, Writer
from src.orchestration.investigation_checkpoint import (
    NodeStep,
    guarded_node,
    new_investigation_saver,
    require_guarded_graph,
)
from src.orchestration.tool_gateway import InvestigationToolGateway


class InvestigationGraphBuilder:
    """Build an investigation graph whose every node is guarded and ledger-bound."""

    def __init__(self, gateway: InvestigationToolGateway) -> None:
        if type(gateway) is not InvestigationToolGateway:
            raise TypeError(
                "An investigation graph is bound to a trusted InvestigationToolGateway."
            )
        self.__ledger = gateway.ledger
        self.__graph: StateGraph[Any] = StateGraph(InvestigationState)
        self.__names: set[str] = set()
        self.__compiled = False

    def add_step(self, name: str, owner: Writer, step: NodeStep) -> None:
        """Register ``step`` as a guarded node that writes only the channels ``owner`` owns."""
        self._require_open()
        if not isinstance(name, str) or not name or name in self.__names:
            raise ValueError("Each step needs a new, non-empty name.")
        if not callable(step):
            raise TypeError("A step must be callable.")
        ledger = self.__ledger if owner == CHANNEL_OWNERS["budget_usage"] else None
        self.__graph.add_node(name, guarded_node(owner, step, ledger=ledger))
        self.__names.add(name)

    def add_edge(self, start: str | Sequence[str], end: str) -> None:
        self._require_open()
        self.__graph.add_edge(start, end)

    def add_conditional_edges(
        self,
        source: str,
        path: Callable[[Mapping[str, Any]], Hashable | Sequence[Hashable]],
        path_map: Mapping[Hashable, str] | None = None,
    ) -> None:
        """Route by state; the path function reads state and returns step names, never writes."""
        self._require_open()
        self.__graph.add_conditional_edges(source, path, path_map)

    def compile(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        """Compile with a strict saver, then refuse the graph unless every node is guarded."""
        self._require_open()
        self.__compiled = True
        graph = self.__graph.compile(checkpointer=new_investigation_saver())
        require_guarded_graph(graph, ledger=self.__ledger)
        return graph

    def _require_open(self) -> None:
        if self.__compiled:
            raise RuntimeError("This investigation graph has already been compiled.")
