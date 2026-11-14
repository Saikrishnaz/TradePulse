"""Wires the components together and runs the background workers.

``python -m tradepulse.app``    - dashboard/API (+ workers when TP_RUN_WORKERS=1, the default)
``python -m tradepulse.worker`` - workers only, for deploying monitoring/notification separately

Data flows only through Redis (the OMS's streams/roster/sessions, read-only) and TradePulse's own
database. The OMS itself - its database and its HTTP API - is never contacted.
"""

from __future__ import annotations

import logging
import threading

from . import bus as busmod
from .assistant import Assistant
from .bot import Bot
from .brokers import BrokerReaders
from .localdata import LocalData
from .notify.outbox import Notifier
from .notify.telegram import TelegramClient
from .notify.whatsapp import WhatsAppProvider
from .processor import Processor
from .reconcile import BookKeeper, Reconciler
from .scheduler import Scheduler
from .service import DataService
from .sources.redis_tail import RedisTail
from .sources.webhooks import adapters
from .store import Store
from .users import ensure_bootstrap_admin
from .vault import Vault

log = logging.getLogger("tradepulse.runtime")


class Runtime:
    def __init__(self, settings, bus=None, store=None, telegram_client=None, ai_client=None, broker_http=None, oms=None):
        self.settings = settings
        self.store = store or Store(settings.DB_PATH)
        if bus is None:
            try:
                bus = busmod.make_bus(settings)
            except Exception as ex:
                log.error("Redis unavailable (%s). TradePulse reads the OMS only through Redis, so nothing new will "
                          "arrive until it is reachable. Using an in-process bus meanwhile.", ex)
                bus = busmod.MemoryBus()
        self.bus = bus
        self.vault = Vault(self.store, settings.FERNET_KEY)
        ensure_bootstrap_admin(self.store, settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)
        self.oms = LocalData(self.store)               # name kept: the data interface the API/bot/assistant use
        self.svc = DataService(self.oms, self.store, self.bus)
        self._tg_fixed = telegram_client
        self._tg_cache: tuple[str, TelegramClient] | None = None
        self.assistant = Assistant(self.svc, self.store, settings, client=ai_client)
        self.processor = Processor(self.store, self.bus, self.oms)
        self.keeper = BookKeeper(self.store, self.bus)
        self.readers = BrokerReaders(self.bus, settings, self.store, http=broker_http)
        self.reconciler = Reconciler(self.store, self.bus, self.readers, self.keeper, self.oms)
        self.tail = RedisTail(self.store, self.bus, self.keeper, events_stream=settings.OMS_EVENTS_STREAM,
                              command_pattern=settings.OMS_COMMAND_STREAMS)
        self.whatsapp = WhatsAppProvider(self.vault)
        self.notifier = Notifier(self.store, self.bus, self.telegram, self.whatsapp)
        self.scheduler = Scheduler(self.store, self.bus, self.svc)
        self.bot = Bot(self.store, self.svc, self.assistant, self.telegram, bus=self.bus, reconciler=self.reconciler)
        self.adapters = adapters(self.oms, self.vault, settings)
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    # --- Telegram client (token from env, else the encrypted vault) --------------------------
    def bot_token(self) -> str:
        return self.settings.TELEGRAM_BOT_TOKEN or self.vault.get("telegram_bot_token")

    def telegram(self):
        if self._tg_fixed is not None:
            return self._tg_fixed
        token = self.bot_token()
        if not token:
            return None
        if not self._tg_cache or self._tg_cache[0] != token:
            self._tg_cache = (token, TelegramClient(token, self.settings.TELEGRAM_API))
            self.bot.username = ""
        return self._tg_cache[1]

    # --- workers ------------------------------------------------------------------------------
    def start_workers(self, polling: bool | None = None, primary: bool = True) -> None:
        """``primary`` runs the singletons (Redis tail, scheduler, 09:00/09:05 reconciler, Telegram polling).
        Extra replicas (primary=False) run only the idempotent processor and notifier."""
        polling = self.settings.TELEGRAM_MODE == "polling" if polling is None else polling
        jobs = [("processor", self.processor.run), ("notifier", self.notifier.run)]
        if primary:
            jobs += [("redis-tail", self.tail.run), ("scheduler", self.scheduler.run), ("reconciler", self.reconciler.loop)]
        if polling and primary:
            jobs.append(("telegram", self.bot.run_polling))
        for name, fn in jobs:
            t = threading.Thread(target=fn, args=(self.stop,), name=f"tp-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        log.info("workers started: %s", ", ".join(n for n, _ in jobs))

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=3)
