"""Fail closed when the CI controller still owns live worker leases."""

import json

from app.ci_database import get_controller_drain_status, init_db


def main() -> int:
    init_db()
    status = get_controller_drain_status()
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0 if status["safe_to_restart"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
