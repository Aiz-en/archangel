"""First Webull auth smoke test.

Loads credentials from .env, opens an ApiClient, and asks the trade
service for the account list. Success means our App Key/Secret pair
is valid and the SDK can sign requests.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    region = os.getenv("WEBULL_REGION", "us")

    if not app_key or not app_secret:
        print("Missing WEBULL_APP_KEY or WEBULL_APP_SECRET in .env", file=sys.stderr)
        return 1

    api_client = ApiClient(app_key, app_secret, region)
    trade_client = TradeClient(api_client)

    response = trade_client.account_v2.get_account_list()
    print(response.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
