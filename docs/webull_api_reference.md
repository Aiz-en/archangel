# Webull Open API Reference

Research gathered from official Webull documentation.

## Getting Started

1. Apply for API access through the Webull website (1-2 business day review)
2. Once approved, generate **App Key** and **App Secret** from the Webull portal
3. Alternatively, use shared **test account credentials** to start immediately without approval

## Market Data Access (Important Gotcha)

**OpenAPI market data is paid-only.** Discovered the hard way: calling `data_client.market_data.get_snapshot("AAPL", Category.US_STOCK)` on a fresh, valid API key returns:

> HTTP 401 Unauthorized — "Insufficient permission, please subscribe to stock quotes."

Trade endpoints (`account_v2.get_account_list`, etc.) work without a market-data subscription. **Quote endpoints** (`get_snapshot`, `get_quotes`, `get_history_bar`, snapshots, depth, ticks, EOD bars) all require a separate **OpenAPI Advanced Quotes** subscription.

### How to subscribe (paid path)
1. Log in at https://www.webullapp.com/quote (Webull Technology site, *not* the developer portal)
2. Click avatar (upper-right) → **Advanced Quotes**
3. Open **OpenAPI Advanced Quotes**, pick a service, complete checkout

**Critical:** Advanced Quotes purchased through the Webull mobile app or desktop (QT) **do not transfer** to the OpenAPI. The OpenAPI subscription is separate.

### No public free tier
The docs do not list a free or delayed-quotes tier for OpenAPI. There is a sandbox/test environment at `us-openapi-alb.uat.webullbroker.com` with shared test credentials, but the data is canned/synthetic — useful only for testing client-side code (request signing, response parsing), not for any real bot logic.

### Pricing
Not posted publicly. Email `api@webull-us.com` to request OpenAPI Advanced Quotes pricing.

### Archangel decision (current)
Deferred. Using `yfinance` (Yahoo Finance) for free historical and intraday OHLCV during the paper-trading build (see `code_base/hello_quote.py`). Will revisit when we're ready for live trading and need real-time streaming, or if a strategy needs tick-level data.

Sources:
- https://developer.webull.com/apis/docs/market-data-api/subscribe-quotes/
- https://developer.webull.com/apis/docs/faq/

## SDK Installation

```bash
pip3 install --upgrade webull-openapi-python-sdk
```

### SDK Modules
- `webull-python-sdk-core` — Core functionality
- `webull-python-sdk-trade` — Trading operations
- `webull-python-sdk-quotes-core` — Market data handling
- `webull-python-sdk-trade-events-core` — Order event management
- `webull-python-sdk-mdata` — Market data services

### Basic Usage

```python
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

api_client = ApiClient("<your_app_key>", "<your_app_secret>", "us")
trade_client = TradeClient(api_client)
res = trade_client.account_v2.get_account_list()
print(res.json())
```

## Protocols

| Protocol | Use Case |
|----------|----------|
| HTTP (REST) | Orders, account info, historical data, snapshots |
| MQTT | Real-time market data streaming |
| gRPC | Real-time trade event subscriptions (order fills, status changes) |

## Supported Order Types

- **Market** (stocks, futures, crypto)
- **Limit** (all products)
- **Stop Loss** and **Stop-Loss-Limit**
- **Trailing Stop** (stocks, futures)
- **Combo orders**: OTO / OCO / OTOCO (stocks, options)
- **Algo orders**: TWAP / VWAP / POV (stocks only)
- Extended/overnight hours trading (stocks)

Note: Market orders for options are NOT supported.

## Supported Products

- **Stocks**: US equities (NYSE, NASDAQ), fractional shares, short selling
- **Options**: Single and multi-leg strategies
- **Futures**: Index, interest rate, currency, agriculture, metals, energy, crypto
- **Crypto**: 70+ assets, 24/7 spot trading
- **Event Contracts**: Binary outcome contracts

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| Order placement | 600 per 60 seconds |
| Order preview | 150 per 10 seconds |
| Account balance/positions | 2 per 2 seconds |
| Instrument queries | 10 per 30 seconds |

## API Endpoints (Core)

- Place / replace / cancel orders
- Batch order placement
- Order history and open orders
- Account balance and positions
- Real-time trade event subscription (gRPC)

## Paper Trading

The API docs do not explicitly expose a paper/sandbox mode. Options:
1. Use Webull's **shared test account** credentials for experimentation
2. Build a **local paper trading simulation** — consume real market data, simulate fills locally

Option 2 is recommended for faster iteration and full control.

## Official Resources

- API Docs: https://developer.webull.com/apis/docs/
- Trading API: https://developer.webull.com/apis/docs/trade-api/overview
- Getting Started: https://developer.webull.com/apis/docs/getting-started
- Python SDK: https://github.com/webull-inc/openapi-python-sdk
- Quick Start: https://developer.webull.com/api-doc/prepare/start/
