"""Observed flags (DESIGN.md §7 "Observed flags"): the targeting composer's typeahead.

Every serving-API request already says "give me the prompt for THESE flags", so the
server records the (environment, flag, value) triples it sees — no SDK protocol, and it
works for every client in every language. Three layers keep this off the hot path and
out of Postgres' way:

  * **Request path** (:meth:`FlagObserver.observe`): a lock-free dict check per flag;
    only a value not seen within the dedupe window takes the lock, is marked, and is
    queued. Never a DB write (§8 "No DB per request"). The queue is bounded — when
    Postgres is down it drops, it never grows.
  * **Writer** (:func:`flush_observations`, driven by a background loop on the full
    node): one upsert per pass whose ``last_seen`` refresh is guarded (``WHERE
    last_seen < EXCLUDED.last_seen - 1 hour``) so N processes re-observing the same
    value don't churn dead tuples.
  * **Census** (:func:`prune_and_census`, hourly): prunes values unseen for the TTL and
    suppresses high-cardinality flags (user ids, emails) — their values are purged and
    further observations dropped at the source, so they can never fill the table. The
    observer also trips suppression locally, mid-hour, when a flag's distinct values
    exceed the cap inside one dedupe window.

Serve replicas run no observer (they have no write path); v1 records what the full
node serves. Values are suggestions only — nothing about what serves depends on them.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import models
from ..core.model import All, Any_, Clause, Condition, EnvSnapshot, Not, Rule

log = logging.getLogger("incant.observed")

MAX_VALUE_LEN = 128
#: Two observations of the same value closer together than this never both write:
#: the upsert's WHERE clause skips the refresh. Mirrors the observer's dedupe window
#: for the cross-process/cold-cache case.
REFRESH_GUARD = "1 hour"


# ── value normalisation ───────────────────────────────────────────────

def scalar_of(raw) -> tuple[str, str] | None:
    """``(value_type, text)`` for a recordable scalar, else None. Bools before ints
    (bool is an int subclass); no NaN/inf; empty strings are not suggestions."""
    if isinstance(raw, bool):
        return ("bool", "true" if raw else "false")
    if isinstance(raw, int):
        return ("int", str(raw))
    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw):
            return None
        return ("float", repr(raw))
    if isinstance(raw, str):
        return ("str", raw) if raw else None
    return None


def typed_value(value: str, value_type: str):
    """Inverse of :func:`scalar_of`: the JSON value a clause should carry."""
    if value_type == "bool":
        return value == "true"
    if value_type == "int":
        try:
            return int(value)
        except ValueError:
            return value
    if value_type == "float":
        try:
            return float(value)
        except ValueError:
            return value
    return value


# ── rule-derived flags (the zero-traffic baseline) ────────────────────

def _collect(cond: Condition, out: dict[str, set]) -> None:
    if cond is None:
        return
    if isinstance(cond, Clause):
        vals = out.setdefault(cond.flag, set())
        for v in (cond.value, *cond.values):
            if isinstance(v, (str, int, float, bool)):
                vals.add(v)
    elif isinstance(cond, (All, Any_)):
        for c in cond.of:
            _collect(c, out)
    elif isinstance(cond, Not):
        _collect(cond.of, out)


def collect_rule_flags(snap: EnvSnapshot, rules: Iterable[Rule]) -> dict[str, set]:
    """Flag names the given rules consult → the enumerable values their clauses name."""
    flags: dict[str, set] = {}
    for r in rules:
        _collect(r.when, flags)
    return flags


def all_rules(snap: EnvSnapshot) -> list[Rule]:
    return [r for pid in snap.all_prompt_ids() for r in snap.prompt_rules(pid)]


# ── request-path observer ─────────────────────────────────────────────

@dataclass(frozen=True)
class Observation:
    env: str
    flag: str
    value: str
    value_type: str
    seen_at: dt.datetime


@dataclass
class ObserverStats:
    queued: int = 0
    deduped: int = 0
    dropped: int = 0      # queue full
    ignored: int = 0      # non-scalar, too long, excluded, or suppressed


class FlagObserver:
    """Per-process observer. ``observe`` is the only method on the request path."""

    def __init__(
        self, *, enabled: bool = True, dedupe_seconds: float = 900.0,
        max_pending: int = 100_000, value_cap: int = 50_000,
        exclude: Iterable[str] = (), clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self._dedupe = float(dedupe_seconds)
        self._max_pending = int(max_pending)
        self._value_cap = int(value_cap)
        self._exclude = frozenset(exclude)
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str, str], float] = {}          # key → expires_at
        self._pending: dict[tuple[str, str, str], tuple[str, dt.datetime]] = {}
        self._distinct: dict[tuple[str, str], int] = {}             # (env, flag) → live keys
        self._suppressed: set[tuple[str, str]] = set()
        self._new_suppressions: set[tuple[str, str]] = set()
        self.stats = ObserverStats()

    # request path ────────────────────────────────────────────────────
    def observe(self, env: str, flags: dict) -> int:
        """Record the scalar flags of one request. Returns how many were newly queued.
        Hit path: one dict read per flag, no lock. Miss path: lock, mark, enqueue."""
        if not self.enabled or not flags:
            return 0
        now = self._clock()
        queued = 0
        for name, raw in flags.items():
            if name in self._exclude:
                self.stats.ignored += 1
                continue
            sc = scalar_of(raw)
            if sc is None or len(sc[1]) > MAX_VALUE_LEN:
                self.stats.ignored += 1
                continue
            value_type, value = sc
            if (env, name) in self._suppressed:
                self.stats.ignored += 1
                continue
            key = (env, name, value)
            exp = self._seen.get(key)
            if exp is not None and exp > now:
                self.stats.deduped += 1
                continue
            with self._lock:
                exp = self._seen.get(key)
                if exp is not None and exp > now:
                    self.stats.deduped += 1
                    continue
                if (env, name) in self._suppressed:
                    self.stats.ignored += 1
                    continue
                if len(self._pending) >= self._max_pending:
                    self.stats.dropped += 1
                    continue
                self._seen[key] = now + self._dedupe
                self._pending[key] = (value_type, dt.datetime.now(dt.timezone.utc))
                n = self._distinct.get((env, name), 0) + 1
                self._distinct[(env, name)] = n
                queued += 1
                if n > self._value_cap:
                    self._suppress_locked(env, name)
        self.stats.queued += queued
        return queued

    # writer side ─────────────────────────────────────────────────────
    def drain(self) -> list[Observation]:
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        return [Observation(env, flag, value, vt, at) for (env, flag, value), (vt, at) in items]

    def unmark(self, observations: Iterable[Observation]) -> None:
        """A flush failed: forget these marks so the next request re-queues them."""
        with self._lock:
            for o in observations:
                self._seen.pop((o.env, o.flag, o.value), None)

    def sweep(self) -> int:
        """Evict expired marks (bounds memory). Returns how many were evicted."""
        now = self._clock()
        with self._lock:
            expired = [k for k, exp in self._seen.items() if exp <= now]
            for k in expired:
                del self._seen[k]
                pair = (k[0], k[1])
                n = self._distinct.get(pair, 0) - 1
                if n <= 0:
                    self._distinct.pop(pair, None)
                else:
                    self._distinct[pair] = n
            if len(self._seen) > 2 * self._max_pending:   # pathological; start over
                self._seen.clear()
                self._distinct.clear()
        return len(expired)

    def _suppress_locked(self, env: str, flag: str) -> None:
        self._suppressed.add((env, flag))
        self._new_suppressions.add((env, flag))
        for key in [k for k in self._pending if k[0] == env and k[1] == flag]:
            del self._pending[key]
        log.warning("observed flags: %r in %r exceeded %d distinct values — suppressed "
                    "(not suggested; values purged)", flag, env, self._value_cap)

    def take_new_suppressions(self) -> set[tuple[str, str]]:
        with self._lock:
            out, self._new_suppressions = self._new_suppressions, set()
            return out

    def set_suppressed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Replace the suppressed set with DB truth (boot, census, forget)."""
        with self._lock:
            self._suppressed = set(pairs)

    def is_suppressed(self, env: str, flag: str) -> bool:
        return (env, flag) in self._suppressed

    def pending_size(self) -> int:
        return len(self._pending)

    def seen_size(self) -> int:
        return len(self._seen)


# ── writer ────────────────────────────────────────────────────────────

def flush_observations(session: Session, observations: list[Observation]) -> int:
    """One upsert for a drained batch. Rows for environments that no longer exist are
    skipped (an env deleted between observe and flush must not fail the batch).
    Returns the number of rows inserted or refreshed — refreshes closer than
    ``REFRESH_GUARD`` to the stored ``last_seen`` are skipped by the WHERE clause."""
    if not observations:
        return 0
    envs = set(session.execute(select(models.Environment.id)).scalars())
    rows = [
        {"environment_id": o.env, "flag": o.flag, "value": o.value,
         "value_type": o.value_type, "first_seen": o.seen_at, "last_seen": o.seen_at}
        for o in observations if o.env in envs
    ]
    if not rows:
        return 0
    stmt = pg_insert(models.ObservedFlag).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["environment_id", "flag", "value"],
        set_={"last_seen": stmt.excluded.last_seen, "value_type": stmt.excluded.value_type},
        where=text(f"observed_flags.last_seen < EXCLUDED.last_seen - interval '{REFRESH_GUARD}'"),
    ).returning(models.ObservedFlag.flag)
    # RETURNING yields only inserted/refreshed rows (the guarded ones are neither), so
    # the count is exact even where the driver reports no multi-row rowcount.
    return len(session.execute(stmt).all())


def record_suppressions(session: Session, pairs: Iterable[tuple[str, str]], values_seen: int) -> None:
    """Persist observer-tripped suppressions (values purged) so the UI and every later
    boot see them; no-op for environments that vanished."""
    pairs = list(pairs)
    if not pairs:
        return
    envs = set(session.execute(select(models.Environment.id)).scalars())
    now = dt.datetime.now(dt.timezone.utc)
    for env, flag in pairs:
        if env not in envs:
            continue
        session.execute(delete(models.ObservedFlag).where(
            models.ObservedFlag.environment_id == env, models.ObservedFlag.flag == flag))
        stmt = pg_insert(models.ObservedFlagSuppression).values(
            environment_id=env, flag=flag, values_seen=values_seen, suppressed_at=now)
        session.execute(stmt.on_conflict_do_update(
            index_elements=["environment_id", "flag"],
            set_={"values_seen": values_seen, "suppressed_at": now}))


# ── census ────────────────────────────────────────────────────────────

@dataclass
class CensusResult:
    pruned: int = 0
    suppressed: list[tuple[str, str, int]] = field(default_factory=list)   # newly suppressed
    total_suppressed: int = 0

    def summary(self) -> str:
        return (f"observed flags census: pruned {self.pruned} stale value(s); "
                f"newly suppressed {len(self.suppressed)} flag(s); "
                f"{self.total_suppressed} suppressed in total")


def prune_and_census(session: Session, *, ttl_days: int, value_cap: int) -> CensusResult:
    """Hourly hygiene: drop values unseen for ``ttl_days``; suppress any (env, flag)
    with more than ``value_cap`` distinct values (purging them). Suppression rows are
    exempt from the TTL — a flag stays suppressed until an operator forgets it."""
    res = CensusResult()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ttl_days)
    res.pruned = session.execute(
        delete(models.ObservedFlag).where(models.ObservedFlag.last_seen < cutoff)
    ).rowcount or 0
    over = session.execute(
        select(models.ObservedFlag.environment_id, models.ObservedFlag.flag, func.count())
        .group_by(models.ObservedFlag.environment_id, models.ObservedFlag.flag)
        .having(func.count() > value_cap)
    ).all()
    for env, flag, n in over:
        record_suppressions(session, [(env, flag)], int(n))
        res.suppressed.append((env, flag, int(n)))
    res.total_suppressed = session.execute(
        select(func.count()).select_from(models.ObservedFlagSuppression)).scalar_one()
    session.flush()
    if res.pruned or res.suppressed:
        log.info(res.summary())
    return res


def load_suppressions(session: Session) -> set[tuple[str, str]]:
    return {
        (r.environment_id, r.flag)
        for r in session.execute(select(models.ObservedFlagSuppression)).scalars()
    }


def forget_flag(session: Session, env: str, flag: str) -> int:
    """Operator reset: drop a flag's values AND its suppression. Returns values removed."""
    n = session.execute(delete(models.ObservedFlag).where(
        models.ObservedFlag.environment_id == env, models.ObservedFlag.flag == flag)).rowcount or 0
    session.execute(delete(models.ObservedFlagSuppression).where(
        models.ObservedFlagSuppression.environment_id == env,
        models.ObservedFlagSuppression.flag == flag))
    return n
