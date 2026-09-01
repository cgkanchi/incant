"""Bootstrap configuration (pydantic-settings). Only the essentials to start up."""

from __future__ import annotations

import os
from pathlib import Path

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secrets may arrive as files (k8s/docker secrets mounts) instead of env values:
# INCANT_<NAME>_FILE points at a file whose stripped contents become the value.
# The direct env var wins when both are set.
_FILE_BACKED = ("DATABASE_URL", "KEY_PEPPER", "BOOTSTRAP_ADMIN_KEY")


def _load_file_secrets() -> None:
    for name in _FILE_BACKED:
        env = f"INCANT_{name}"
        path = os.environ.get(f"{env}_FILE")
        if path and env not in os.environ:
            try:
                os.environ[env] = Path(path).read_text().strip()
            except OSError as exc:
                raise RuntimeError(f"{env}_FILE points at an unreadable file: {exc}") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INCANT_", env_file=".env", extra="ignore")

    # Storage. Postgres is the control plane — Incant is multi-user from the ground
    # up, and a real connection pool is where concurrency bugs surface (SQLite's
    # serialized writer hides them). Point this at your Postgres; docker-compose
    # wires the bundled `db` service automatically.
    database_url: str = "postgresql+psycopg://incant:incant@localhost:5432/incant"
    repo_path: str = "./var/repo"          # canonical git repository (bare)

    # Serving
    default_environment: str = Field(default="prod", min_length=1, max_length=32)
    mode: Literal["full", "serve"] = "full"

    # Bind
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    # Auth: the bootstrap admin key. Empty by default — on first boot with no admin
    # yet, Incant generates a strong random key and prints it once (see
    # ensure_bootstrap_admin). Set this to pin your own key; the well-known
    # `incant_sk_dev_admin` is refused unless INCANT_ALLOW_DEV_KEY=1 (dev/test only).
    bootstrap_admin_key: str = ""

    # Key hashing pepper (defense-in-depth). Empty ⇒ legacy plain-SHA256 hashing
    # (keys are high-entropy, so unsalted SHA-256 is not brute-forceable). Set it and
    # new/rotated keys are stored as `v2$` HMAC-SHA256(pepper, key); legacy keys are
    # upgraded in place on their next successful auth. Keep it stable and secret
    # (a filesystem/env secret, not in the DB) — changing it invalidates v2 hashes.
    key_pepper: str = ""

    # In-memory API-key cache TTL (seconds). Revocation/issuance is immediate on the
    # local process (invalidate_auth); on multi-replica deployments a change made on
    # one node propagates to the others within this TTL.
    auth_ttl: float = Field(default=5.0, gt=0)

    # Control-plane poll interval (seconds). The serving hot path never reads the DB
    # itself (§8 "No DB per request"; §10 "the DB is never on the per-request path"); a
    # background loop polls every this-many seconds and pulls targeting bumps + the
    # TTL-driven auth reload into memory. This is the poll fallback for the design's
    # Postgres LISTEN/NOTIFY (§7), which names a 2s poll — so a targeting change
    # (including "make live") propagates to every replica in < 2 s.
    control_poll_seconds: float = Field(default=2.0, gt=0)

    # Periodic git↔DB main-commit drift check interval (seconds). `reconcile_main_commits`
    # runs once at boot and then on this cadence (full mode) so governance drift — an
    # unvalidated `main` tip or an orphan commit left by a publish whose outer DB
    # transaction rolled back after `main` moved (§3 "git owns content, the DB owns
    # state"; §5 "Validation first") — stays visible in metrics and /healthz instead of
    # being a boot-only log line. Detect-and-log only: it NEVER flips readiness (a drifted
    # node still serves correct content from the last VALIDATED SHAs).
    reconcile_interval_seconds: float = Field(default=3600.0, gt=0)

    # Backup pushes (§6, full mode): every this-many seconds, push the full ref set to
    # any enabled remote that is behind main. The interval bounds the content-durability
    # exposure window between a commit landing and its off-site copy. 0 disables the loop
    # (remotes can still be pushed manually via POST /mgmt/remotes/{id}/push).
    backup_poll_seconds: float = Field(default=15.0, ge=0)   # 0 disables

    # Serve replicas (§13/§15): every this-many seconds, mirror-fetch content from the
    # first enabled remote that answers, so SHAs referenced by fresh targeting become
    # fetchable without a shared volume. 0 disables (shared-volume deployments).
    content_fetch_seconds: float = Field(default=30.0, ge=0)  # 0 disables

    # Timeout (seconds) for one remote git operation (push/fetch/clone) — a hung ssh
    # must not wedge the backup or fetch loop.
    backup_timeout_seconds: float = Field(default=60.0, gt=0)

    # Pinned known_hosts file for ssh remotes (mounted read-only in §15's compose).
    # Empty ⇒ ssh's default resolution.
    known_hosts_path: str = ""

    # First-boot content bootstrap (§6): a git URL the full node clones from when its
    # repo volume is EMPTY. A populated Incant repo is ADOPTED (registry rebuilt from
    # the tree + trailers, tips re-validated); a blank repo means "start fresh and
    # push here". Either way the remote is registered as an enabled backup target.
    # Unreachable while set ⇒ boot FAILS (a mistyped remote must not silently create
    # a fresh lineage that later force-pushes over the real one). Empty ⇒ off.
    bootstrap_remote: str = ""
    # Credential for the bootstrap remote: path to an ssh private key (ssh URLs) or
    # to a git credential-store file (https URLs) — becomes the remote's auth_ref.
    bootstrap_remote_key: str = ""

    # Targeting-revision checkpointing (§7): a FULL environment state is materialized
    # every this-many revisions (plus always on baseline and rollback revisions); the
    # revisions in between carry only their per-object change. Replay/rollback to a
    # checkpoint is O(1); to any other revision it reconstructs by forward-applying at
    # most K-1 per-object changes from the nearest older checkpoint — bounded work,
    # off the hot path. Storage: O(N/K) states instead of O(N).
    revision_checkpoint_interval: int = Field(default=20, ge=1)

    # Observed flags (§7): flag/value pairs seen on the serving API feed the targeting
    # composer's typeahead. The request path does a dict check + a bounded queue put —
    # never a DB write; a background writer on the full node flushes the queue.
    observe_flags: bool = True
    observed_flags_exclude: str = ""                                     # comma-separated names never recorded
    observed_flags_dedupe_seconds: float = Field(default=900.0, gt=0)     # per-process "seen recently" window
    observed_flags_flush_seconds: float = Field(default=5.0, gt=0)
    observed_flags_max_pending: int = Field(default=100_000, ge=1_000)    # queue bound (drops when full)
    observed_flags_value_cap: int = Field(default=50_000, ge=100)         # distinct values per (env, flag) before suppression
    observed_flags_ttl_days: int = Field(default=30, ge=1)

    # Failed-auth throttling: per-client-IP sliding window over FAILED bearer auths.
    # After `limit` failures within `window` seconds, that IP gets 429 (Retry-After)
    # until the window drains. Successful auth is never throttled. limit=0 disables.
    auth_throttle_limit: int = Field(default=20, ge=0)        # 0 disables
    auth_throttle_window: float = Field(default=60.0, gt=0)

    # /metrics access: a Prometheus scraper with no principal can authenticate with
    # `Authorization: Bearer <this>`. Empty ⇒ /metrics requires a real viewer key.
    metrics_token: str = ""

    # Emit HSTS (Strict-Transport-Security) on every response. Only enable when TLS
    # terminates in front of Incant (a reverse proxy); Incant itself speaks plain HTTP.
    enforce_tls: bool = False

    # Trusted reverse-proxy IPs (comma-separated). X-Forwarded-For is honored (its
    # first hop taken as the client IP for throttling) ONLY when the direct peer
    # (request.client.host) is in this list; otherwise the direct peer is used. Empty
    # (default) ⇒ never trust XFF — a client can't spoof its IP past an untrusted hop.
    trusted_proxies: str = ""

    # Request/response budgets (§8). Every request body is capped — by Content-Length
    # up front, and by counting streamed bytes for chunked bodies — so a renderer key
    # cannot hand the node an arbitrarily large `variables` payload. 1 MiB is generous:
    # renders carry a few KB and the largest shipped template is well under 10 KB. Set
    # the reverse proxy's body limit to match. Rendered output is capped too: a template
    # looping over a caller-supplied list can otherwise emit tens of MB from a tiny
    # request; over the cap renders fail as a 422 render error, never a 500.
    max_request_bytes: int = Field(default=1_048_576, ge=4096)
    max_render_bytes: int = Field(default=2_097_152, ge=1024)

    @model_validator(mode="after")
    def _cross_field(self) -> "Settings":
        # Fail at BOOT with the actual problem, not at 3am with a tight loop or a
        # cookie that never arrives. These are the combinations users actually get
        # wrong; single-field bounds live on the fields above.
        if self.mode == "serve" and not self.database_url.startswith("postgres"):
            raise ValueError("serve mode requires a Postgres INCANT_DATABASE_URL")
        if self.control_poll_seconds < 0.1:
            raise ValueError("INCANT_CONTROL_POLL_SECONDS below 0.1s is a busy-loop, not a poll")
        return self

    def trusted_proxy_set(self) -> set[str]:
        return {p.strip() for p in self.trusted_proxies.split(",") if p.strip()}

    def observed_flags_exclude_set(self) -> set[str]:
        return {f.strip() for f in self.observed_flags_exclude.split(",") if f.strip()}

    def repo_dir(self) -> Path:
        return Path(self.repo_path).resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _load_file_secrets()
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override settings (tests)."""
    global _settings
    _settings = settings
