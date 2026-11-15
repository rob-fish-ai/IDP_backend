#!/usr/bin/env python
"""Check what this host needs before its first case.

    .venv/bin/python scripts/preflight.py            # all checks
    .venv/bin/python scripts/preflight.py --offline  # skip the network ones

Reads .env like the engine does. Exits 1 when any check fails.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.core.dependencies import get_settings
    from app.services.preflight import run_all
    settings = get_settings()
    checks = run_all(settings, network="--offline" not in sys.argv)
    width = max(len(name) for name, _, _ in checks)
    for name, status, detail in checks:
        print(f"{status.upper():4}  {name:<{width}}  {detail}")
    failed = sum(1 for _, s, _ in checks if s == "fail")
    print(f"\n{failed} failed, {sum(1 for _, s, _ in checks if s == 'warn')} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
