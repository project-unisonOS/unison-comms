from __future__ import annotations

import os
import sys

import uvicorn

from main import app


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def main() -> int:
    host = os.getenv("COMMS_HOST", "127.0.0.1")
    port = int(os.getenv("COMMS_PORT", "8080"))
    unsafe = (os.getenv("COMMS_UNSAFE_ALLOW_NONLOCAL") or "").lower() in {"1", "true", "yes", "on"}
    if not unsafe and not _is_loopback(host):
        raise RuntimeError("Refusing to bind non-loopback without COMMS_UNSAFE_ALLOW_NONLOCAL=true")
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)

