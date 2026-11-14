# TradePulse

Real-time monitoring, positions & holdings reconciliation and notifications for the **SkyMatrix OMS**.
A central dashboard controls everything; a Telegram bot (**TradePulse**) delivers updates to groups and
answers commands in groups and private chats.

> **Connected only to Redis.** TradePulse never opens the OMS database or calls the OMS API, so it keeps
> working after the OMS host goes down for the day (OMS hours 08:45–15:50). It never places, modifies or
> cancels orders. Broker access is limited to **read-only positions/holdings**, **one request every 3 s**
> across everyone, and only at **09:00 / 09:05** or when a user explicitly asks.

---

## Architecture

```text
            Redis (shared with the OMS)                                    TradePulse
 ┌───────────────────────────────────────────┐
 │ smx:roster, smx:oms:apikeys  (accounts)   │──read──▶ roster sync (credentials dropped on parse)
 │ smx:session:<acct>           (tokens)     │──read──▶ session health  ·  broker readers (09:00/09:05/user)
 │ smx:oms:cmd:<worker>  EXECUTE/RESUME      │──XREAD─▶ orders (symbol, side, qty, product...)
 │ smx:oms:events  ACCEPTED/STATUS/FILL/...  │──XREAD─▶ lifecycle, fills, workers, OMS book refreshes
 │ smx:oms:paper:pos:<acct>                  │──read──▶ paper books (no broker)
 │ LTP:<exch>:<seg>                          │──read──▶ live prices for unrealised P&L
 │ tradepulse:*   (the ONLY keys written)    │◀─write── ingest queue, live fan-out, slot, health
 └───────────────────────────────────────────┘
                                  │
            fills ──▶ BOOK = broker baseline (09:00 / 09:05 / refresh) ⊕ every fill since   (Decimal, idempotent)
                                  │
   events ─▶ processor (dedupe) ─▶ store ─▶ rules ─▶ outbox ─▶ Telegram groups      SSE ─▶ dashboard
                                                                                     bot (commands, scoped)
```

| Component | Module | Notes |
|---|---|---|
| Redis tail | `sources/redis_tail.py` | plain `XREAD` with saved cursors (no consumer group on OMS keys); events for not-yet-seen orders are parked and retried |
| Book | `book.py`, `reconcile.BookKeeper` | baseline ⊕ fills, average-cost, exact Decimal; recomputed per fill (never double-counts) |
| Reconciler | `reconcile.Reconciler` | 09:00 + 09:05 IST (configurable), monitored accounts; user refresh; mismatch alerts |
| Broker readers | `brokers.py` | Zerodha / Angel / 5paisa positions + holdings only; global Redis slot; no retries; paper from Redis |
| Local data | `localdata.py` | TradePulse's own copy of users, accounts, orders, fills, book - same scoping rules as the OMS |
| Processor / rules / notifier / bot | `processor.py`, `rules.py`, `notify/`, `bot.py` | dedupe, precedence, durable outbox, Telegram commands |

### What TradePulse reads from the OMS's Redis (and why it is enough)

* **Orders** - the OMS dispatches every order to its worker as an `EXECUTE` command on
  `smx:oms:cmd:<worker>` carrying the full order (symbol, side, exchange, token, product, quantity,
  price, trading mode). TradePulse reads those streams without a consumer group.
* **Lifecycle & fills** - every worker report (`ORDER_SUBMITTING`, `ORDER_ACCEPTED`, `ORDER_STATUS`,
  `ORDER_MODIFIED`, `ORDER_UNKNOWN`, `FILL`, `GROUP_REJECTED`, `ALERT`, `HEARTBEAT`) is on `smx:oms:events`.
  Status rules mirror the OMS: terminal states are final; working states only move forward.
* **Accounts** - `smx:roster` (client name, broker) and `smx:oms:apikeys`. The roster also carries
  login secrets for the session generator; TradePulse keeps 4 fields and discards the rest at parse time
  (a test asserts no secret ever reaches TradePulse's database).
* **Free snapshots** - when the OMS itself refreshes a broker book (its 15:40/16:15 daily books or a
  manual refresh), `POSITION_BOOK` / `HOLDINGS` events are used as a newer baseline at no cost.

Not visible through Redis (by design of the OMS): orders the OMS rejects *before* routing to a worker,
and OMS-API-side alerts that are only written to its database.

---

## Positions & holdings reconciliation

| When | What happens | Broker calls |
|---|---|---|
| **09:00 IST** | positions + holdings of every **monitored** account → new baseline; differences from the book carried overnight are recorded | 2 per account, 1 every 3 s |
| between | each **FILL** rebuilds that account's book (baseline ⊕ fills since); emits `POSITION_UPDATED` / `HOLDING_UPDATED` | **none** |
| **09:05 IST** | second snapshot verifies the book; any difference → `RECONCILE_MISMATCH` alert | 2 per account, 1 every 3 s |
| user presses **Refresh** (dashboard) or `/refresh positions ACCOUNT` (Telegram) | one snapshot of one account | 1 - or **"please wait N s"** if the slot is taken |
| OMS publishes `POSITION_BOOK` / `HOLDINGS` | compared and used as baseline | none |

**The 3-second rule** is a Redis slot (`SET tradepulse:broker:slot NX PX 3000`) shared by every
process and user, re-armed *after the broker answers* - so two requests never reach a broker less than
3 s apart even when one is slow (verified end-to-end: minimum gap 3.03 s). Scheduled passes wait their
turn; a user request that finds the slot busy is refused with HTTP 429 + `Retry-After` ("Please wait 2
seconds…"), and the dashboard's Refresh buttons count down. The spacing can be raised, never below 3 s.

Timing: a snapshot is stamped with the moment its request was **sent**; fills strictly after that are
applied on top. A fill inside the same second *after* the request (brokers report second-resolution
fill times) is the one ambiguous case - the 09:05 verification and mismatch alerts exist for it.

---

## Setup

```bash
python -m venv venv && . venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # REDIS_HOST/PORT/PASSWORD/DB (same as the OMS .env), TP_FERNET_KEY, TP_TELEGRAM_BOT_TOKEN
python -m tradepulse.admin import-oms --db /path/to/skymatrix.db   # ONE-TIME: users, passwords, account owners
python -m tradepulse.app                           # http://127.0.0.1:8895
```

The import opens the OMS database read-only, once, and copies usernames, roles, manager links, bcrypt
hashes (OMS passwords keep working) and which user owns which account. Afterwards users are managed on
the **Users** page; re-running the import adds new users and never overwrites existing ones.
Alternatively set `TP_ADMIN_PASSWORD` to create a first administrator.

Separate deployment (see `deploy/`):

```bash
TP_RUN_WORKERS=0 python -m tradepulse.app          # UI / API / SSE / webhooks
python -m tradepulse.worker                        # PRIMARY: Redis tail, reconciler, scheduler, Telegram, processor, notifier
python -m tradepulse.worker --replica              # optional extra processor + notifier
```

TradePulse can run on any host that reaches Redis - it needs no access to the OMS host.

### Try it without the OMS

```bash
python scripts/oms_redis_sim.py --redis redis://127.0.0.1:6379/0 seed          # roster, sessions, keys (fake)
python scripts/oms_redis_sim.py --redis redis://127.0.0.1:6379/0 run --n 20     # live order activity
```

---

## Telegram

1. Create the bot with @BotFather → *TradePulse*; set `TP_TELEGRAM_BOT_TOKEN` (or paste it in Telegram → Save token).
2. Add the bot to groups → they appear as *Pending* → **Activate** (or `/register` by a linked admin).
3. **Groups**: bundle accounts + chats, instant/batched delivery, summaries and position reports.
4. **Accounts**: turn monitoring on (also selects the accounts reconciled at 09:00/09:05).
5. Users link their login: Telegram page → *Link my Telegram* → `/link CODE` to the bot.

Commands: `/status` `/today` `/positions [acct]` `/holdings [acct]` `/refresh positions|holdings ACCT`
`/orders` `/pending` `/rejected` `/trades` `/pnl [week|month]` `/recent` `/order <id>` `/accounts`
`/groups` `/link CODE` `/whoami` `/register` `/chatid`.
In groups the scope is the group's accounts; in private chat it is the linked user's accounts.

## Notification rules

Sent only for **monitored**, unmuted accounts in an enabled group with an active chat, for enabled
trading modes. Event-type precedence (first explicit On/Off wins): **Account › Client › Group › Global
› default**. New event types: `HOLDING_UPDATED`, `RECONCILE_MISMATCH` (both on by default).

## Security

TradePulse logins (bcrypt, cost 12) with OMS roles and scoping · HttpOnly session cookie · CSRF header
· strict CSP · rate limits · validation · Fernet-encrypted secrets, never returned · masked account ids
· CSV formula-injection safe · audit log · Redis writes guarded to `tradepulse:*` · broker access
confined to one read-only module (tests enforce it).

## Tests

```bash
pip install pytest fakeredis
python -m pytest -q                 # 141 tests
```
End-to-end (Playwright) against a real Redis-protocol server, a separate OMS-simulator process and fake
brokers counting every request: imported login, roster accounts, full reconcile (10 requests, min gap
3.03 s, only positions/holdings endpoints), refresh countdown + 429 "please wait" without reaching the
broker, live book update from fills with zero broker calls, holdings T1, mismatch alert, 20 s idle =
zero broker requests, users page, client scoping, 375 px mobile, no console errors - 24/24.

## Known limitations

* Positions/holdings are as good as the last snapshot + the fills the OMS reports. Trades made outside
  the OMS (directly at the broker) appear only at the next snapshot - as a mismatch.
* Unrealised P&L uses the live `LTP:*` price, else the snapshot's last price; shown "n/a" otherwise.
  Unrealised is measured against the broker's average price (for carried positions: since entry).
* Performance for week/month counts the current book's realised P&L; there is no separate P&L history.
* The OMS's own pre-routing rejections and DB-only alerts are not on Redis, so TradePulse cannot see them.
* WhatsApp provider is implemented but optional.
