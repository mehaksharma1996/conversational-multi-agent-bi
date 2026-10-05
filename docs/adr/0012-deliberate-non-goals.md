# ADR 0012: Deliberate non-goals for the local BI product

- Status: Accepted
- Date: 2026-10-05
- Issue: [#32](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/32), under [#33](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/33)

## Context and evidence

The current product is a local-first business intelligence application for uploaded tabular files and PDFs. Its architecture has explicit boundaries for the API, analytics, retrieval, orchestration, governance, and evaluation. The job execution boundary remains deferred under [ADR 0005](0005-job-execution-boundary.md); the local container topology and restart-persistence waiver are recorded in [ADR 0010](0010-local-container-release.md); the product UI direction is recorded in [ADR 0011](0011-streamlit-disposition.md). Hosting remains follow-up work in the [definition-of-done ledger](../architecture/definition-of-done.md).

These decisions keep the implementation and operational model proportionate to the product's current local scope. Each item below can be reconsidered when its stated evidence appears.

## Decision

The following are deliberate non-goals for the current product scope:

| Non-goal | Why not now | Revisit when |
|---|---|---|
| Kubernetes, Helm, and managed-cloud deployment | The supported topology is a single local Compose deployment. A cluster and managed service would add deployment, security, and operational requirements without a current supported hosted environment. | A funded hosted deployment has named operators, availability and recovery targets, and a documented tenant and security model. |
| Event-driven architecture and message brokers | Current workflows fit the API and existing job boundary; introducing brokers would add delivery, ordering, retry, and operations concerns. | Measured workload or user-facing latency shows that asynchronous processing cannot meet documented service targets, and a workload requires durable cross-process delivery. |
| Fine-tuning and model hosting | The current bounded model tasks use an external provider, while local deterministic analytics cover tasks that do not need a model. Training and serving models would require data governance, infrastructure, and evaluation capability not established here. | Evaluations show a material, repeatable quality or privacy gap that cannot be addressed with prompts, retrieval, deterministic logic, or provider configuration, and a governed dataset and serving plan exist. |
| Agent frameworks beyond LangGraph | A second framework would duplicate orchestration concepts and increase maintenance without a demonstrated workflow need. | A required workflow cannot be represented safely or maintained effectively in LangGraph, demonstrated by a concrete use case and comparative prototype. |
| Knowledge graphs and GraphRAG | Current document retrieval does not establish that graph modeling improves answers enough to justify entity extraction, graph storage, and upkeep. | Evaluation on representative multi-document questions shows persistent relationship-retrieval failures that a graph prototype materially resolves. |
| Computer-use and browser agents | The application has defined file-upload and API workflows; autonomous interaction with external interfaces adds unpredictable actions and a broader security boundary. | A supported user workflow requires interaction with an external interface, and a constrained prototype demonstrates reliable behavior with explicit authorization and safety controls. |
| A full MLOps platform | Current model use does not include owned model training or serving that would justify model registries, deployment pipelines, and lifecycle operations. | The product owns deployed or fine-tuned models and repeated operational evidence shows versioning, promotion, monitoring, or rollback cannot be handled by the existing release and evaluation process. |

## Deliberately small implementation surfaces

The current implementation also keeps two existing boundaries narrow. There is a single model provider today; [issue #27](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/27) tracks changing that. SQL remains guarded within the SQLite boundary; [issue #28](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/28) tracks that surface. These are scoped implementation decisions, not endorsements of broader provider or database support.

## Consequences

The current scope avoids operating infrastructure and abstractions without evidence of need. The revisit triggers make these boundaries reviewable as workload, evaluation, and deployment evidence changes. Reopening a non-goal requires a concrete proposal and the relevant evidence, with a new ADR if an accepted architectural decision changes.
