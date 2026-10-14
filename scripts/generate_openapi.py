"""Generate the committed OpenAPI contract from the FastAPI application."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.offline_execution import ensure_offline_module, invocation_path

if __name__ == "__main__":
    ensure_offline_module("scripts.generate_openapi")

from apps.api.main import create_app  # noqa: E402 - offline boundary must run first

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "openapi" / "openapi.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=invocation_path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
