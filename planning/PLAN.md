# Simulated Trading — AI Trading Workstation

## Project Specification

_This is the shared contract for all agents building this project. Sections 1–12 are the spec. Appendix A records the design decisions that shaped this revision and why, for traceability._

## 1. Vision

Simulated Trading is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single start script (or `docker compose up`). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger version of that ticker's chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog. Fractional shares supported.
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time (accumulated on the frontend since page load)
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change vs. average cost
- **View trade history** — an append-only blotter of executed trades
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat
- **Reset the simulation** — a control that restores the starting state ($10,000 cash, default watchlist, no positions, cleared history) without touching the Docker volume

### Session-Scoped Charts

All time-series visuals — watchlist sparklines, the main chart, and the P&L chart — are accumulated on the frontend from the SSE stream starting at page load. There is no historical price backfill: on a fresh load or after a refresh they start empty and fill in as data streams. This is deliberate and keeps all three visuals consistent (see Appendix A, D2).

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme

| Token | Hex | Role |
|---|---|---|
| Blue Primary | `#209dd7` | Interactive elements, links, selected watchlist row, primary chart line |
| Accent Yellow | `#ecad0a` | Highlights, warnings, alerts, "reconnecting" connection state |
| Purple Secondary | `#753991` | Submit / confirm buttons (Buy, Sell, Send) |
| Green / Red | (agent's choice, semantic) | Price ticks and P&L — distinct from the accent palette above |

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (bind-mounted at /app/db)      │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `/app/db/simulated_trading.db`, bind-mounted to `./db/` for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided
- **Charting**: A single library — **Recharts** — covers all four visuals (sparkline, main chart, P&L line, treemap heatmap)

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |
| One chart library (Recharts) | Sparkline + line + treemap from one dependency and one mental model |
| Session-scoped charts | No historical price endpoint or storage; all series accumulate client-side from SSE |

---

## 4. Directory Structure

```
simulated_trading/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── db/                   # schema.sql + seed logic (definitions only, not the live file)
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Runtime bind-mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; simulated_trading.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper (equivalent to the start scripts)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains the schema SQL and seed logic — *definitions only*. The backend lazily initializes the database on first request, creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime bind-mount point. The live SQLite file (`db/simulated_trading.db`) is created here by the backend and persists across container restarts. The two `db/` directories never overlap: one holds definitions in the repo, the other holds the running database.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (`docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains the blessed start/stop entry points. `docker-compose.yml` is an optional equivalent for users who prefer compose.

---

## 5. Environment Variables

```bash
# OpenRouter API key for LLM chat functionality.
# Required UNLESS LLM_MOCK=true.
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data.
# If not set, the built-in market simulator is used (recommended for most users).
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing / no-key dev).
LLM_MOCK=false
```

These three variables are the complete set. The committed `.env.example` mirrors this block with empty/placeholder values.

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses the Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses and does **not** require `OPENROUTER_API_KEY`
- If `LLM_MOCK` is not `true` and `OPENROUTER_API_KEY` is missing/empty → the backend still boots and serves market data, portfolio, and watchlist normally, but `POST /api/chat` returns `503` with a clear message ("LLM not configured: set OPENROUTER_API_KEY or LLM_MOCK=true"). The frontend shows the chat panel in a disabled state with that reason.
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface (`get_prices(tickers) -> dict[ticker, PriceTick]` plus a `start()` background loop). The backend selects which to use based on `MASSIVE_API_KEY`. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using independent per-ticker geometric Brownian motion (GBM) with configurable drift and volatility
- Updates at ~500ms intervals
- **Seed prices**: the 10 default tickers use a curated table of realistic values (e.g. AAPL ~$190, GOOGL ~$175). Any *other* ticker added later gets a seed price derived deterministically from its symbol (stable hash → a plausible $15–$600), drift 0, and ~30% annualized volatility.
- **Optional / stretch** (not required for a passing build): correlated moves across related tickers, and occasional random "events" (sudden 2–5% moves) for drama. Core behavior is independent per-ticker GBM.
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all watched tickers on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2–15 seconds depending on tier
- Parses the REST response into the same `PriceTick` format as the simulator
- **Unknown symbol**: if Massive returns no data for a ticker, `POST /api/watchlist` rejects it with `400 unknown_ticker`; a ticker that later stops resolving is kept but streams its last known price

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to a process-global in-memory price cache
- The cache holds, per ticker: latest price, the price emitted on the previous tick, and a timestamp
- SSE streams read from this cache and push updates to connected clients
- The cache is process-global, so multiple browser tabs / SSE connections share it with no extra work

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses the native `EventSource` API
- Server pushes the latest cache value for every known ticker at a regular cadence (~500ms). If a price has not changed since the previous tick (common in Massive free tier), it is still re-emitted with `direction: "flat"`.
- Each SSE event is JSON: `{ "ticker", "price", "previous_price", "timestamp", "direction" }` where `direction ∈ {"up","down","flat"}` and `previous_price` is the value emitted on the prior tick for that ticker
- `timestamp` is ISO 8601 UTC with a `Z` suffix
- Client handles reconnection automatically (`EventSource` has built-in retry)

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh volumes start with a clean, seeded database automatically

The database opens in **WAL mode** so concurrent SSE readers and API reads don't block; writes (trades) are short and serialized.

### Conventions

- All `id` columns are UUID strings (`TEXT PRIMARY KEY`).
- All timestamps are `TEXT`, ISO 8601 UTC with a `Z` suffix.
- All tables carry a `user_id TEXT DEFAULT 'default'`. Hardcoded to `"default"` for now (single-user); present so a future multi-user mode needs no schema migration.

### Schema

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT
- UNIQUE `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT
- UNIQUE `(user_id, ticker)`
- A sell that brings `quantity` to `0` (within a small epsilon) **deletes the row**.

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL (fill price)
- `executed_at` TEXT

**chat_messages** — Conversation history with the LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT (the `message` text)
- `actions` TEXT (JSON; see shape below; `null` for user rows)
- `created_at` TEXT

`chat_messages.actions` JSON shape (assistant rows only):

```json
{
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10, "status": "filled", "price": 190.12, "error": null}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add", "status": "applied", "error": null}
  ]
}
```

`status` for trades is `"filled"` or `"rejected"`; for watchlist changes `"applied"`, `"noop"` (duplicate add / missing remove), or `"rejected"`. `error` is a machine code (see §8) or `null`.

### Portfolio state is stored explicitly

`positions` and `users_profile.cash_balance` are the source of truth for the portfolio and are updated transactionally on every trade (manual or LLM). The `trades` log is an independent append-only audit trail. A backend unit test recomputes holdings and cash purely from `trades` and asserts they match the stored `positions` / `cash_balance`, guarding against drift.

### No portfolio-value history table

There is no `portfolio_snapshots` table and no history endpoint. The P&L chart is accumulated on the frontend from the SSE stream, exactly like the sparklines (see Appendix A, D2).

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates (see §6 for event shape) |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Positions (with live price + unrealized P&L), cash balance, total value |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/trades` | Trade history (append-only blotter), newest first |
| POST | `/api/portfolio/reset` | Restore starting state: cash → 10000, default watchlist, no positions, cleared trades + chat |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers (symbol + `added_at`); prices come from the SSE stream |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message; receive complete JSON response (message + executed actions + post-trade portfolio) |
| GET | `/api/chat/history` | Recent conversation, oldest first, for repopulating the panel on reload |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (process + DB + market-feed freshness) |

### Request / Response Contracts

**`POST /api/portfolio/trade`**

Request: `{ "ticker": "AAPL", "quantity": 10, "side": "buy" }` — `quantity > 0`, fractional allowed (min `0.0001`), `side ∈ {"buy","sell"}`.

Success `200`:
```json
{
  "trade": {"id": "…", "ticker": "AAPL", "side": "buy", "quantity": 10, "price": 190.12, "executed_at": "…Z"},
  "cash_balance": 8098.80,
  "position": {"ticker": "AAPL", "quantity": 10, "avg_cost": 190.12}
}
```
`position` is `null` when the trade closed the position.

Error `400`:
```json
{ "error": { "code": "insufficient_cash", "message": "…" } }
```
Codes: `insufficient_cash`, `insufficient_shares` (includes any attempt to sell more than held — no shorting), `unknown_ticker`, `invalid_quantity`.

**`GET /api/health`** → `200`:
```json
{ "status": "ok", "db": "ok", "market_feed": { "source": "simulator", "last_update_age_ms": 320 } }
```
Returns `503` if the DB is unreachable or the feed's last update is older than 10s.

**Watchlist edge cases**: adding a ticker already present → `200` no-op. Removing a ticker not present → `404`. Removing a ticker you hold a position in is allowed; the position remains and its price keeps streaming (all held tickers stay "known" to the price cache regardless of watchlist membership). Soft cap: 30 tickers; `POST` beyond that → `400 watchlist_full`.

---

## 9. LLM Integration

When writing code to call the LLM, use the **cerebras** skill: LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider, requesting Structured Outputs. `OPENROUTER_API_KEY` is in `.env`.

### How It Works (single pass)

When the user sends a chat message, the backend:

1. Loads the current portfolio context (cash, positions with P&L, watchlist with live prices, total value)
2. Loads recent conversation history from `chat_messages` — up to the last 20 messages or ~3000 tokens, whichever is smaller
3. Constructs a prompt: system message + portfolio context + history + the new user message
4. Calls the LLM via LiteLLM → OpenRouter with Structured Outputs (the cerebras skill)
5. Parses the structured JSON response (`message`, `trades`, `watchlist_changes`)
6. **Then** auto-executes each trade and watchlist change, through the exact same validation as the manual endpoints
7. Persists the user message and the assistant message (with the augmented `actions` JSON) to `chat_messages`
8. Returns one JSON payload to the frontend (no token streaming — Cerebras is fast enough that a loading indicator suffices)

The LLM writes `message` *before* execution, so its prose does not reference exact fill prices or failures. Execution outcomes are returned in a separate `actions` block and rendered by the frontend as inline confirmation / error chips beneath the message. If the user needs the assistant to comment on an outcome, that happens naturally on their next turn (the results are in the history).

### What the LLM Returns (Structured Output schema)

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): conversational text shown to the user
- `trades` (optional): trades to auto-execute; `side ∈ {"buy","sell"}`, `quantity > 0`
- `watchlist_changes` (optional): `action ∈ {"add","remove"}`

### What `POST /api/chat` Returns

```json
{
  "message": "…",
  "actions": {
    "trades": [
      {"ticker": "AAPL", "side": "buy", "quantity": 10, "status": "filled", "price": 190.12, "error": null}
    ],
    "watchlist_changes": [
      {"ticker": "PYPL", "action": "add", "status": "applied", "error": null}
    ]
  },
  "portfolio": { "…": "same shape as GET /api/portfolio, reflecting post-execution state" }
}
```

`actions` is also what gets persisted in `chat_messages.actions`.

### Auto-Execution

Trades and watchlist changes specified by the LLM execute automatically — no confirmation dialog. Deliberate:
- Simulated environment, fake money, zero stakes
- Impressive, fluid demo experience
- Demonstrates agentic AI capabilities — the theme of the course

A failed trade or watchlist change is reported in `actions[*].status` / `error`; it never raises to the user as an HTTP error.

### System Prompt Guidance

Prompt the LLM as "Simulated Trading, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter — for fast, free, reproducible E2E tests, no-key development, and CI. Mock responses still flow through real trade/watchlist execution and validation.

---

## 10. Frontend Design

### Layout

A single-page application with a dense, terminal-inspired layout. Component architecture is up to the Frontend Engineer, but the UI must include:

- **Watchlist panel** — grid/table of watched tickers: symbol, current price (flashing green/red on change), change % **since page load**, and a sparkline mini-chart (accumulated from SSE). The change % baseline is the first price seen this session — labeled accordingly (not "daily"), since the simulator has no real previous close.
- **Main chart area** — a larger rendering of the selected ticker's session price series (the same client-side buffer that feeds its sparkline, drawn bigger). Clicking a watchlist row selects it.
- **Portfolio heatmap** — Recharts treemap; each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — Recharts line chart of total portfolio value over time, accumulated on the frontend from the SSE stream since page load
- **Positions table** — ticker, quantity, avg cost, current price, unrealized P&L, % change vs. avg cost
- **Trade history / blotter** — executed trades from `GET /api/portfolio/trades`
- **Trade bar** — ticker field, quantity field (fractional allowed), Buy and Sell buttons. Market orders, instant fill, no dialog.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling history, loading indicator. Trade executions and watchlist changes shown inline as confirmation / error chips from the response `actions`. Repopulated on reload via `GET /api/chat/history`. Shows a disabled state with reason when the LLM is not configured.
- **Header** — portfolio total value (live), connection status dot, cash balance
- **Reset control** — small, out-of-the-way button (e.g. in a header menu) calling `POST /api/portfolio/reset`, with a brief "are you sure?" since it clears history

### Frontend State Model

- **Prices**: `EventSource` on `/api/stream/prices`. The client keeps a per-ticker rolling buffer (for sparklines + main chart) and the latest price map.
- **Portfolio**: fetched from `GET /api/portfolio` on load, re-fetched after every trade (manual or via chat, using the `portfolio` block in the chat response) and on a slow interval (~10s) as a safety net. Header total value is computed client-side between fetches from the latest price map × held quantities.
- **Watchlist**: `GET /api/watchlist` on load and after add/remove.
- **Chat**: `GET /api/chat/history` on load; append on each exchange.

### Technical Notes

- `output: 'export'` rules out Next.js API routes, SSR/ISR, middleware, and the Image Optimization API. Everything dynamic comes from `/api/*`.
- Recharts for **all** charts (sparkline, main, P&L line, treemap) — one dependency.
- Price flash: on a new price, briefly add a CSS class with a background-color transition, then remove it.
- All API calls are same-origin (`/api/*`) — no CORS config.
- Tailwind CSS with a custom dark theme.

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: node:20-slim
  - Copy frontend/
  - npm ci && npm run build   (produces static export in frontend/out)

Stage 2: python:3.12-slim
  - Install uv (pinned version)
  - Copy backend/
  - uv sync --frozen          (install from lockfile)
  - Copy the frontend export into a static/ directory served by FastAPI
  - EXPOSE 8000
  - CMD: uvicorn app:app --host 0.0.0.0 --port 8000
```

Pin: Node 20, Python 3.12, Next.js major (14.x), `uv` version, and base-image digests, for reproducible builds.

FastAPI serves the static frontend and all API routes on port 8000.

### Persistence (bind mount)

```bash
docker run --rm -p 8000:8000 --env-file .env \
  -v "$(pwd)/db:/app/db" \
  simulated-trading
```

The project's `./db` directory maps to `/app/db`; the backend writes `simulated_trading.db` there. Using a bind mount (not a named volume) means students can see and delete their database file directly. `docker-compose.yml` expresses the same mount.

### Start/Stop Scripts (the blessed entry points)

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the image if missing (or if `--build` is passed)
- Checks that port 8000 is free; if not, exits with a readable message
- Runs the container with the bind mount, port mapping, and `--env-file .env`
- Prints the URL; optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does **not** delete `./db` (data persists). Accepts `--wipe` to also clear `./db/simulated_trading.db` for a full reset.

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents.

All scripts are idempotent — safe to run repeatedly.

### Optional Cloud Deployment

The container can deploy to AWS App Runner, Render, or any container platform. A Terraform config for App Runner may live in `deploy/` as a stretch goal; not part of the core build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator produces valid prices, GBM math is correct (independent per-ticker), Massive response parsing works, both implementations satisfy the abstract interface, deterministically-seeded price for a novel ticker
- Portfolio: trade execution, P&L math, `avg_cost` on repeated buys, sell-to-zero deletes the row, no-shorting rejection, insufficient-cash rejection, sell-at-a-loss
- Consistency: holdings + cash recomputed from the `trades` log equal the stored `positions` / `cash_balance`
- Reset: `POST /api/portfolio/reset` restores exact seed state
- LLM: structured-output parsing for all valid schemas, graceful handling of malformed model output, trade validation inside the chat flow, `503` when the key is missing and mock is off
- API routes: status codes, response shapes, error-code contract

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash triggers on price change
- Watchlist add/remove
- Portfolio display calculations
- Chat message rendering, loading state, and inline action chips
- Disabled chat panel when LLM not configured

### E2E Tests (in `test/`)

**Infrastructure**: `docker-compose.test.yml` in `test/` spins up the app container plus a Playwright container. Keeps browser deps out of the production image.

**Environment**: `LLM_MOCK=true`. Assertions are on structure and behavior (a price element updates, series length grows, chips appear) — never exact price values, since the simulator is stochastic.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio + header update
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with P&L colors; P&L chart accumulates points as prices stream
- AI chat (mocked): send a message, receive a response, trade-execution chip appears inline
- Chat history: reload the page, prior messages reappear via `GET /api/chat/history`
- Reset: `POST /api/portfolio/reset` returns the UI to the seeded starting state
- SSE resilience: force a disconnect (Playwright route abort or offline mode on `/api/stream/prices`), verify the status dot goes yellow/red then green and streaming resumes

---

## Appendix A — Design Decisions From Review

These record choices made after the documentation review, so later agents don't re-open settled questions.

| # | Topic | Decision | Rationale |
|---|---|---|---|
| D1 | Portfolio state storage | **Keep explicit `positions` + `users_profile.cash_balance`**, updated transactionally per trade; `trades` is a parallel audit log. Add a consistency unit test that recomputes from `trades`. | Conventional and matches the schema; the drift risk is contained by the test rather than by removing tables. |
| D2 | P&L / value history | **Dropped** the 30s background task, the `portfolio_snapshots` table, and `GET /api/portfolio/history`. The P&L chart accumulates client-side from SSE, like the sparklines. | Removes a background task and an unbounded table; makes all three time-series visuals behave consistently. Trade-off: no history across refresh/restart. |
| D3 | Launch mechanism | **Keep the four per-OS scripts** as the blessed path; `docker-compose.yml` stays as an optional equivalent; `docker-compose.test.yml` stays separate. | Course materials may reference the scripts by name; compose remains for those who prefer it. |
| D4 | Reset | **Added `POST /api/portfolio/reset`** plus a guarded UI control and a `--wipe` flag on the stop script. | Repeatable classroom demos without deleting the bind-mounted DB. |
| D5 | Charting library | **Recharts only**, for sparkline + main chart + P&L line + treemap. | One dependency covers all four; avoids pulling in a second chart lib for the treemap. |
| D6 | Main chart | It is the selected ticker's **session series drawn larger** — the same client buffer that feeds the sparkline. No separate historical/richer chart. | No historical price source exists; avoids implying scope that can't be built. |
| D7 | `daily change %` | Computed **client-side as "since page load"** from the first tick seen; labeled as such, not "daily". | The simulator has no real previous close; no backend reference-price tracking needed. |
| D8 | Chat trade results | **Single LLM pass.** Execution happens after; results returned in a separate `actions` block + a post-trade `portfolio` snapshot; frontend renders chips. | Simplest; consistent with "Cerebras is fast"; the model can comment on results on the next turn. |
| D9 | New endpoints | Added `GET /api/chat/history` and `GET /api/portfolio/trades`. | Chat panel must survive reload; the `trades` table should be visible as a blotter. |
| D10 | SSE payload | Kept `previous_price` + `direction` in the event (not trimmed). | Cost is negligible and it makes first paint correct without the client having to see two ticks. |
| D11 | IDs / `user_id` | Kept UUID `TEXT` primary keys and the `user_id` column defaulting to `"default"`. | Already threaded through the schema and examples; `user_id` is cheap future-proofing the course wants to teach. |
| D12 | Simulator correlation & events | **Optional / stretch.** Core is independent per-ticker GBM. | Far easier to implement and unit-test; the effect is polish. |
| D13 | `db/` directories | `backend/db/` holds `schema.sql` + seed logic (definitions); top-level `db/` is the runtime **bind mount**. `docker run` example and compose both use the bind mount. | Removes the named-volume vs bind-mount inconsistency; students can see their DB file. |
| D14 | Test-determinism flag | No new env var. `.env` keeps exactly `OPENROUTER_API_KEY`, `MASSIVE_API_KEY`, `LLM_MOCK`. E2E assertions avoid exact price values instead. | `.env` is fixed; asserting on behavior rather than values is the more robust fix anyway. |
