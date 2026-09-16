"""Prompt locally for a demo password and print only its salted hash to stdout."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trial_demo import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) != 1:
        print("Run without arguments; the password is requested privately.", file=sys.stderr)
        return 2
    try:
        password = getpass.getpass("Demo password (at least 8 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 2
        encoded = hash_password(password)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 2
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
