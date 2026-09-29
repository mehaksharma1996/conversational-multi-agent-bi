"""Generate the committed OpenAPI contract from the FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from apps.api.main import create_app

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "openapi" / "openapi.json"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
