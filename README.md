# Incant

A prompt management platform — **git for content, Jinja2 for rendering, flag-based
targeting for who sees what**. The LaunchDarkly parallel for prompts: non-devs
author, target, test, and iterate; services and agents render.

See [DESIGN.md](./DESIGN.md) for the full design. This repository implements it.

## Three planes

- **Git is the content store.** One canonical bare repo, Incant-owned, one file per
  version (`support/system/v2.j2`). Per-file history, immutable SHAs, diffs.
- **Postgres is the control plane.** Targeting rules, segments, live pointers,
  review state, RBAC, audit — SHAs only, never template text.
- **Memory is the serving plane.** Compiled templates + rule snapshots; the render
  path is **memory-first** — the common case touches no git, no disk, no DB.

Serving is memory-*first*, not memory-*only*: the hot path reads from an in-memory,
content-addressed cache that is eagerly warmed at boot and on every commit/targeting
change, so a warm node answers renders entirely from memory. It falls through to a
**single git read** only in the uncommon cases — a cache miss on cold or LRU-evicted
content, or an old validated `pin` for a SHA no longer in the working set — after which
that blob is cached and subsequent renders are back in memory. Postgres is never on the
per-request path (targeting is served from the snapshot). The
`incant_content_git_reads_total` metric counts those fall-throughs; on a healthy node it
sits at ~0.

## Quick start (Docker + Postgres)

Incant is multi-user from the ground up: the control plane runs on **Postgres**, not
SQLite (SQLite's serialized writer masks the concurrency this app is built for). The
supported way to run it is Docker Compose, which brings up the app plus Postgres:

```bash
docker compose up -d --build
docker compose logs incant | grep -A6 "bootstrap admin key"   # grab the generated admin key
docker compose exec incant uv run incant seed   # example dataset (prints a renderer key)
```

Open <http://localhost:8080> — the **first-run screen creates the initial admin
account** (name, email, password; no API key involved). Do it right after boot: the
setup door closes the moment the first account exists. From there, invite everyone
else from **Access** — each person gets a single-use link (valid 7 days) to pick
their own password. People sign in with email & password; **API keys are for
machines and developer access**, issued by admins in Access.

For headless/API-only use, Incant also **generates a bootstrap admin key and prints
it once** to the logs on first boot (`incant_sk_…`) — save it; it is not shown
again. Pin your own by setting `INCANT_BOOTSTRAP_ADMIN_KEY`. The well-known
`incant_sk_dev_admin` is refused unless you also set `INCANT_ALLOW_DEV_KEY=1`
(local/test only). `uv run incant seed` prints its own scoped renderer key for the
serving examples below.

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
Discovery for the same credential: `GET /prompts?environment=…` lists what the key can
render (ids, descriptions, versions, defaults, labels — renderer-scoped), and
`GET /prompt/{id}/spec` says what to pass — variables merged across the versions
targeting can currently serve, plus the flags active rules consult.

Or use the Python SDK ([`sdk/python`](sdk/python/README.md)):

```python
from incant_sdk import Incant

client = Incant()   # INCANT_URL, INCANT_API_KEY, INCANT_ENVIRONMENT
r = client.render("support/system", flags={"user_id": "u_12"},
                  variables={"customer_name": "Acme", "history": []})
r.text     # the rendered prompt
r.pin      # log it beside the LLM call; pass back to replay this render exactly

client.prompts()                   # what this key can render
client.prompt("support/system")    # variables to pass + flags targeting consults
```

AI agents get the same reach through the MCP server ([`mcp/python`](mcp/python/README.md)) —
curated tools for authoring, testing, publishing, and targeting under the API
key's roles — paired with the agent skills in [`skills/`](skills) that encode
the workflows and guardrails (`incant-authoring`, `incant-release`).

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
fragment — `versions` map + `rules_version` is the reproducibility tuple. Feed it back
as `pin` to replay: `pin.versions` replays exact content (only validated SHAs — a pin
can never surface draft or validation-failed content), `pin.rules_version` replays the
recorded targeting state of that moment (DESIGN.md §9 for the precise semantics).

## Backups and replicas

The canonical repo's off-site durability is **backup remotes** (DESIGN.md §6): register
any `git push`-able URL at `POST /mgmt/remotes` (admin; an optional `auth_ref` names a
push-only ssh deploy key, and https URLs may embed a token — redacted in every
response). A background pass force-pushes the complete ref set to every enabled remote
that is behind `main`, every `INCANT_BACKUP_POLL_SECONDS`; `GET /mgmt/remotes` shows
each remote's pending-commit queue and lag, and `POST /mgmt/remotes/{id}/push` pushes
one immediately. `incant_backup_queue_depth` and `incant_backup_lag_seconds{remote}`
bound the exposure window on a dashboard.

The same remotes distribute content to **serve replicas** (`INCANT_MODE=serve`): a
replica with an empty volume hydrates itself by mirror-cloning an enabled remote, then
follows it with a mirror-fetch every `INCANT_CONTENT_FETCH_SECONDS` — so a "make live"
that references a fresh commit finds its content on every replica within one interval.
Sharing the full node's volume works too (set the interval to 0).

## Testing

The suite runs against Postgres — the only control plane there is. There is
deliberately no SQLite fallback: it enforced no foreign keys, took a `create_all`
shortcut past the Alembic migrations, and serialized writes — three ways a green
local run could lie about production. Bring up the bundled `db` once and `pytest`
finds it by default:

```bash
docker compose up -d db
uv run pytest                # full suite, incl. the concurrency tests
```

Point `INCANT_TEST_DATABASE_URL` at a different Postgres if you manage your own.
If no server answers, the suite exits immediately with that exact instruction
instead of failing test-by-test.

Tests drop and recreate all tables, so they are **isolated to a dedicated
`<db>_test` database**: the URL is redirected to `incant_test` (created on
demand) and the app's `incant` database is never touched. A safety rail refuses to
reset any database whose name doesn't end in `_test`.

### Browser end-to-end tests (opt-in)

`tests/browser/` drives the real UI with Playwright over your **system Chrome**
(headless) against a server it boots itself on a dedicated `incant_browser_test`
database — wiped per run and rebuilt through the real Alembic migrations, so the
browser suite also exercises the production DDL path. It's opt-in — the
`browser` dependency group and the `INCANT_BROWSER_TESTS=1` flag are both required, so
the default `uv run pytest` above is untouched (the suite is skipped, or not collected
at all when Playwright is absent):

```bash
uv sync --group browser                                   # once: install Playwright
INCANT_BROWSER_TESTS=1 uv run --group browser pytest tests/browser -q
```

It uses `/usr/bin/google-chrome`; point `INCANT_BROWSER_CHROME` at another
Chrome/Chromium binary if yours lives elsewhere.

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
| `INCANT_BACKUP_POLL_SECONDS` | `15.0` | backup-push interval to enabled remotes (full mode); `0` disables the loop |
| `INCANT_CONTENT_FETCH_SECONDS` | `30.0` | serve-replica content mirror-fetch interval; `0` disables (shared-volume deployments) |
| `INCANT_BACKUP_TIMEOUT_SECONDS` | `60.0` | timeout for one remote git operation (push/fetch/clone) |
| `INCANT_KNOWN_HOSTS_PATH` | *(empty)* | pinned known_hosts file for ssh remotes; empty ⇒ ssh defaults |
| `INCANT_BOOTSTRAP_REMOTE` | *(empty)* | git URL cloned on first boot when the repo volume is empty (blank remote ⇒ fresh start pushed there; populated Incant repo ⇒ content adopted; unreachable ⇒ boot fails). Auto-registered as a backup remote |
| `INCANT_BOOTSTRAP_REMOTE_KEY` | *(empty)* | credential **path** for the bootstrap remote — an ssh private key or an https credential-store file mounted into the container |
| `INCANT_DATABASE_URL_FILE`, `INCANT_KEY_PEPPER_FILE`, `INCANT_BOOTSTRAP_ADMIN_KEY_FILE` | *(empty)* | read the corresponding value from a file (Docker/K8s secrets) instead of the environment |
| `INCANT_AUTH_TTL` | `5.0` | in-memory key-cache TTL (s); bounds revocation propagation across replicas |
| `INCANT_AUTH_THROTTLE_LIMIT` | `20` | failed bearer auths per IP per window before `429`; `0` disables |
| `INCANT_AUTH_THROTTLE_WINDOW` | `60.0` | sliding window (s) for the failed-auth throttle |
| `INCANT_TRUSTED_PROXIES` | *(empty)* | comma-separated proxy IPs whose `X-Forwarded-For` is trusted for the client IP; empty ⇒ never trust XFF (use the direct peer) |

## Deploying

Production topology, the hardening checklist, and the restore drill live in
[docs/DEPLOYING.md](./docs/DEPLOYING.md). The bundled docker-compose is a
development convenience, not a production posture. One deployment hosts exactly
**one project** (named at first-run setup); run another instance for another
project.

## Security

Incant authenticates every request; there is no side door. A few operational notes:

- **TLS terminates at your proxy.** Incant speaks plain HTTP behind a reverse proxy /
  load balancer that does TLS. Set `INCANT_ENFORCE_TLS=1` there so responses carry
  HSTS (`Strict-Transport-Security`). Every response also carries a strict
  `Content-Security-Policy` (scripts are `'self'` only), `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a locked-down
  `Permissions-Policy`.
- **People sign in with email & password.** Accounts are created by the first-run
  setup screen or by admin invites (single-use links, shown once, 7-day expiry —
  the same mechanism doubles as password reset). Passwords are hashed with
  **scrypt** (per-user salt, self-describing parameters, transparently upgraded on
  sign-in if the work factor is raised); policy is NIST-style length-only (10+).
  Sign-in failures share the API's per-IP throttle and never reveal whether an
  email exists. Changing a password signs out every other session; disabling an
  account is immediate and total (sessions deleted, keys revoked, invites dead).
- **Browser sessions (the UI's door).** Interactive UI access never keeps a credential
  in JS-readable storage. The browser exchanges a password (or, for machine access, a
  key) once at `POST /auth/session` for a
  server-side session, delivered as an `HttpOnly`, `SameSite=Strict`, `Path=/` cookie
  (`incant_session`) — marked `Secure` when TLS is enforced or the request is https, and
  persistent (`Max-Age`) only for "remember me" (30 days; 12 hours otherwise). Only the
  token's hash is stored server-side (same hashing as keys). Because cookies ride along
  automatically, cookie-authenticated **mutations** (any non-GET) must echo the session's
  CSRF token in an `X-Incant-CSRF` header (returned by `POST`/`GET /auth/session`);
  mismatch/absent ⇒ `403`. Bearer (header) auth is CSRF-immune and needs no such header.
  `DELETE /auth/session` (CSRF-guarded) signs out — it deletes the row and clears the
  cookie. Sessions are control-plane only: the serving hot path is bearer-only and stays
  memory-first. Expired sessions are swept at startup and then hourly.
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
  `Retry-After` until the window drains; successful auth is never throttled.
- **Client IP behind a proxy.** `X-Forwarded-For` is honored (its first hop taken as the
  client IP) **only** when the direct peer is a listed `INCANT_TRUSTED_PROXIES` address;
  otherwise the direct peer is used. The default trusts nothing, so a client can't spoof
  its IP past an untrusted hop — set `INCANT_TRUSTED_PROXIES` to your load balancer's IP(s)
  when you run behind one, so per-IP throttling keys off the real client.
- **`/metrics`** requires either a valid key holding `viewer` (any scope) or the
  `INCANT_METRICS_TOKEN` bearer (for principal-less scrapers). `/healthz` and `/readyz`
  stay public — they are LB probes and return no sensitive data.
- **Roadmap — SSO.** Local email+password accounts are the human door today; API keys
  remain the service-to-service mechanism. Optional OIDC/SSO login (minting the same
  server-side sessions for the same principals) is a possible future addition.
