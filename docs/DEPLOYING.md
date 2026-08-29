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
