"""Run with ``python -m apps.mcp``."""

from config.runtime import load_runtime_dotenv


def main() -> None:
    """Load runtime configuration before importing the stdio server."""
    load_runtime_dotenv()

    from apps.mcp.server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
