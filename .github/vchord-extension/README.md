# VectorChord CNPG Extension Image

Why this exists, and the one-time setup it needs — see `.github/workflows/vchord-extension-rebuild.yaml` and `.github/scripts/vchord_extension_rebuild.py` for the automation itself.

## Why

Immich requires the VectorChord (`vchord`) Postgres extension for smart search/facial recognition. CloudNativePG's newer `Cluster.spec.postgresql.extensions` mechanism lets an extension be mounted into an *existing* Postgres cluster from a separate OCI image, without needing a whole custom base image — this is exactly how the shared `postgres` cluster (`kubernetes/apps/database/cloudnative-pg/cluster/cluster.yaml`) got pgvector-adjacent VectorChord support without a second dedicated CNPG cluster.

The catch: unlike pgvector (which CNPG publishes an official, correctly-laid-out extension image for), **no official CNPG-compatible VectorChord extension image exists**. Upstream's own `ghcr.io/tensorchord/vchord-scratch` image is *shaped* like one, but keeps the raw Debian package tree (`usr/lib/postgresql/18/lib/...`, `usr/share/postgresql/18/extension/...`) instead of the flat `/lib` + `/share/extension` layout CNPG's `extension_control_path`/`dynamic_library_path` defaults expect.

`Dockerfile` here repackages upstream's image into that expected layout — the same repackaging step CNPG's own official `pgvector` extension Dockerfile does.

## Automation

`.github/workflows/vchord-extension-rebuild.yaml` runs weekly (+ manual `workflow_dispatch`):
1. Checks `ghcr.io/tensorchord/vchord-scratch` for a newer `pg18-vX.Y.Z` tag than what's currently pinned in `cluster.yaml`.
2. If found: rebuilds this Dockerfile against the new source tag, pushes `ghcr.io/cypr0/cnpg-vchord-extension:pg18-vX.Y.Z`, bumps the pin, and opens a PR. Never auto-merges.
3. If not: no-op.

## One-time setup required

The target image (`ghcr.io/cypr0/cnpg-vchord-extension`) lives under the **personal** GHCR namespace, not this repository's own package namespace — so the workflow's default `GITHUB_TOKEN` doesn't have push rights to it out of the box, even with `packages: write` granted. Two one-time steps, both on the package's own settings page (`github.com/users/cypr0/packages/container/cnpg-vchord-extension/settings`):

1. **Visibility → Public.** It's just repackaged open-source binaries — no reason to keep it private, and this avoids needing a separate pull-secret in the cluster for CNPG to fetch it.
2. **Manage Actions access → Add repository → `cypr0/k8s-ops` → Write.** This is what actually lets the workflow's `GITHUB_TOKEN` push new versions here — without it, the rebuild workflow will fail at the `docker push` step with a permission error.

## A version bump can still need a manual step

Bumping the pinned image version updates which `.so`/`.control`/upgrade-SQL files are mounted, but CNPG does **not** run `ALTER EXTENSION vchord UPDATE` automatically on any database using it. If Immich's search/indexing behaves oddly right after a version bump lands, connect to `immichdb` and run `ALTER EXTENSION vchord UPDATE;` by hand.
