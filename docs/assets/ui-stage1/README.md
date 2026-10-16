# Stage 1 workbench screenshots

These screenshots were captured from the production React bundle talking to the real FastAPI
application with the repository's deterministic scripted provider. They are not mockups. The
capture run is `E2E_CAPTURE=1 E2E_CAPTURE_DIR=docs/assets/ui-stage1 npm run test:e2e:real` (use the
equivalent environment-variable syntax for the current shell).

The matching pre-redesign captures remain in `docs/assets/demo/`, providing before-and-after pairs
for the overview, schema review, deterministic analysis, SQL evidence, document citations, SQL
approval, hybrid answers, recovery, and phone layout. `10-phone-navigation.png` and
`11-phone-ask-composer.png` additionally show the Stage 1 mobile drawer and compact Ask workflow.

The provider is scripted, but ingestion, profiling, analysis, storage, retrieval, SQL validation,
approval, recovery, API transport, and UI rendering use production application code.
