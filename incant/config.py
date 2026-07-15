"""Bootstrap configuration (pydantic-settings). Only the essentials to start up."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INCANT_", env_file=".env", extra="ignore")

    # Storage. Postgres is the control plane — Incant is multi-user from the ground
    # up, and a real connection pool is where concurrency bugs surface (SQLite's
    # serialized writer hides them). Point this at your Postgres; docker-compose
    # wires the bundled `db` service automatically.
    database_url: str = "postgresql+psycopg://incant:incant@localhost:5432/incant"
    repo_path: str = "./var/repo"          # canonical git repository (bare)

    # Serving
    default_environment: str = "prod"
    mode: str = "full"                     # full | serve

    # Bind
    host: str = "0.0.0.0"
    port: int = 8080

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
    auth_ttl: float = 5.0

    # Control-plane poll interval (seconds). The serving hot path never reads the DB
    # itself (§8 "No DB per request"; §10 "the DB is never on the per-request path"); a
    # background loop polls every this-many seconds and pulls targeting bumps + the
    # TTL-driven auth reload into memory. This is the poll fallback for the design's
    # Postgres LISTEN/NOTIFY (§7), which names a 2s poll — so a targeting change
    # (including "make live") propagates to every replica in < 2 s.
    control_poll_seconds: float = 2.0

    # Failed-auth throttling: per-client-IP sliding window over FAILED bearer auths.
    # After `limit` failures within `window` seconds, that IP gets 429 (Retry-After)
    # until the window drains. Successful auth is never throttled. limit=0 disables.
    auth_throttle_limit: int = 20
    auth_throttle_window: float = 60.0

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

    def trusted_proxy_set(self) -> set[str]:
        return {p.strip() for p in self.trusted_proxies.split(",") if p.strip()}

    def repo_dir(self) -> Path:
        return Path(self.repo_path).resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override settings (tests)."""
    global _settings
    _settings = settings
