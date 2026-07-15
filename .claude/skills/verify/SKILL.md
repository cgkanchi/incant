---
name: verify
description: Build, launch, and drive Incant locally to verify a change end-to-end (server + vanilla-JS UI).
---

# Verifying Incant changes

## Launch (no Postgres/Docker needed)

The app runs fine on SQLite for verification. From a scratch dir:

```bash
export INCANT_DATABASE_URL="sqlite:///$(pwd)/incant.db"
export INCANT_REPO_PATH="$(pwd)/repo"
export INCANT_ALLOW_DEV_KEY=1 INCANT_BOOTSTRAP_ADMIN_KEY=incant_sk_dev_admin  # dev key needs explicit opt-in
uv run --project /home/cgkanchi/code/incant incant init
uv run --project /home/cgkanchi/code/incant incant seed     # example dataset; prints a renderer key
uv run --project /home/cgkanchi/code/incant incant serve --host 127.0.0.1 --port 8765  # background
```

- `uv run incant` must resolve the project — use `--project` when cwd is elsewhere.
- Admin auth: `Authorization: Bearer incant_sk_dev_admin` — but ONLY with `INCANT_ALLOW_DEV_KEY=1`; without a configured key the server generates one and prints it once at first boot. The UI has NO baked-in key: every fresh browser context lands on the sign-in card (fill `input[type=password]`, click "Sign in"). sessionStorage is per-tab — each new Playwright page must sign in again unless "Remember on this device" was checked.
- The client is split across `incant/ui/js/**` (ordered classic scripts listed in index.html); the DOM harnesses load them via `scratchpad/load-app.js`.
- Seed data: `support/system` (v2 live by Dana, v3 testing via rules, 2 unpublished edits by Sam), `support/greeting` (v2 committed, never published — the "draft, not live" case), `shared/style/language-rules`. `prod` is protected (type-to-confirm on publish/rollback), `staging` is track_tip.

## Drive the UI

It's a hash-routed SPA (`incant/ui/app.js`, no build). Playwright with system Chrome works:

```bash
uv run --with playwright --no-project python drive.py
# in the script: p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
```

Key routes: `#/prompts`, `#/p/support%2Fsystem/overview` (status hero), `.../rules` (Who sees what + targeting toggle), `.../pointers` (Publish history), `.../draft` (editor), `#/play`, `#/audit`, `#/access`.

Gotchas:
- The "technical details" disclosure state persists in localStorage (`incant_tech`) across screens/loads.
- Protected env mutations open a type-to-confirm modal: `#confirmInput` + `#confirmBtn` (disabled until the token — prompt id or env name — is typed exactly).
- The draft primary button only reads "Save edits…" when lint is clean AND the review policy is satisfied; seeded `support` needs 1 approval, so it shows "Awaiting 1 approval(s)" — not a bug.
- Capture `page.on("pageerror")` — the app has no framework, so a JS error usually kills the whole render.

## API smoke

```bash
curl -s -H "Authorization: Bearer incant_sk_dev_admin" "http://127.0.0.1:8765/mgmt/overview?environment=prod"
```

Tests (`uv run pytest`, ~1 min, SQLite) are CI's job — verification is driving the running app.

The proven flows here are now a committed, repeatable suite: `tests/browser/` (opt-in,
Playwright over system Chrome). Run it with
`INCANT_BROWSER_TESTS=1 uv run --group browser pytest tests/browser -q` — it boots its
own seeded SQLite server, so it's a fast way to re-confirm the big UI flows (sign-in,
CSRF, drafts/autosave conflict, review invalidation, targeting, publish, sign-out,
mobile/reduced-motion) after a change.
