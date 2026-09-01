---
name: verify
description: Build, launch, and drive Incant locally to verify a change end-to-end (server + vanilla-JS UI).
---

# Verifying Incant changes

## Launch (Postgres via the bundled compose db)

The control plane is Postgres-only (no SQLite path — it hid FK/migration/concurrency
differences). Use the compose `db` with a scratch database so verification never
touches the app's `incant` DB. From a scratch dir:

```bash
docker compose -f /home/cgkanchi/code/incant/docker-compose.yaml up -d db
docker compose -f /home/cgkanchi/code/incant/docker-compose.yaml exec -T db \
  psql -U incant -d postgres -c 'DROP DATABASE IF EXISTS incant_verify' \
       -c 'CREATE DATABASE incant_verify'
export INCANT_DATABASE_URL="postgresql+psycopg://incant:incant@localhost:5432/incant_verify"
export INCANT_REPO_PATH="$(pwd)/repo"
export INCANT_ALLOW_DEV_KEY=1 INCANT_BOOTSTRAP_ADMIN_KEY=incant_sk_dev_admin  # dev key needs explicit opt-in
uv run --project /home/cgkanchi/code/incant incant init     # builds the schema via Alembic
uv run --project /home/cgkanchi/code/incant incant seed     # example dataset; prints a renderer key
uv run --project /home/cgkanchi/code/incant incant serve --host 127.0.0.1 --port 8765  # background
```

- `uv run incant` must resolve the project — use `--project` when cwd is elsewhere.
- Admin auth: `Authorization: Bearer incant_sk_dev_admin` — but ONLY with `INCANT_ALLOW_DEV_KEY=1`; without a configured key the server generates one and prints it once at first boot. The UI has NO baked-in key: a fresh browser context lands on the signed-out card — **first-run setup** (create-admin form) when no user accounts exist, email+password otherwise. To sign in with the dev key, click the API-key mode link (`[data-act="signinMode"][data-mode="key"]`), fill `#signinKey`, click `#signinBtn` — exactly what `tests/browser/conftest.signin()` does. Cookies are per-context; a new Playwright context must sign in again unless "Stay signed in for 30 days on this device" (`#signinRemember`) was checked.
- The client is split across `incant/ui/js/**` (ordered classic scripts listed in `index.html`; `js/main.js` is the router/entry point).
- Seed data: `support/system` (v2 live by Dana, v3 testing via rules, 2 unpublished edits by Sam), `support/greeting` (v2 committed, never published — the "draft, not live" case), `support/style/language-rules`, `support/escalation/triage`. `prod` is protected (type-to-confirm on publish/rollback), `staging` is track_tip.

## Drive the UI

It's a hash-routed SPA (`incant/ui/js/main.js` + `js/screens/*`, no build). Playwright with system Chrome works:

```bash
uv run --with playwright --no-project python drive.py
# in the script: p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
```

Key routes: `#/prompts`, `#/p/support%2Fsystem/overview` (status hero), `.../rules` (Who sees what + targeting toggle), `.../pointers` (Publish history), `.../draft` (editor), `#/play`, `#/audit`, `#/access`.

Gotchas:
- The "technical details" disclosure state persists in localStorage (`incant_tech`) across screens/loads.
- Protected env mutations open a type-to-confirm modal (disabled until the token — prompt id or env name — is typed exactly). The generic modal is `#confirmInput` + `#confirmBtn`; the publish screen has its own (`#publishConfirm` + `[data-btn="publishBtn"]`, as `tests/browser/test_browser.py` drives it); env rename uses `#renEnvConfirm`.
- The draft primary button only reads "Save edits…" when lint is clean AND the review policy is satisfied; seeded `support` needs 1 approval, so it shows "Awaiting 1 approval(s)" — not a bug.
- Capture `page.on("pageerror")` — the app has no framework, so a JS error usually kills the whole render.

## API smoke

```bash
curl -s -H "Authorization: Bearer incant_sk_dev_admin" "http://127.0.0.1:8765/mgmt/overview?environment=prod"
```

Tests (`uv run pytest`, needs the compose `db` up) are CI's job — verification is
driving the running app.

The proven flows here are now a committed, repeatable suite: `tests/browser/` (opt-in,
Playwright over system Chrome). Run it with
`INCANT_BROWSER_TESTS=1 uv run --group browser pytest tests/browser -q` — it boots its
own seeded server on a dedicated `incant_browser_test` Postgres database (wiped and
rebuilt through the real migrations), so it's a fast way to re-confirm the big UI flows (sign-in,
CSRF, drafts/autosave conflict, review invalidation, targeting, publish, sign-out,
mobile/reduced-motion) after a change.
