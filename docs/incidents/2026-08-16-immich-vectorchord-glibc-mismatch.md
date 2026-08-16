# Immich VectorChord extension-image GLIBC mismatch

> **Date**   2026-08-16
> **Severity**  SEV3 (caught before real user impact — the shared cluster's primary never rolled)
> **Apps affected**  cloudnative-pg (shared `postgres` cluster), immich

## Summary
While onboarding Immich, the first design used a dedicated CNPG Postgres cluster for the VectorChord (`vchord`) extension it needs for smart search/facial recognition, since the shared cluster's image doesn't have it. Per operator feedback, this was reworked to add `vchord` to the **shared** `postgres` cluster instead, the same way Open WebUI's pgvector RAG database was added — using CNPG's `Cluster.spec.postgresql.extensions` mechanism (OCI-image-mounted extensions, supported since CNPG operator 1.27, confirmed available on this cluster's operator 1.30 / Kubernetes 1.36.3).

No official CNPG-compatible VectorChord extension image exists upstream, so a corrected one was built by hand (repackaging `ghcr.io/tensorchord/vchord-scratch` into CNPG's expected flat `/lib` + `/share/extension` layout, mirroring CNPG's own official pgvector extension Dockerfile) and pushed to `ghcr.io/cypr0/cnpg-vchord-extension:pg18-v1.1.1`.

Applying this to the shared cluster (`shared_preload_libraries: vchord.so` + the extension mount) triggered the expected rolling restart — and both replicas immediately crash-looped:

```
FATAL: could not load library "/extensions/vchord/lib/vchord.so": /lib/x86_64-linux-gnu/libc.so.6:
version `GLIBC_2.33' not found (required by /extensions/vchord/lib/vchord.so)
```

## Root cause
This cluster's shared Postgres image, `ghcr.io/cloudnative-pg/postgresql:18.6`, is built on **Debian 11 Bullseye** (confirmed live: `ldd --version` → glibc 2.31, `/etc/os-release` → `PRETTY_NAME="Debian GNU/Linux 11 (bullseye)"`). The VectorChord binary (built by upstream against a newer Debian) requires **GLIBC 2.33+**. CNPG's own official pgvector extension image is explicitly built against a `-trixie` (Debian 13) base for exactly this reason — extension images need a base OS compatible with the *consuming* cluster's own glibc, not just a matching Postgres major version.

The rolling restart reached both replicas (`postgres-2`, `postgres-3`) before the mismatch was caught — both crash-looped repeatedly. **The primary (`postgres-1`) never rolled** — CNPG's own rolling-update sequencing update the replicas first, and won't proceed to the primary while replicas are unhealthy — so there was no actual outage for any app using the shared cluster (Nextcloud, Paperless, Authentik, Open WebUI, Firecrawl, portfolio all kept working throughout, confirmed live afterward).

## Resolution
Reverted `shared_preload_libraries`/`postgresql.extensions` on the shared cluster entirely (commit via PR #86, merged same day) — both replicas recovered cleanly once the config was removed, cluster returned to `Cluster in healthy state` with all 3 instances healthy.

**The actual fix turned out not to need VectorChord at all.** Immich's own docs (`docs.immich.app/install/environment-variables`) state `DB_VECTOR_EXTENSION` accepts either `vectorchord` **or** `pgvector` — and plain pgvector is already bundled directly in this cluster's `postgresql:18.6` image (confirmed live: `/usr/lib/postgresql/18/lib/vector.so` already present), the same reason Open WebUI's RAG database needed no extension-image mechanism at all. Switched Immich's `DB_VECTOR_EXTENSION` to `"pgvector"` and its `Database` CRD's `extensions` list to `vector`/`cube`/`earthdistance` (all three already natively available) — no extension image, no GLIBC risk, no custom package to maintain.

The custom `ghcr.io/cypr0/cnpg-vchord-extension` package and its auto-rebuild GitHub Actions workflow were deprovisioned as part of this fix.

## What would have been needed to actually use VectorChord
For the record, since this may come up again for a future app: getting VectorChord itself working on this cluster's shared Postgres would need either (a) rebuilding the extension against a Bullseye-compatible glibc target — untested, no evidence upstream supports this — or (b) switching the shared cluster's base image to a newer Debian variant (Bookworm/Trixie). Option (b) carries a real, separate risk that wasn't part of this incident but is worth flagging: **changing a running Postgres cluster's underlying glibc can silently corrupt indexes built on collation-dependent text columns** — a well-documented PostgreSQL/glibc upgrade hazard. Not something to attempt without a planned maintenance window and a full reindex pass, and out of scope for what Immich actually needed.

## Timeline
- Dedicated-cluster design proposed and built (PR #84 v1).
- Reworked to shared-cluster + extension-image per feedback (PR #84 v2, merged).
- Extension image built, verified byte-for-byte against the real image contents before use.
- Applied to shared cluster — both replicas crash-looped within ~2 minutes.
- Root cause (GLIBC mismatch) identified directly from pod logs within a few minutes.
- Reverted (PR #86), cluster confirmed fully healthy — primary never rolled, no outage.
- Root fix identified (`pgvector` instead of `vectorchord`) and applied same day.

## TODOs / unknowns
- Whether VectorChord will ever be worth revisiting for Immich (better search relevance/performance than plain pgvector, per its own marketing) — not evaluated; plain pgvector's search quality for Immich's actual usage hasn't shown a problem.
- No monitoring/alert exists that would have caught this faster than direct `kubectl get pods` polling — worth considering for future CNPG rolling-restart changes given how the failure mode (crash-loop) could plausibly have reached the primary if response had been slower.
