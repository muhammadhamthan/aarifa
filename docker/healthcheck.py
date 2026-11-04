"""Container health check (Docker HEALTHCHECK and the ECS api container health check).

api: GET http://127.0.0.1:$KX_API_PORT$KX_HEALTH_PATH must return 2xx.
worker / voice / migrate: no HTTP port; the process being alive is the signal (ECS restarts on exit).
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if os.environ.get("KX_PROCESS", "api") != "api":
        return 0
    port = os.environ.get("KX_API_PORT", "8000")
    path = os.environ.get("KX_HEALTH_PATH", "/healthz")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:  # noqa: S310
            return 0 if 200 <= resp.status < 300 else 1
    except (urllib.error.URLError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
