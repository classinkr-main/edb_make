#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys


ITERATIONS = 210_000


def build_verifier(pin: str, *, salt: bytes | None = None) -> str:
    normalized = str(pin or "").strip()
    if not normalized.isascii() or not normalized.isdigit() or not 4 <= len(normalized) <= 12:
        raise ValueError("PIN은 ASCII 숫자 4~12자리여야 합니다.")
    actual_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized.encode("ascii"),
        actual_salt,
        ITERATIONS,
    )
    return f"pbkdf2_sha256${ITERATIONS}${actual_salt.hex()}${digest.hex()}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ClassIn EDB 자동 업데이트 PIN의 PBKDF2 검증값을 생성합니다.",
    )
    parser.add_argument(
        "--pin-env",
        default="",
        help="대화형 입력 대신 PIN을 읽을 환경 변수 이름",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pin = os.environ.get(args.pin_env, "") if args.pin_env else getpass.getpass("업데이트 PIN (숫자 4~12자리): ")
    try:
        print(build_verifier(pin))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
