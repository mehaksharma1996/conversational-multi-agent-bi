"""Investigation specialists, Supervisor, and dispatcher modules (ADR 0024; INV-4 and INV-5).

``supervisor`` (INV-4 S3) plans only through a ``PlannerPort``
(``InvestigationPlanningGateway.planner_port()``): one initial call and at most one repair.
``sql_analyst`` and ``document_researcher`` (INV-5 S3) run one admitted task each, one port call
per step; the dispatcher and run owner that drive them are trusted and live in
``src/orchestration/investigation_execution.py`` (INV-5 decision R7). Every
module added here is untrusted with respect to execution and is checked structurally by
``tests/test_investigation_boundaries.py``:

- it may reach SQL, document research, and models only through a ``SpecialistGateway``
  (``InvestigationToolGateway.specialist_port()``), with the typed requests from
  ``src.orchestration.tool_gateway``;
- it may build a graph only with ``InvestigationGraphBuilder``
  (``src.orchestration.investigation_graph``);
- it may not import services, executors, retrievers, model clients, storage, ``BudgetLedger``,
  LangGraph, evaluation code, or evaluator-only data, and it may not use private attributes or
  dynamic access (``getattr``, ``eval``, ``__import__``, and similar).
"""
