# React accessibility baseline

The React client is checked with `@axe-core/playwright` 4.13.0, exact-pinned because accessibility
rules can change across releases and should move only in a reviewed dependency update. Playwright
also exercises keyboard behavior in Chromium. These checks run in the frontend CI job and against
the nginx image in the container job.

## Automated baseline

`apps/web/e2e/accessibility.spec.ts` requires zero axe violations for:

- a ready workspace and empty workflow;
- deep-linked workbench navigation with an announced active destination and focus moved to the
  selected section heading;
- the phone navigation drawer before and after selecting a destination;
- schema review, including its profile table and canonical-field controls;
- an analysis dashboard containing a Plotly chart, anomaly caveat, tables, and report actions;
- a chat answer with sources and a grounding warning;
- pending SQL approval controls; and, against the real API with a scripted model
  (`apps/web/e2e/real`), the restored workspace with answers, evidence panels, and an approval, at
  desktop and 375 px widths;
- the safe API error banner with its request ID.

The keyboard-only journey proves the skip link is first, becomes visible on focus, moves focus into
`main`, then reaches the workspace summaries and guided-demo control in document order. It verifies
the three-pixel focus indicator and tabs repeatedly through multiple control types without a trap.
Semantic headings, landmarks, labels, active navigation, table headers/captions, live regions,
alert roles, and color contrast outside Plotly are covered by axe.

`apps/web/e2e/workbench-shell.spec.ts` adds keyboard-only phone-width coverage: the closed drawer is
inert and skipped by Tab; opening it focuses the active link, keeps focus inside the modal drawer
(with zero axe violations while open), and Escape or the close button returns focus to the toggle;
viewport changes never leave focus hidden; workspace export is reachable and invokable from the
keyboard. It also proves citation activation focuses the cited passage without changing the URL or
history, and that Back, Forward, and reload restore the correct section.

## Accepted limits

- No manual screen-reader exercise has been performed. Axe checks the accessibility tree and names,
  but it does not prove that a workflow is understandable in NVDA, JAWS, or VoiceOver.
- The `.js-plotly-plot` subtree is excluded from axe. Plotly generates a large, dynamic SVG/canvas
  tree whose internal contrast and announcements need a chart-specific manual review. The chart
  title, description, surrounding headings, deterministic tables, and all non-Plotly contrast stay
  in scope. This is not a claim that Plotly figures pass contrast.
- The automated keyboard run uses desktop Chromium. Mobile assistive technology, browser zoom,
  high-contrast mode, and 400% reflow still require manual testing.

There are no accepted axe violations. Any future exception must identify the rule, affected state,
user impact, owner, expiry, and linked issue rather than disabling the rule globally.
