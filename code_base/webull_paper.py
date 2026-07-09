"""Webull paper-trading broker (unofficial).

Drives a Webull paper trading account via the unofficial tedchou12/webull
library's `paper_webull` class — the same library that powers the
top-gainers scanner. Here we use its trade-execution side.

SECURITY MODEL — read before running:

1. Uses your Webull username + password (NOT scoped API keys, unlike the
   official OpenAPI). Treat the credentials accordingly.
2. Run this against an ISOLATED Webull account that has NO funded live
   side. If credentials leak, that account is the entire blast radius.
3. The auth token cache is written to ~/.archangel/webull_paper_token.json
   — outside the repo. The cached token is equivalent to a logged-in
   session; protect the file like a password.
4. The library writes did.bin (device ID) to the working directory. Both
   filenames are gitignored defensively.

Auth flow:
- First run: prompts for the MFA code Webull emails/SMSes you, then
  caches a session token good for ~30 days.
- Subsequent runs: refresh_login() reads the cached token, no MFA needed.

CURRENT SCOPE — read-only. This module connects, reads the paper account
state, and lists positions. ORDER PLACEMENT IS NOT WIRED UP YET — that
arrives with the live polling runner in the next iteration.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv


TOKEN_DIR = Path.home() / ".archangel"
TOKEN_PATH = TOKEN_DIR / "webull_paper_token.json"


class WebullPaperBroker:
    def __init__(self, env_path: Optional[Path] = None) -> None:
        from webull import paper_webull

        self._wb = paper_webull()

        env_path = env_path or Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path)
        self._username = os.getenv("WEBULL_PAPER_USERNAME")
        self._password = os.getenv("WEBULL_PAPER_PASSWORD")
        if not self._username or not self._password:
            raise RuntimeError(
                "Missing WEBULL_PAPER_USERNAME / WEBULL_PAPER_PASSWORD in .env. "
                "Set them to the credentials of your isolated Webull paper account "
                "(see code_base/webull_paper.py docstring for security guidance)."
            )

    def connect(self) -> None:
        """Authenticate. Uses cached token if present; runs MFA flow on first call."""
        TOKEN_DIR.mkdir(exist_ok=True)

        if TOKEN_PATH.exists():
            try:
                self._wb.refresh_login(save_token=True, token_path=str(TOKEN_PATH))
                if self._wb.is_logged_in():
                    return
            except Exception as exc:
                print(
                    f"Cached token refresh failed ({type(exc).__name__}); "
                    f"falling back to fresh login.",
                    file=sys.stderr,
                )

        print("First-time login (or expired token).")
        print("Webull will send an MFA code to the email/phone on the account.")
        self._wb.get_mfa(self._username)
        mfa = input("Enter the MFA code: ").strip()

        result = self._wb.login(
            username=self._username,
            password=self._password,
            device_name="archangel-bot",
            mfa=mfa,
            save_token=True,
            token_path=str(TOKEN_PATH),
        )
        if not self._wb.is_logged_in():
            # Don't print the result dict — it can include sensitive auth fields.
            raise RuntimeError(
                "Login failed. Check the credentials in .env and re-run. "
                "(Response intentionally not printed; check Webull app for any "
                "security alerts.)"
            )
        print("Logged in. Token cached at:", TOKEN_PATH)

    def get_account_raw(self) -> dict[str, Any]:
        return self._wb.get_account()

    def get_positions_raw(self) -> list[dict[str, Any]]:
        result = self._wb.get_positions()
        return result if isinstance(result, list) else []


def _smoke_test() -> int:
    try:
        broker = WebullPaperBroker()
    except RuntimeError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 1

    print("Connecting to Webull paper account...")
    broker.connect()
    print("Connected.\n")

    print("=== Raw account response ===")
    account = broker.get_account_raw()
    if isinstance(account, dict):
        # Print top-level keys + values, but redact anything that looks
        # token-like so we don't accidentally leak session state to logs.
        for k, v in account.items():
            if any(s in k.lower() for s in ("token", "auth", "secret", "key")):
                print(f"  {k}: <redacted>")
            else:
                print(f"  {k}: {v}")
    else:
        print(account)

    print("\n=== Raw positions response ===")
    positions = broker.get_positions_raw()
    print(f"({len(positions)} position(s))")
    for p in positions:
        print(f"  {p}")

    print("\nNext step: review the field names above, then we can build typed")
    print("wrappers (AccountSnapshot, Position) and the order-placement API.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
