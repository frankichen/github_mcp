"""Fail closed when the CI controller still owns live worker leases."""

import argparse
import json

from app.ci_database import get_controller_drain_status, init_db, set_controller_draining


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private CI controller drain gate")
    parser.add_argument("action", nargs="?", choices=("begin", "status", "resume"), default="begin")
    args = parser.parse_args(argv)

    init_db()
    if args.action == "begin":
        status = set_controller_draining(True)
    elif args.action == "resume":
        status = set_controller_draining(False)
    else:
        status = get_controller_drain_status()

    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    if args.action == "resume":
        return 0
    return 0 if status["draining"] and status["safe_to_restart"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
