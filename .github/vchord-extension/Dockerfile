# Repackages ghcr.io/tensorchord/vchord-scratch (upstream's own VectorChord
# extension image) into the flat /lib + /share/extension layout CNPG's
# Cluster.spec.postgresql.extensions mechanism expects. There is no official
# CNPG-compatible VectorChord extension image upstream (unlike pgvector,
# which CNPG publishes directly) - upstream's own image keeps the raw Debian
# package tree (usr/lib/postgresql/<ver>/lib/..., usr/share/postgresql/<ver>/
# extension/...), which CNPG's default extension_control_path/
# dynamic_library_path (/extensions/<name>/share, /extensions/<name>/lib)
# won't find. Mirrors cloudnative-pg/postgres-extensions-containers' own
# pgvector Dockerfile's repackaging approach.
#
# Rebuilt automatically by ../workflows/vchord-extension-rebuild.yaml
# whenever upstream publishes a new pg18 tag - see that workflow for how
# SOURCE_TAG/PG_MAJOR get resolved and passed as build args.
ARG PG_MAJOR=18
ARG SOURCE_TAG=pg18-v1.1.1
FROM ghcr.io/tensorchord/vchord-scratch:${SOURCE_TAG} AS source

FROM scratch
ARG PG_MAJOR
COPY --from=source /usr/lib/postgresql/${PG_MAJOR}/lib/vchord* /lib/
COPY --from=source /usr/share/postgresql/${PG_MAJOR}/extension/vchord* /share/extension/
USER 65532:65532
