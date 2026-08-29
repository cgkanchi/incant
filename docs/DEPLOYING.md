# Deploying Incant

One deployment = one project = one canonical repo + one Postgres database, with
**exactly one `full` node** (enforced by a boot-time advisory lock) and any number
of `INCANT_MODE=serve` replicas.

## Topology

```
                       ┌──────────────┐
   users/browsers ───► │ reverse proxy │ ◄─── services (bearer keys)
   (TLS terminates)    └──────┬───────┘
                    ┌─────────┴──────────┐
              ┌─────▼─────┐        ┌─────▼─────┐
              │ full node │        │ serve × N │   ← stateless; hydrate + follow
              │ (1, owns  │        │ (renders  │     content from a backup remote
              │ repo+jobs)│        │  only)    │
              └─────┬─────┘        └─────┬─────┘
                    └────────┬───────────┘
                        ┌────▼────┐          ┌──────────────┐
                        │Postgres │          │ git remotes  │ ◄─ backup pushes
                        └─────────┘          │ (off-site)   │    (full node)
                                             └──────────────┘
```

## Production checklist

1. **Secrets.** Set a real `INCANT_DATABASE_URL` (never the compose defaults),
   `INCANT_KEY_PEPPER` (stable, outside the DB), and either pin
   `INCANT_BOOTSTRAP_ADMIN_KEY` or capture the one printed at first boot.
2. **TLS at the proxy.** Incant speaks plain HTTP; terminate TLS in front and set
   `INCANT_ENFORCE_TLS=1` so responses carry HSTS and cookies are `Secure`. Set
   `INCANT_TRUSTED_PROXIES` to the proxy's IP(s) so throttling sees real clients.
3. **One worker per container** (the shipped CMD does this). Scale render capacity
   with `INCANT_MODE=serve` replicas, never with `--workers`.
4. **First run.** Open the UI → the setup screen creates the initial admin account
   and names the project. Do it immediately; the door closes once an account exists.
5. **Register backup remotes** (Access is admin-only; `POST /mgmt/remotes`) the
   same day. They are BOTH your off-site content durability and how serve replicas
   hydrate/follow content. Watch `incant_backup_queue_depth` / `_lag_seconds`.
6. **Postgres backups** as you would any control plane (pgdata volume / managed
   snapshots). The DB holds SHAs and state, never template text.
7. **Probes.** `/readyz` for rotation (gates on the default environment + auth
   cache), `/healthz` for liveness — it stays green under governance drift but
   names it (`drift`, `degraded_environments`).
8. **Kubernetes.** StatefulSet (or Deployment + PVC) for the full node; Deployment
   for serve replicas (they need only the cache volume — content hydrates from a
   remote); managed Postgres; `readyz` as readinessProbe.

## The backup remote, done right (deploy key click-path)

The remote is a plain bare git repo. Best practice is a **dedicated repository
with a write deploy key that only this deployment holds** — not a personal
account's credentials:

1. Create an empty private repo (GitHub/GitLab/Gitea/an internal bare repo):
   `acme/incant-backup`. Don't initialize it with a README — Incant owns the
   lineage.
2. Generate a keypair for the deployment, no passphrase:
   `ssh-keygen -t ed25519 -f incant_deploy_key -N "" -C incant-backup`.
3. Add `incant_deploy_key.pub` as a **deploy key with write access** on that one
   repo (GitHub: repo → Settings → Deploy keys → check "Allow write access").
   A deploy key is scoped to a single repository — strictly better than a bot
   account with broad access.
4. Mount the private key into the container **read-only** (orchestrator secret
   or bind mount), e.g. at `/run/secrets/incant_deploy_key`.
5. Register the remote (UI: Access → Backup remotes → Add remote, or
   `POST /mgmt/remotes`) with the ssh URL and the key's in-container path as the
   *credential path* (`auth_ref`):
   `{"url": "git@github.com:acme/incant-backup.git", "auth_ref": "/run/secrets/incant_deploy_key"}`.
   The UI pushes immediately, so you learn right away whether it's reachable.

`auth_ref` is always a **filesystem path**, never a secret: Incant stores the
path in the DB and reads the file at push time. For https remotes, `auth_ref`
points at a git credential-store file instead (one line:
`https://user:token@git.example.com`, mode 0600). Putting the token directly in
the remote URL works but is warned against at registration — the URL lives in
the database; the credential file lives with your other mounted secrets.

Host-key verification: bake the git host into a known-hosts file and point
`INCANT_KNOWN_HOSTS_PATH` at it (e.g. `ssh-keyscan github.com > known_hosts`).

## First boot against a remote (`INCANT_BOOTSTRAP_REMOTE`)

Set `INCANT_BOOTSTRAP_REMOTE` (and `INCANT_BOOTSTRAP_REMOTE_KEY`, the same kind
of credential path as `auth_ref`) and the full node's first boot — an **empty
repo volume** — clones the remote before anything else. Three outcomes:

- **Blank remote** → Incant seeds a fresh lineage and pushes it there from
  minute one. This is the recommended way to start: durability precedes content.
- **Populated Incant repo + empty database** → the content tree is **adopted**:
  prompts, versions and validation records are rebuilt from the repo. This is
  the disaster-recovery path and it is content-only — environments, targeting
  rules, users and keys live in Postgres and come back via your DB backup (the
  boot log says so loudly). The repo must contain a single top-level project;
  anything else refuses to boot.
- **Unreachable remote** → the boot **fails on purpose**. A mistyped URL must
  not silently mint a fresh lineage that would later fight the real one.

The bootstrap remote is auto-registered as a backup remote, so pushes continue
without further setup. Once the volume is populated, the setting is inert — boots
skip the clone.

## Where the repo lives

- **Full node: a durable volume.** The bare repo *is* the canonical store; give
  it the same care as `pgdata`. Running it on ephemeral disk technically works
  once a backup remote is registered — every publish pushes — but your RPO
  becomes "the backup queue at the moment of loss" (`incant_backup_queue_depth`
  is the exposure). A restart then re-clones via `INCANT_BOOTSTRAP_REMOTE`
  and recovers content by adoption, control plane from Postgres. Supported, but
  a durable volume is the design.
- **Serve replicas: ephemeral is fine.** They hydrate from a backup remote at
  boot and follow it; nothing on their disk is canonical.

## External Postgres (managed RDS/Cloud SQL/your own)

```
INCANT_DATABASE_URL=postgresql+psycopg://incant:PASS@db.internal:5432/incant?sslmode=require
```

- One **database per deployment** (not just a schema) — deployments sharing a
  server should each get their own database. The single-writer advisory lock is
  taken *per database*, so two full nodes on the same DB refuse to double-write,
  but two deployments must not share one.
- The role needs to own its database (DDL at boot: Alembic migrations) plus
  ordinary DML. No superuser, no extensions required.
- `sslmode=require` (or `verify-full` with a CA bundle) for anything off-host.
- With docker compose, drop the bundled db: `docker compose up incant --no-deps`.
- Upgrades: **full node first** (it runs migrations at boot, before readiness),
  then serve replicas.

## Secrets: what lives where

| Secret | Where it lives | How Incant gets it |
|---|---|---|
| Postgres URL/password | orchestrator secret | `INCANT_DATABASE_URL` or `INCANT_DATABASE_URL_FILE` |
| key pepper | orchestrator secret | `INCANT_KEY_PEPPER` or `INCANT_KEY_PEPPER_FILE` |
| bootstrap admin key | orchestrator secret (or one-time boot log) | `INCANT_BOOTSTRAP_ADMIN_KEY(_FILE)` |
| git ssh deploy key | file mounted ro | `auth_ref` / `INCANT_BOOTSTRAP_REMOTE_KEY` = its **path** |
| git https token | credential-store file mounted ro | same — the path |
| user passwords / API keys | Postgres, hashed (scrypt / peppered SHA-256) | n/a |

Every `INCANT_*_FILE` variant reads the value from the named file at boot —
use them with Docker/Kubernetes secrets so nothing sensitive sits in `ps e`
output or compose files. **The database never stores a git credential**, only
filesystem paths to them.

## Restore drill (rehearse once before you need it)

Losing the **repo volume**: content = any backup remote, in standard git.

```bash
# 1. stop the full node        2. restore the repo from a remote
git clone --mirror ssh://backup-host/incant.git /var/lib/incant/repo
# 3. start the full node — boot reconciles git↔DB and re-warms; /readyz goes
#    green when the default environment serves from memory again.
```

Losing **Postgres**: restore the DB backup, start the full node. Content was never
in the DB; the reconcile sweep reports any drift between the restored control
plane and the repo (metrics + /healthz) without auto-repairing.

Losing **both**: restore Postgres backup + clone a remote. Anything committed
after the DB backup shows up as reconcile drift for a human to adjudicate.

## Upgrades

Migrations run automatically at full-node boot, before readiness. Serve replicas
never migrate — upgrade the full node first, then the replicas.
