"""Server entry point.

    python main.py

Starts the API server on SHORTS_HOST:SHORTS_PORT (default 0.0.0.0:8100).
"""
import sys

# Windows uses 'charmap' by default, which can't encode Unicode characters
# like →. Reconfigure stdout/stderr to UTF-8 so output works on all platforms.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn

from shorts_generator.config import SHORTS_HOST, SHORTS_PORT
from shorts_generator.server.app import create_app


def main() -> int:
    uvicorn.run(create_app(), host=SHORTS_HOST, port=SHORTS_PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
