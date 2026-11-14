# Project: TradePulse – Real-Time OMS Account Monitoring Dashboard

## 1. Project Overview

Build a standalone, modern, responsive dashboard called **TradePulse** that integrates with our existing Order Management System (OMS).

The primary objective is to monitor selected client trading accounts and receive real-time notifications whenever an order or trade event occurs.

We already have an existing OMS where:

- Client account details are stored in Redis.
- Broker authentication sessions/tokens are stored in Redis.
- Multiple clients and broker accounts are managed.
- Order execution is already handled by the existing OMS.

Do NOT rebuild or modify the existing order execution engine.

TradePulse must function as an independent monitoring and notification layer connected to the existing OMS infrastructure.

## 2. Primary Client Requirements

Initially, we need to monitor these three clients:

1. Mathew
2. Jayesh Mehta
3. Bharti Raval

However, the architecture must support dynamically loading all available clients and broker accounts from Redis.

The dashboard must allow administrators to select which accounts they want to monitor.

## 3. Critical Architecture Requirements

### A. Redis Integration

Connect to the existing Redis instance and dynamically retrieve:

- Available clients.
- Client IDs.
- Account IDs.
- Broker names.
- Broker-specific account configurations.
- Existing account mappings.
- Relevant session references.

Important:

- Reuse the existing Redis data structures.
- Inspect the current Redis key patterns and data structures before implementation.
- Do not assume Redis key names or schemas.
- Never expose broker access tokens, API secrets, or session credentials to the frontend.
- Do not modify or overwrite existing OMS Redis keys.
- Use separate namespaces for TradePulse configuration and monitoring state.

### B. Webhook-Based Broker Integration

This is the most critical requirement.

Implement a broker-specific webhook architecture.

Whenever an order event occurs at a broker, the broker webhook should send an event to our backend.

Example endpoint:

POST /api/webhooks/brokers/{broker_name}

The backend should:

1. Receive broker webhook payloads.
2. Identify the broker and account.
3. Validate the webhook signature/authenticity wherever supported.
4. Normalize broker-specific payloads into a common event format.
5. Identify the corresponding client and account.
6. Process the event asynchronously.
7. Publish the event to the dashboard in real time.
8. Trigger configured Telegram or WhatsApp notifications.

Supported event types should include:

- ORDER_PLACED
- ORDER_MODIFIED
- ORDER_CANCELLED
- ORDER_REJECTED
- ORDER_OPEN
- ORDER_PARTIALLY_FILLED
- ORDER_FILLED
- TRADE_EXECUTED
- POSITION_UPDATED

Do not assume every broker supports the same webhook events.

Implement broker-specific adapters so that additional brokers can be integrated without changing the core notification engine.

### C. Absolutely No API Polling

Do not implement:

- Periodic broker order-book API requests.
- Position polling.
- Trade-book polling.
- Background loops calling broker APIs.
- Repeated broker session requests to check order status.

Reason:

The existing OMS is handling live trading execution. Additional API polling could introduce unnecessary broker API load, rate-limit issues, and execution interference.

All monitoring updates must originate from broker webhooks or other explicitly configured event-driven sources.

If a broker does not provide the required webhook functionality, clearly report that limitation instead of silently introducing polling.

### D. Independent Processing

TradePulse must not interfere with live OMS execution.

Requirements:

- Webhook endpoints must acknowledge valid incoming requests quickly.
- Use asynchronous event processing.
- Separate webhook ingestion from notification delivery.
- Never block the webhook response while sending Telegram or WhatsApp messages.
- Implement retry mechanisms for failed notifications.
- Implement event deduplication using broker event IDs or suitable idempotency keys.
- Maintain independent Redis keys and background workers.
- Do not modify existing order placement, modification, or cancellation logic.

Suggested architecture:

Broker Webhooks → Webhook Receiver → Event Validation → Event Normalizer → Async Queue → Redis Event Store → WebSocket/SSE + Notification Workers

## 4. Dashboard Requirements

Build a clean, premium-quality, professional trading operations dashboard.

Design inspiration:

- Modern OMS dashboards.
- TradingView dark interface.
- Professional financial terminal.
- Minimal, information-dense UI.

Use a responsive dark theme with clear typography and intuitive navigation.

### A. Overview Dashboard

Display:

- Total monitored clients.
- Total monitored accounts.
- Active monitoring accounts.
- Orders today.
- Executed trades today.
- Rejected orders.
- Partially filled orders.
- Last received webhook.
- Notification delivery status.

### B. Client & Account Selector

Create a dedicated account management section.

Display clients retrieved dynamically from Redis.

Example:

Mathew

- Broker accounts
- Account status
- Broker name
- Monitoring status

Jayesh Mehta

- Broker accounts
- Account status
- Broker name
- Monitoring status

Bharti Raval

- Broker accounts
- Account status
- Broker name
- Monitoring status

Requirements:

- Search clients.
- Filter by broker.
- Select individual accounts.
- Select multiple accounts.
- Enable/disable monitoring.
- Select all accounts belonging to a client.
- Create custom monitoring groups.

The dashboard must support multiple broker accounts under one client.

### C. Monitoring Groups

Allow administrators to create custom groups.

Examples:

- VIP Clients
- Mathew Accounts
- Jayesh Accounts
- Bharti Accounts
- High Priority Accounts

A group should support multiple accounts, including accounts from different brokers.

Allow:

- Creating groups.
- Editing groups.
- Deleting groups.
- Adding/removing accounts.
- Enabling/disabling group monitoring.
- Configuring group-specific notification channels.

### D. Live Activity Feed

Create a real-time activity feed showing:

- Client name.
- Broker.
- Account identifier (masked).
- Trading symbol.
- Exchange.
- Transaction type (BUY/SELL).
- Order type.
- Quantity.
- Executed quantity.
- Price.
- Order status.
- Timestamp.
- Order ID.
- Trade ID.

Use real-time WebSocket or Server-Sent Events updates.

No frontend polling.

Include:

- Search.
- Client filter.
- Account filter.
- Broker filter.
- Event type filter.
- Date filter.
- Export to Excel/CSV.

### E. Account Details View

When an account is selected, show:

- Client information.
- Broker details.
- Account monitoring status.
- Latest order events.
- Latest executed trades.
- Order history.
- Notification configuration.
- Webhook health information.

Do not expose sensitive credentials.

## 5. Telegram Notification Integration

Implement Telegram bot notifications.

Administrators should be able to:

- Configure Telegram bot token securely.
- Register Telegram chat IDs.
- Create notification groups.
- Associate multiple accounts with a Telegram group.
- Associate multiple Telegram groups with different monitoring configurations.
- Enable/disable notifications per account or group.
- Configure which events trigger notifications.

Example notification:

TRADEPULSE – TRADE EXECUTED

Client: Mathew
Broker: Zerodha
Account: \*\*\*\*1234

Symbol: NIFTY 26000 CE
Exchange: NFO
Side: BUY
Quantity: 75
Price: 125.50
Status: FILLED

Time: 10:32:15 IST

Account: Mathew Trading

Notifications should be delivered asynchronously.

Support Telegram commands where practical:

/start – Register bot.
/help – Display commands.
/accounts – Show authorized monitored accounts.
/groups – List monitoring groups.
/status – Display monitoring status.
/today – Today's order and trade statistics.
/recent – Recent account activity.

Only authorized Telegram users should be allowed to access account information.

## 6. WhatsApp Integration

Design the notification engine to support WhatsApp Business Cloud API or an approved WhatsApp Business provider.

Requirements:

- Configure WhatsApp credentials securely.
- Register recipient phone numbers.
- Support approved WhatsApp templates where required.
- Associate phone numbers with monitoring groups.
- Enable/disable WhatsApp notifications.
- Track delivery status and failures.

Telegram and WhatsApp should be independent notification providers.

The absence of WhatsApp configuration must not affect Telegram notifications.

## 7. Notification Configuration

Provide flexible notification rules.

Example:

Mathew:

- Telegram: Enabled
- WhatsApp: Enabled
- Order placed: Enabled
- Order modified: Disabled
- Order cancelled: Enabled
- Trade executed: Enabled
- Order rejected: Enabled

Jayesh Mehta:

- Telegram: Enabled
- WhatsApp: Disabled
- Trade executed: Enabled
- Order rejected: Enabled

Bharti Raval:

- Telegram: Enabled
- WhatsApp: Enabled
- All order events: Enabled

Support configuration at:

- Global level.
- Client level.
- Account level.
- Group level.

Define and document rule precedence to avoid conflicting notification settings.

## 8. Backend API Design

Suggested endpoints:

Clients:
GET /api/clients
GET /api/clients/{client_id}/accounts

Monitoring:
GET /api/monitoring/accounts
POST /api/monitoring/accounts/select
POST /api/monitoring/accounts/{account_id}/enable
POST /api/monitoring/accounts/{account_id}/disable

Groups:
GET /api/groups
POST /api/groups
PUT /api/groups/{group_id}
DELETE /api/groups/{group_id}

Events:
GET /api/events
GET /api/events/{event_id}
GET /api/events/stats

Notifications:
GET /api/notifications/settings
PUT /api/notifications/settings
GET /api/notifications/history
POST /api/notifications/test

Webhooks:
POST /api/webhooks/brokers/{broker_name}

Dashboard:
WebSocket /ws/events

These are suggested routes. Adapt them to the existing backend conventions.

## 9. Redis Data Architecture

Keep existing OMS Redis data untouched.

Create separate namespaces, for example:

tradepulse\:config
tradepulse\:config
tradepulse\:config
tradepulse\:config
tradepulse\:events
tradepulse\:events
tradepulse\:notifications
tradepulse\:notifications
tradepulse\:health

Use appropriate Redis data structures and TTLs.

Important:

- Do not duplicate broker session tokens.
- Resolve existing account mappings safely.
- Store only the minimum necessary monitoring metadata.
- Use Redis Streams or another suitable durable queue mechanism for asynchronous event processing.
- Ensure events are not silently lost during worker restarts.

## 10. Security and Reliability

Implement:

- Authentication for dashboard users.
- Role-based authorization.
- Secure webhook validation.
- Rate limiting.
- Input validation.
- Secret management through environment variables.
- Masked account identifiers.
- Audit logging.
- Event deduplication.
- Notification retry policies.
- Webhook health monitoring.
- Error tracking.

Never log broker access tokens or sensitive credentials.

## 11. Technology Stack

Prefer our existing OMS technology stack.

Backend:

- Python.
- FastAPI.
- Redis.
- Async processing.
- WebSockets or SSE.

Frontend:

- React / Next.js, depending on the existing project architecture.
- Tailwind CSS.
- Reusable dashboard components.
- Lucide icons.
- Responsive layouts.

Do not introduce unnecessary infrastructure or dependencies.

## 12. Implementation Strategy

Before writing code:

1. Inspect the existing OMS repository.
2. Understand the current Redis key structure.
3. Identify how client accounts map to broker sessions.
4. Identify all currently integrated brokers.
5. Check which brokers support webhooks and which event types they provide.
6. Understand the existing authentication and deployment architecture.
7. Prepare a short implementation plan.

Then implement in phases:

Phase 1: Redis integration and dynamic client/account discovery.

Phase 2: Broker webhook ingestion and broker-specific adapters.

Phase 3: Normalized event processing and asynchronous queue.

Phase 4: Real-time dashboard and activity feed.

Phase 5: Account selection and monitoring groups.

Phase 6: Telegram integration and bot commands.

Phase 7: WhatsApp integration. (not required for now)

Phase 8: Testing, security, deployment and documentation.

## 13. Acceptance Criteria

The project is complete when:

1. Mathew, Jayesh Mehta and Bharti Raval are dynamically visible from Redis.
2. Additional Redis accounts appear without hardcoding.
3. Administrators can select individual accounts or groups.
4. Broker webhooks generate real-time dashboard updates.
5. Telegram receives notifications for configured events.
6. WhatsApp notifications work when configured.
7. No broker API polling is introduced.
8. Existing OMS execution functionality remains unchanged.
9. Duplicate webhook deliveries do not create duplicate notifications.
10. Events and failed notifications are recoverable.
11. The dashboard works responsively on desktop and mobile.
12. Monitoring and notification services can be deployed independently.

**Most important:** This is a monitoring and notification system, NOT another OMS execution engine. Preserve the existing live trading infrastructure and build TradePulse as an isolated, event-driven service.
