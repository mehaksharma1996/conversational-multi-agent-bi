# Dotenv-isolated offline tooling

Offline Python tools that import the application or ChromaDB run their sensitive imports in a
child process whose working directory is a newly created empty temporary directory. The launcher
sets `PYTHON_DOTENV_DISABLED=1`, prepends the repository root to `PYTHONPATH`, preserves other
explicit environment variables, and passes commands as argument lists with `shell=False`.

This two-part boundary is necessary because the application's `load_dotenv()` honors
`PYTHON_DOTENV_DISABLED`, while ChromaDB 1.5.x uses Pydantic Settings to look for `.env` relative to
the current working directory and does not honor that flag.

Use these protected entry points:

```powershell
python -m scripts.run_pytest
python -m scripts.run_pytest --target tests/test_settings.py -q
python -m scripts.run_evaluations
python -m scripts.generate_openapi
python -m scripts.run_benchmarks --quick
python -m tests.e2e.workflow_baseline_runner
python -m scripts.check_all
```

`scripts.check_all` runs its pytest and deterministic-evaluation children through the same
boundary. OpenAPI generation, evaluations, benchmarks, and the workflow-baseline CLI re-exec
themselves before importing application or ChromaDB modules. Relative input and output paths on
those CLIs are resolved against the directory from which the command was invoked.

An arbitrary `python -m pytest` command is **not** globally protected. Use
`python -m scripts.run_pytest`; pass each focused file or directory with `--target`. Likewise,
importing these modules into an already-running Python process does not retroactively isolate that
process. Runtime Streamlit, FastAPI, MCP, and explicitly enabled live-provider commands retain
their existing configuration behavior.

The boundary never opens, copies, renames, or deletes the repository's `.env`. Regression tests
install a pre-import audit hook that rejects any attempted open of a file named `.env`, including
the independent ChromaDB/Pydantic path.
