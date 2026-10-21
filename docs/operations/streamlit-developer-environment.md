# Optional Streamlit developer environment

Streamlit is the developer-only compatibility UI kept by
[ADR 0011](../adr/0011-streamlit-disposition.md). React/FastAPI is the product. Since 2026-10-20
Streamlit has its own environment, separate from production:

| | Production (`requirements.txt`, `requirements.lock`) | Streamlit developer environment (`requirements-streamlit.txt`, `requirements-streamlit.lock`) |
|---|---|---|
| Contains | The API, analytics, retrieval, reporting, MCP, and dev tools | The production set plus Streamlit and the packages only it needs (`altair`, `httptools`, `itsdangerous`, `pyarrow`, `pydeck`, `toml`, `watchdog`) |
| Pillow | 13.0.0 (patched) | 12.3.0 (Streamlit requires `pillow<13`) |
| Image | `Dockerfile.api` (`conversational-bi/api:local`), which fails its build if Streamlit is importable | `Dockerfile.streamlit` (`conversational-bi/streamlit:local`), `streamlit` Compose profile only |
| Scanned by the CI `security` job | Yes (`pip-audit --strict`) | No; see below |

## Known exposure (read before using it)

**This environment is not vulnerability-free.** Every released Streamlit (1.66.0 and earlier, as of
2026-10-20) requires `pillow<13`, and Pillow 12.3.0 has nine published advisories fixed only in
13.0.0: CVE-2026-112395, -112397, -112400, -112403 (critical), -112404, -112895, -112896, -112897,
and -112899. Upstream support is tracked in streamlit/streamlit#17586 and #17587.

The exposure is in image decoding. Use the environment only:

- for trusted local development, on loopback (the Compose profile binds `127.0.0.1` only);
- with files you trust, never with images or documents from untrusted sources;
- never in production, and never installed into the main virtual environment or the API image.

When a Streamlit release accepts Pillow 13 or later, regenerate `requirements-streamlit.lock` with it,
re-run the checks below, and remove this exposure section.

## Install and run (local)

Use a separate virtual environment, so its Pillow pin never touches the production environment:

```powershell
python -m venv .venv-streamlit
.venv-streamlit\Scripts\Activate.ps1
python -m pip install -r requirements-streamlit.lock
python -m pip check
python -m streamlit run app.py --server.address=127.0.0.1
```

Run its UI tests from that environment (in the production environment they are skipped):

```powershell
python -m scripts.run_pytest --target tests/test_app.py --target tests/test_chat_panel.py --target tests/test_upload_panel.py
```

## Run (container)

```powershell
docker compose --profile streamlit up -d --wait streamlit   # http://127.0.0.1:8501
```

CI's `streamlit-dev` job installs this lock, runs `pip check` and the UI tests, and the `containers`
job builds and starts the profile.
