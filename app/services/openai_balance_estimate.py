"""Use Magpie's priced usage between verified OpenAI credit readings."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

USAGE_URL = "http://magpie:3425/v1/magpie/usage/requests"
REFRESH_AFTER = timedelta(hours=12)
RETRY_AFTER = timedelta(minutes=15)
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Invalid amount")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("Invalid amount")
    return amount


@dataclass(frozen=True)
class Ledger:
    calls: int
    unpriced: int
    cost: Decimal

    @classmethod
    def read(cls, calls, unpriced, cost):
        if type(calls) is not int or type(unpriced) is not int or not 0 <= unpriced <= calls:
            raise ValueError("Invalid usage counts")
        cost = _money(cost).quantize(Decimal("0.00000001"))
        if cost < 0:
            raise ValueError("Invalid usage cost")
        return cls(calls, unpriced, cost)

    def extends(self, prior: Ledger) -> bool:
        if self.calls == prior.calls:
            return self == prior
        return self.calls > prior.calls and self.unpriced >= prior.unpriced and self.cost >= prior.cost


@dataclass(frozen=True)
class Snapshot:
    identity: str
    amount: Decimal
    checked_at: datetime
    ledger: Ledger | None


async def magpie_ledger(client: httpx.AsyncClient, key: str) -> Ledger | None:
    request = httpx.Request("GET", USAGE_URL,
        params={"provider": "raven-openai", "period": "all", "via": "through", "limit": 1},
        headers={"Authorization": f"Bearer {key}"})
    try:
        async with asyncio.timeout(0.65):
            result = (await client.send(request, follow_redirects=False)).raise_for_status().json(parse_float=Decimal)
        return Ledger.read(result["total"], result["unpriced"], result["cost"])
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError):
        return None


class BalanceEstimate:
    def __init__(self, path: Path | None):
        self.path = path
        self.snapshot = self._load()
        self.last_ledger = self.snapshot.ledger if self.snapshot else None
        self.ledger_reset = False
        self.retry_at: datetime | None = None
        self.identity = self.snapshot.identity if self.snapshot else ""
        self.lock = asyncio.Lock()

    def _load(self) -> Snapshot | None:
        if self.path is None:
            return None
        try:
            data = json.loads(self.path.read_text())
            checked = datetime.fromisoformat(data["checked_at"])
            if checked.tzinfo is None:
                return None
            ledger = Ledger.read(**data["ledger"]) if data["ledger"] is not None else None
            return Snapshot(data["identity"], _money(data["amount"]), checked, ledger)
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
            return None

    def verified(self, identity: str) -> Snapshot | None:
        snap = self.snapshot
        return snap if snap and snap.identity == identity else None

    def reusable(self, identity: str, ledger: Ledger | None, floor: Decimal, now: datetime) -> Decimal | None:
        if identity != self.identity:
            self.identity, self.last_ledger, self.retry_at = identity, None, None
            self.ledger_reset = False
        snap = self.verified(identity)
        prior = self.last_ledger
        if ledger is not None:
            self.last_ledger = ledger
            if prior and not ledger.extends(prior):
                self.ledger_reset = True
        if not snap or not timedelta(0) <= now - snap.checked_at < REFRESH_AFTER:
            return None
        if snap.amount < floor:
            return snap.amount
        if (ledger is None or snap.ledger is None or self.ledger_reset
                or not ledger.extends(snap.ledger) or ledger.unpriced != snap.ledger.unpriced):
            return None
        spend = ledger.cost - snap.ledger.cost
        estimate = snap.amount - spend
        buffer = floor + Decimal(5)
        # After verifying inside the buffer, recheck after another dollar of
        # priced spend or on reaching the floor, whichever comes first.
        boundary = buffer if snap.amount > buffer else max(floor, snap.amount - Decimal(1))
        return estimate if spend == 0 or estimate > boundary else None

    def remember(self, identity: str, amount: Decimal, ledger: Ledger | None, now: datetime) -> None:
        prior = self.verified(identity)
        uncertain = (ledger is None or self.ledger_reset
                     or (prior and prior.ledger and ledger.unpriced != prior.ledger.unpriced))
        self.snapshot = Snapshot(identity, amount, now, ledger)
        self.last_ledger = ledger
        self.ledger_reset = False
        self.retry_at = now + RETRY_AFTER if uncertain else None
        if self.path is None:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as out:
                temporary = Path(out.name)
                json.dump(asdict(self.snapshot), out, default=str)
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            logger.warning("Could not persist the verified OpenAI balance baseline")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
