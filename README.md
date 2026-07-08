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
docker compose exec incant uv run incant seed   # example dataset (prints a renderer key)
```

Open <http://localhost:8080> for the UI. The bootstrap admin key is
`incant_sk_dev_admin` (override with `INCANT_BOOTSTRAP_ADMIN_KEY`).

To run outside Docker, point `INCANT_DATABASE_URL` at a Postgres you manage:

```bash
uv sync
INCANT_DATABASE_URL=postgresql+psycopg://incant:incant@localhost:5432/incant \
  uv run incant serve
```

Render a prompt:

```bash
curl -s localhost:8080/prompt/support/system \
  -H "Authorization: Bearer incant_sk_dev_admin" \
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
| `INCANT_BOOTSTRAP_ADMIN_KEY` | `incant_sk_dev_admin` | bootstrap admin API key |
