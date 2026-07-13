# Incant

A prompt management platform — **git for content, Jinja2 for rendering, flag-based
targeting for who sees what**. The LaunchDarkly parallel for prompts: non-devs
author, target, test, and iterate; services and agents render.

See [DESIGN.md](./DESIGN.md) for the full design. This repository implements it.

## Three planes

- **Git is the content store.** One canonical bare repo, Incant-owned, one file per
  version (`support/system/v2.j2`). Per-file history, immutable SHAs, diffs.
- **Postgres/SQLite is the control plane.** Targeting rules, segments, live pointers,
  review state, RBAC, audit — SHAs only, never template text.
- **Memory is the serving plane.** Compiled templates + rule snapshots; the render
  path touches no git, no disk, no DB.

## Quick start (Docker + Postgres)

Incant is multi-user from the ground up: the control plane runs on **Postgres**, not
SQLite (SQLite's serialized writer masks the concurrency this app is built for). The
supported way to run it is Docker Compose, which brings up the app plus Postgres:

```bash
docker compose up -d --build
docker compose logs incant | grep -A6 "bootstrap admin key"   # grab the generated admin key
docker compose exec incant uv run incant seed   # example dataset (prints a renderer key)
```

Open <http://localhost:8080> for the UI. On first boot with no admin configured,
Incant **generates a strong random bootstrap admin key and prints it once** to the
logs (`incant_sk_…`) — save it; it is not shown again. Pin your own key by setting
`INCANT_BOOTSTRAP_ADMIN_KEY`. The well-known `incant_sk_dev_admin` is refused unless
you also set `INCANT_ALLOW_DEV_KEY=1` (local/test only). `uv run incant seed` prints
its own scoped renderer key for the serving examples below.

To run outside Docker, point `INCANT_DATABASE_URL` at a Postgres you manage:

```bash
uv sync
INCANT_DATABASE_URL=postgresql+psycopg://incant:incant@localhost:5432/incant \
  uv run incant serve
```

Render a prompt:

```bash
curl -s localhost:8080/prompt/support/system \
  -H "Authorization: Bearer $INCANT_RENDERER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flags":{"user_id":"u_12"},"variables":{"customer_name":"Acme","history":[]}}'
```

## Layout

```
incant/
├── core/        # pure library: evaluator, sandboxed renderer, variable inference,
│                #   include resolution — no I/O, exhaustively unit-tested
├── gitstore/    # canonical bare repo (git plumbing), commit + validation pipeline,
│                #   content-addressed ContentStore for the hot path
├── registry/    # version registry, drafts, review policy, refinements, test contexts
├── targeting/   # rules, segments, append-only pointers, defaults, kills, snapshots
├── server/      # FastAPI: serving API, mgmt API, API-key RBAC, audit, metrics
├── ui/          # single-page UI ("Signal" direction), served as static assets
├── service.py   # AppContext: wiring + snapshot cache + serve/evaluate hot path
├── models.py    # control-plane ORM (SQLAlchemy) — SHAs and state, no content
└── seed.py      # the design's example dataset
```

## The core loop

Commits are cheap and change nothing; **serving changes are pointer moves and are
governed**. Tweak a live version → review → commit (validated, lands as a new SHA on
`vN.j2`) → target the tip to a cohort (`v2 @ tip`) → widen → **make live** (advance
the append-only pointer) → drop the rule. The tip↔live gap is the testing window.

Every render reports the resolved version **and SHA** of the prompt and every included
fragment — `versions` map + `rules_version` is the reproducibility tuple.

## Testing

Pure-logic tests run anywhere. The DB-touching tests default to a throwaway SQLite
file for a quick local pass, but the real target is Postgres — point
`INCANT_TEST_DATABASE_URL` at one (the compose `db` publishes `localhost:5432`) to run
the full suite, including the concurrency tests that prove no lost `rules_version`
bumps under parallel writes:

```bash
uv run pytest                                    # quick local pass (SQLite)
INCANT_TEST_DATABASE_URL=postgresql+psycopg://incant:incant@localhost:5432/incant \
  uv run pytest                                  # full suite incl. concurrency (67 tests)
```

Tests drop and recreate all tables, so they are **isolated to a dedicated
`<db>_test` database**: the URL above is redirected to `incant_test` (created on
demand) and the app's `incant` database is never touched. A safety rail refuses to
reset any Postgres database whose name doesn't end in `_test`.

## Config (`INCANT_*` env vars)

| Var | Default | Meaning |
|---|---|---|
| `INCANT_DATABASE_URL` | `postgresql+psycopg://incant:incant@localhost:5432/incant` | control plane (Postgres) |
| `INCANT_REPO_PATH` | `./var/repo` | canonical bare git repo |
| `INCANT_DEFAULT_ENVIRONMENT` | `prod` | default serving environment |
| `INCANT_MODE` | `full` | `full` (API + mgmt + UI) or `serve` (read-only) |
| `INCANT_BOOTSTRAP_ADMIN_KEY` | *(empty)* | bootstrap admin API key; empty ⇒ generate + print once on first boot |
| `INCANT_ALLOW_DEV_KEY` | *(unset)* | set to `1` to permit the unsafe `incant_sk_dev_admin` (local/test only) |
| `INCANT_KEY_PEPPER` | *(empty)* | secret pepper for key hashing; set ⇒ new/rotated keys stored as `v2$` HMAC-SHA256, legacy keys upgraded on next auth |
| `INCANT_METRICS_TOKEN` | *(empty)* | shared bearer token that lets a principal-less Prometheus scraper read `/metrics` |
| `INCANT_ENFORCE_TLS` | `false` | emit `Strict-Transport-Security` (HSTS) — enable only when TLS terminates at a proxy in front of Incant |
| `INCANT_AUTH_TTL` | `5.0` | in-memory key-cache TTL (s); bounds revocation propagation across replicas |
| `INCANT_AUTH_THROTTLE_LIMIT` | `20` | failed bearer auths per IP per window before `429`; `0` disables |
| `INCANT_AUTH_THROTTLE_WINDOW` | `60.0` | sliding window (s) for the failed-auth throttle |

## Security

Incant authenticates every request; there is no side door. A few operational notes:

- **TLS terminates at your proxy.** Incant speaks plain HTTP behind a reverse proxy /
  load balancer that does TLS. Set `INCANT_ENFORCE_TLS=1` there so responses carry
  HSTS (`Strict-Transport-Security`). Every response also carries a strict
  `Content-Security-Policy` (scripts are `'self'` only), `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a locked-down
  `Permissions-Policy`.
- **Keys are opaque, high-entropy bearer tokens** (`incant_sk_…`) for service-to-service
  use. They are stored hashed, never in the clear. Set `INCANT_KEY_PEPPER` to a secret
  (kept outside the DB) for defense-in-depth: new and rotated keys are stored as
  `v2$` HMAC-SHA256(pepper, key), and any legacy plain-SHA256 key is upgraded in place
  the next time it authenticates. Keep the pepper stable — changing it invalidates
  existing `v2$` hashes.
- **Key expiry and rotation.** Issue keys with an optional `expires_in_days` (expiry is
  enforced at auth). `POST /mgmt/keys/{key_id}/rotate` atomically mints a replacement
  key for the same principal and revokes the old one (audited as `key.rotate`).
- **Revocation propagation.** Revoking, rotating, or re-binding a key takes effect
  immediately on the node that made the change. On multi-replica deployments the other
  replicas pick it up within `INCANT_AUTH_TTL` (default 5s) as their in-memory key
  cache refreshes.
- **Failed-auth throttling.** Repeated failed bearer auths from one client IP
  (`INCANT_AUTH_THROTTLE_LIMIT` per `INCANT_AUTH_THROTTLE_WINDOW`) earn a `429` with
  `Retry-After` until the window drains; successful auth is never throttled. Behind a
  proxy, the first `X-Forwarded-For` hop is used as the client IP.
- **`/metrics`** requires either a valid key holding `viewer` (any scope) or the
  `INCANT_METRICS_TOKEN` bearer (for principal-less scrapers). `/healthz` and `/readyz`
  stay public — they are LB probes and return no sensitive data.
- **Roadmap — browser sessions.** Opaque API keys are for service-to-service callers.
  Interactive browser access (OIDC login + short-lived `HttpOnly`, `SameSite` session
  cookies) is planned; until then, treat UI access as key-bearing.
