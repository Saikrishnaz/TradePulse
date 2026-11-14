"""Who gets notified about an event.

Gates (all must pass; any failure means the event is still stored and shown live, just not sent):

1. The account is **monitored** (Accounts page) and not **muted**.
2. The event's trading mode is in ``notify_modes`` (default LIVE + PAPER; DRY_RUN is test noise).
3. The destination group is enabled and the destination channel is enabled + ACTIVE, and the
   provider (Telegram / WhatsApp) is enabled globally.

Event-type precedence - the FIRST level that has an explicit on/off setting wins:

    Account  >  Client  >  Group  >  Global  >  built-in default

* Account and client settings describe *the account*: "never tell anyone about Mathew's order
  modifications" applies to every group that account is in.
* Group settings tailor what *one audience* receives, for accounts that have no account/client
  setting of their own ("the VIP group also wants order-open events").
* Global is the house default; the built-in default (``events.DEFAULT_ENABLED``) applies when
  nothing at all is configured.

Provider switches (``CHANNEL_TELEGRAM`` / ``CHANNEL_WHATSAPP``) are resolved with the same chain,
so "Jayesh: WhatsApp disabled" is an account- or client-level ``CHANNEL_WHATSAPP = off``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import DEFAULT_ENABLED

CHANNEL_KEYS = {"telegram": "CHANNEL_TELEGRAM", "whatsapp": "CHANNEL_WHATSAPP"}


@dataclass
class RuleSnapshot:
    settings: dict
    monitored: dict                       # account_id -> row (enabled, muted)
    groups: list                          # store.groups()
    rules: dict                           # (scope, scope_id) -> {event_type: bool}
    client_of: dict = field(default_factory=dict)   # account_id -> client (OMS user) id

    @classmethod
    def load(cls, store, client_of: dict | None = None) -> "RuleSnapshot":
        rules: dict = {}
        for r in store.all_rules():
            rules.setdefault((r["scope"], r["scope_id"]), {})[r["event_type"]] = bool(r["enabled"])
        return cls(settings=store.get_settings(), monitored=store.monitored(), groups=store.groups(),
                   rules=rules, client_of=client_of or {})


def resolve_flag(snap: RuleSnapshot, key: str, account_id: str, group_id: int | None, default: bool) -> tuple[bool, str]:
    """Walk the precedence chain for one setting; returns (value, level that decided it)."""
    chain = [("account", account_id)]
    client = snap.client_of.get(account_id)
    if client is not None:
        chain.append(("client", str(client)))
    if group_id is not None:
        chain.append(("group", str(group_id)))
    chain.append(("global", ""))
    for scope, sid in chain:
        v = snap.rules.get((scope, sid), {}).get(key)
        if v is not None:
            return v, scope
    return default, "default"


def event_enabled(snap: RuleSnapshot, event_type: str, account_id: str, group_id: int | None) -> tuple[bool, str]:
    return resolve_flag(snap, event_type, account_id, group_id, DEFAULT_ENABLED.get(event_type, False))


def targets(snap: RuleSnapshot, event: dict) -> list[dict]:
    """Channels (with their group) that should receive this event. Empty list = store/show only."""
    account_id = event.get("account_id") or ""
    mon = snap.monitored.get(account_id)
    if not mon or not mon.get("enabled") or mon.get("muted"):
        return []
    mode = (event.get("trading_mode") or "").upper()
    global_modes = [m.upper() for m in snap.settings.get("notify_modes", [])]
    out, seen = [], set()
    for g in snap.groups:
        if not g["enabled"] or account_id not in g["account_ids"]:
            continue
        # Trading-mode filter: the group's own choice ("Live trades" group = LIVE only), else the global one.
        # Events without a mode (alerts, reconcile mismatches) are not filtered.
        modes = [m.upper() for m in (g.get("modes") or [])] or global_modes
        if mode and mode not in modes:
            continue
        ok, _lvl = event_enabled(snap, event["type"], account_id, g["id"])
        if not ok:
            continue
        for ch in g["channels"]:
            if not ch["enabled"] or ch["status"] != "ACTIVE" or ch["id"] in seen:
                continue
            if not snap.settings.get(f"{ch['kind']}_enabled", ch["kind"] == "telegram"):
                continue
            prov_ok, _ = resolve_flag(snap, CHANNEL_KEYS[ch["kind"]], account_id, g["id"], True)
            if not prov_ok:
                continue
            seen.add(ch["id"])
            out.append({"channel": ch, "group": g})
    return out


def explain(snap: RuleSnapshot, event_type: str, account_id: str) -> list[dict]:
    """For the dashboard: per group, would this event type be sent for this account, and why."""
    out = []
    for g in snap.groups:
        if account_id not in g["account_ids"]:
            continue
        v, lvl = event_enabled(snap, event_type, account_id, g["id"])
        out.append({"group_id": g["id"], "group": g["name"], "enabled": v, "decided_by": lvl})
    return out
