# Frontend usage, limits, and troubleshooting

Applies to the React client in `apps/web/`. Design of the reload behavior is in
[workspace recovery](../architecture/workspace-recovery.md); a guided tour is in the
[demo walkthrough](demo-walkthrough.md).

## How an answer is presented

| Part | Source | What it means |
|---|---|---|
| Headline, observations, limits | Computed by code from the query rows (`src/agents/sql_insights.py`) | Values keep the query's precision; no currency or unit is guessed; causal wording is disclaimed. The API does not return cell-level references, so check them against the table. |
| Generated SQL | The model's draft, after validation | Always visible. Not proof it answers the intended question. |
| Result table | The executed rows | First 25 rows shown; *Show all* reveals up to the server's 500-row cap. |
| Sources | Retrieved passages | File, page, relevance, and the first 240 characters (personal data redacted). "Source N" in an answer links to its passage. |
| What was checked | `provenance.evidence_statuses`, `policy_sql_status` | Structural checks on references, quotations, and literal policy values. `Reference verified` is not semantic truth; `Meaning not evaluated` is shown when meaning was not assessed. |
| How this was produced | `provenance.execution` and answer status | Ordered workflow stages derived from recorded counts. No timestamps, prompts, or model output exist behind it. |

## Approval states

`Awaiting approval` (nothing has run) → `Approve SQL` / `Edit SQL` + `Run edited SQL` / `Reject SQL`.
A rejection, a refused edit, or a restart ends as `Not executed` with no result table. A policy-linked
query whose edit no longer applies the policy's values falls back to the document answer.

## Known limitations

- Pending approvals live in API process memory and are cancelled safely on restart.
- The interface restores one workspace per page load; there is no workspace picker.
- Older answers lose their rows once more than `MAX_CHAT_DATAFRAMES_RETAINED` newer ones exist.
- Accessibility is checked with axe, keyboard journeys, and responsive checks in Chromium only. No
  manual screen-reader test and no formal WCAG audit has been done, and Plotly's SVG is excluded
  from axe. Nothing here claims WCAG conformance.
- Browser verification ran in Chromium on Windows. Firefox, WebKit, and real devices are untested.
- The scripted-model journeys do not measure real-model accuracy.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Restoring or starting workspace…" for several seconds after an API restart | The first request rebuilds the table, analysis, and document index. Wait; it ends with "Your workspace was restored." |
| "Your earlier workspace is no longer available." | It expired (default 24 h of inactivity), was deleted, or the API runs without `DURABLE_METADATA=true` and restarted. A new workspace was created; re-upload. |
| Error with *Retry workspace* on load | The API was unreachable or unhealthy. Check `/health/ready`, then retry. No new workspace is created while this error shows. |
| Sample questions are greyed out | The demo table must be confirmed (schema step) and, for model-backed questions, the data-sharing notice accepted. Policy and hybrid questions also need the policy PDF. |
| "Could not reach the server" / "took too long" | Network failure or the 120 s client timeout. Your question is put back in the box; send it again. |
| A pending approval became "Not executed" | The API restarted (or an edit was refused). Ask again. |
| `Load demo data` says the files could not be loaded | The static server did not serve `/demo/*`. Rebuild the web bundle (`apps/web/public/demo` is copied into it). |
| Real-stack run fails to start | Ensure `.venv` exists, port 8010 and 4180 are free, and `npm run build` succeeded. Logs: `work/e2e-stack/api.log`, `web.log`. |

## Running the browser tests

```powershell
cd apps/web
npm run test:e2e        # mocked API journeys + axe baselines (the Docker-only specs skip)
npm run test:e2e:real   # real API with a scripted model (no Docker); builds the bundle first
```

`E2E_CAPTURE=1` additionally refreshes `docs/assets/demo/*.png`. The Docker-based real-stack specs
(`E2E_REAL_STACK=local|fake`) still require Compose and were not run for this change.
