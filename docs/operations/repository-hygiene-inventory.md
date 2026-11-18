# Repository hygiene inventory

HIRE-4 ([#207](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/207)).
A snapshot taken on **2026-11-17** of the maintainer's local clone and the GitHub remote: worktrees,
branches, and untracked local files, each with a **proposed** action. Nothing listed here has been
removed. Every removal needs explicit approval for that item, and the approval is recorded in the
log at the end.

## How it was collected

Read-only: `git worktree list --porcelain`, `git for-each-ref`, `git status --porcelain`, and
`gh pr list --state all`. A branch or worktree is proposed for removal only when its latest pull
request is **merged** and its tip commit **equals that pull request's head commit**, so it holds no
unmerged work; no merged branch failed that check in this snapshot. A worktree with uncommitted
changes is always kept. Paths are relative to the repository root; local machine paths are omitted.

## Summary

| Item | Total | Proposed for removal (merged, no extra commits) | Keep or review |
|---|---|---|---|
| Worktrees (excluding the primary checkout) | 45 | 33 | 12 |
| Local branches not checked out | 16 | 14 | 2 |
| Remote branches (excluding `main` and Dependabot) | 58 | 48 | 10 |
| Untracked paths in the primary checkout | 4 | 0 | 4 |
| Open Dependabot pull requests | 20 | not covered here | reviewed with the dependency process |

## Worktrees

| Worktree | Branch | PR | Dirty | Proposed action |
|---|---|---|---|---|
| `.` | `feat/iteration2-ui-stage1-workbench-shell` | #130 merged | no | Keep: primary checkout |
| `../cmab-readme-start-here (outside the repository)` | `docs/readme-start-here` | #218 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/132-ui2-operation-state-recovery` | `feat/iteration2-ui2-operation-state-recovery` | #199 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/133-inv0-investigation-contracts` | `feat/iteration2-inv0-investigation-contracts` | #160 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/134-inv1-guarded-services` | `feat/iteration2-inv1-guarded-services` | #161 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/135-inv2-churn-fixtures` | `issue/135-inv2-churn-fixtures` | #162 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/136-inv3-state-gateway` | `feat/iteration2-inv3-state-gateway` | #165 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/137-inv4-supervisor-planner` | `feat/iteration2-inv4-supervisor-planner` | #166 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/138-inv5-dispatcher-specialists` | `feat/iteration2-inv5-dispatcher-specialists` | #167 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/139-inv6-business-investigation` | `feat/iteration2-inv6-business-investigation` | #174 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/140-inv7-critic-replanning` | `feat/iteration2-inv7-critic-replanning` | #175 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/141-inv8-verified-reporting` | `feat/iteration2-inv8-verified-reporting` | #184 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/142-inv9-investigation-route` | `feat/iteration2-inv9-investigation-route` | #186 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/143-inv10-approval-audit` | `feat/iteration2-inv10-approval-audit` | #188 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/144-inv11-investigation-api` | `feat/iteration2-inv11-investigation-api` | #196 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/145-eval1-trajectory` | `feat/iteration2-eval1-trajectory-suite` | #210 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/146-obs1-telemetry` | `feat/iteration2-obs1-investigation-telemetry` | #212 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/148-eval3-concurrency` | `feat/iteration2-eval3-concurrency-rate-limits` | #223 open | no | Keep: open PR |
| `work/agent-worktrees/149-inv12-adr` | `docs/inv12-durable-checkpoints-adr` | #230 open | no | Keep: open PR |
| `work/agent-worktrees/151-ui3-upload-profile-schema` | `feat/iteration2-ui3-upload-profile-schema` | #217 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/152-ui4-analysis` | `feat/iteration2-ui4-analysis-charts` | #225 open | no | Keep: open PR |
| `work/agent-worktrees/153-ui5-documents` | `feat/iteration2-ui5-documents` | #226 open | no | Keep: open PR |
| `work/agent-worktrees/154-ui6a-ask-conversations` | `feat/iteration2-ui6a-ask-conversations` | #200 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/155-ui6b-investigation-workflow` | `feat/iteration2-ui6b-investigation-workflow` | #201 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/156-ui6c-evidence-report` | `feat/iteration2-ui6c-evidence-report` | #211 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/157-sec1-hardening` | `feat/iteration2-sec1-investigation-hardening` | #213 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/185-planning-prompt-provenance` | `feat/iteration2-185-planning-prompt-provenance` | #220 open | no | Keep: open PR |
| `work/agent-worktrees/198-prod1-bounded-adapter` | `feat/iteration2-prod1-bounded-adapter` | #208 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/198-prod1-s6-smoke` | `feat/iteration2-prod1-s6-smoke-tooling` | #221 open | no | Keep: open PR |
| `work/agent-worktrees/204-hire1-demo` | `feat/iteration2-hire1-investigation-demo` | #216 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/222-schema-focus` | `fix/web-schema-confirm-focus-222` | #224 open | no | Keep: open PR |
| `work/agent-worktrees/ask-lazy-panel-flake` | `fix/web-ask-lazy-panel-wait` | #228 open | no | Keep: open PR |
| `work/agent-worktrees/ci-aiohttp-security` | `fix/ci-aiohttp-security` | #173 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/consent-digest-fix` | `fix/consent-legacy-digest-recipients` | #229 open | no | Keep: open PR |
| `work/agent-worktrees/frontend-ask-recovery-focus` | `fix/frontend-ask-recovery-focus` | #163 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/hotfix-investigation-cancel-lifecycle` | `fix/iteration2-investigation-cancel-lifecycle` | #203 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/integration-sprint` | `integration/iteration2-sprint-local` | none | no | Review: no PR; keep until its purpose is confirmed |
| `work/agent-worktrees/iter2-audit` | `(detached)` | none | no | Review: no PR; keep until its purpose is confirmed |
| `work/agent-worktrees/oracle-scan-skip-dependencies` | `fix/oracle-scan-skip-dependencies` | #164 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/readme-daily-20261114` | `(detached)` | none | no | Review: no PR; keep until its purpose is confirmed |
| `work/agent-worktrees/security-authlib` | `fix/security-authlib-langgraph-sdk` | #197 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/security-certifi-pillow` | `fix/security-certifi-pillow-advisories` | #176 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/security-ci-dependency-remediation` | `fix/security-ci-dependency-remediation` | #187 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/security-joserfc` | `fix/security-joserfc-cve` | #202 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/security-otel-cve-2026-126360` | `fix/opentelemetry-security-cve-2026-126360` | #219 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |
| `work/agent-worktrees/verdict-verification` | `fix/iteration2-independent-verdict-verification` | #215 merged | no | Remove worktree after approval (PR merged, tip equals PR head) |

## Local branches (not checked out in a worktree)

| Local branch | PR | Proposed action |
|---|---|---|
| `demo/grounded-bi-experience` | #100 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `docs/usage-guardrail-ask-user` | none | Review: no PR; keep until its purpose is confirmed |
| `docs/v1-portfolio-demo` | none | Review: no PR; keep until its purpose is confirmed |
| `feat/iteration2-e1-evaluation-baseline` | #107 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-e1b-versioned-evaluation-baseline` | #108 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-e1b2-workflow-evaluation-baseline` | #127 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-a1-report-document-context` | #105 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-a2-anomaly-count-semantics` | #106 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-s0-dotenv-isolation` | #128 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-s0b-explicit-runtime-config` | #129 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `issue/96-numeric-format-integrity` | #97 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `portfolio/authentic-browser-captures` | #104 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `portfolio/readme-architecture` | #102 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `portfolio/readme-redesign` | #103 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `security/98-web-base-cve` | #99 merged | Remove local branch after approval (PR merged, tip equals PR head) |
| `toolkit/developer-agent-toolkit` | #101 merged | Remove local branch after approval (PR merged, tip equals PR head) |

## Remote branches

| Remote branch | PR | Proposed action |
|---|---|---|
| `demo/grounded-bi-experience` | #100 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `docs/inv12-durable-checkpoints-adr` | #230 open | Keep: open PR |
| `docs/readme-start-here` | #218 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `docs/usage-guardrail-ask-user` | none | Review: no PR; keep until its purpose is confirmed |
| `feat/iteration2-185-planning-prompt-provenance` | #220 open | Keep: open PR |
| `feat/iteration2-e1-evaluation-baseline` | #107 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-e1b-versioned-evaluation-baseline` | #108 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-e1b2-workflow-evaluation-baseline` | #127 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-eval1-trajectory-suite` | #210 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-eval3-concurrency-rate-limits` | #223 open | Keep: open PR |
| `feat/iteration2-hire1-investigation-demo` | #216 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv0-investigation-contracts` | #160 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv1-guarded-services` | #161 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv10-approval-audit` | #188 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv11-investigation-api` | #196 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv3-state-gateway` | #165 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv4-supervisor-planner` | #166 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv5-dispatcher-specialists` | #167 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv6-business-investigation` | #174 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv7-critic-replanning` | #175 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv8-verified-reporting` | #184 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-inv9-investigation-route` | #186 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-obs1-investigation-telemetry` | #212 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-prod1-bounded-adapter` | #208 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-prod1-s6-smoke-tooling` | #221 open | Keep: open PR |
| `feat/iteration2-sec1-investigation-hardening` | #213 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui-stage1-workbench-shell` | #130 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui2-operation-state-recovery` | #199 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui3-upload-profile-schema` | #217 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui4-analysis-charts` | #225 open | Keep: open PR |
| `feat/iteration2-ui5-documents` | #226 open | Keep: open PR |
| `feat/iteration2-ui6a-ask-conversations` | #200 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui6b-investigation-workflow` | #201 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `feat/iteration2-ui6c-evidence-report` | #211 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/ci-aiohttp-security` | #173 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/consent-legacy-digest-recipients` | #229 open | Keep: open PR |
| `fix/frontend-ask-recovery-focus` | #163 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-a1-report-document-context` | #105 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-a2-anomaly-count-semantics` | #106 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-independent-verdict-verification` | #215 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-investigation-cancel-lifecycle` | #203 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-s0-dotenv-isolation` | #128 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/iteration2-s0b-explicit-runtime-config` | #129 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/opentelemetry-security-cve-2026-126360` | #219 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/oracle-scan-skip-dependencies` | #164 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/security-authlib-langgraph-sdk` | #197 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/security-certifi-pillow-advisories` | #176 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/security-ci-dependency-remediation` | #187 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/security-joserfc-cve` | #202 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `fix/web-ask-lazy-panel-wait` | #228 open | Keep: open PR |
| `fix/web-schema-confirm-focus-222` | #224 open | Keep: open PR |
| `issue/135-inv2-churn-fixtures` | #162 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `issue/96-numeric-format-integrity` | #97 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `portfolio/authentic-browser-captures` | #104 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `portfolio/readme-architecture` | #102 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `portfolio/readme-redesign` | #103 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `security/98-web-base-cve` | #99 merged | Remove remote branch after approval (PR merged, tip equals PR head) |
| `toolkit/developer-agent-toolkit` | #101 merged | Remove remote branch after approval (PR merged, tip equals PR head) |

## Untracked local files

| Untracked path (primary checkout) | Proposed action |
|---|---|
| `.archify/architecture-readme-system-20261013-031505/system-architecture.html` | Review: local artefact; keep out of git, or delete after approval |
| `.archify/architecture-readme-system-20261013-031505/visual-check/` | Review: local artefact; keep out of git, or delete after approval |
| `.archify/workflow-hybrid-policy-sql-20261013-031505/hybrid-policy-sql-workflow.html` | Review: local artefact; keep out of git, or delete after approval |
| `repository-audit-for-chatgpt.md` | Review: local artefact; keep out of git, or delete after approval |

## Approval log

| Date | Item | Action | Approved by |
|---|---|---|---|
| (none yet) | | | |

To refresh this page, rerun the read-only commands above and keep the same rule: remove only a merged
branch whose tip equals its pull request's head, and record each approval here.
