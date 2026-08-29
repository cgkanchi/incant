# Changelog

## 1.0.0 — 2026-08-29

First stable release.

**The product.** Git for content (one file per prompt version, staged publication
with crash recovery), Postgres for the control plane, memory-first serving
(p50 ~1 ms). Non-devs author, review against rendered output, test with a cohort,
and publish; services render with full reproducibility (`versions` map +
`rules_version`, replayable via `pin`).

**Highlights**
- Flag-based targeting: rules, segments, labels, coherent percentage rollouts,
  kill switches; exact environment rollback with append-only pointer history and
  tombstones; checkpointed revision storage (O(1) recent, bounded reconstruction).
- Review that judges what will be served: rendered before/after diffs per test
  context, approval policies keyed to immutable principal identity.
- Accounts: first-run setup, email+password sign-in (scrypt), single-use invite /
  reset links, immediate total disable; API keys for machine access only.
- Operations: backup remotes double as serve-replica content distribution;
  single-full-writer enforcement; per-environment readiness; credential-redacted
  remote errors; Prometheus metrics for every failure mode that matters.
- One project per deployment; environment-scoped RBAC
  (renderer < viewer < editor < operator < releaser < admin); audit log for every
  mutation.
- Deployment: `INCANT_BOOTSTRAP_REMOTE` first-boot clone (blank ⇒ fresh start,
  populated ⇒ content adoption, unreachable ⇒ refuse to boot), `_FILE`-suffixed
  secret envs, scheme-aware remote credentials (`auth_ref` is always a mounted
  file path — ssh key or https credential store; never a secret in the DB).
- First run: the default environment exists from boot; admin "Get set up"
  checklist; one-click example dataset on an empty library; backup-remote
  management in the UI (add/push/disable with live sync state).

See DESIGN.md for the architecture and §17 for deliberate divergences.
