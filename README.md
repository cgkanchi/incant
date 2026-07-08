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

## Quick start

```bash
uv sync
uv run incant seed         # seed the example dataset (prints a renderer key)
uv run incant serve        # API + UI on http://localhost:8080
```

Open <http://localhost:8080> for the UI. The bootstrap admin key is
`incant_sk_dev_admin` (override with `INCANT_BOOTSTRAP_ADMIN_KEY`).

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

```bash
uv run pytest          # 63 tests: core semantics, git store, end-to-end, HTTP API
```

## Config (`INCANT_*` env vars)

| Var | Default | Meaning |
|---|---|---|
| `INCANT_DATABASE_URL` | `sqlite:///./incant.db` | control plane (Postgres in prod) |
| `INCANT_REPO_PATH` | `./var/repo` | canonical bare git repo |
| `INCANT_DEFAULT_ENVIRONMENT` | `prod` | default serving environment |
| `INCANT_MODE` | `full` | `full` (API + mgmt + UI) or `serve` (read-only) |
| `INCANT_BOOTSTRAP_ADMIN_KEY` | `incant_sk_dev_admin` | bootstrap admin API key |
