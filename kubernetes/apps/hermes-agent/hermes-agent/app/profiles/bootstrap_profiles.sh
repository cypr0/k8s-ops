#!/bin/sh
# Seeds the specialist profiles under $HERMES_HOME/profiles/<name>/ on every
# pod start. Run by the bootstrap-config init container (deployment.yaml) as
# uid 10000, the image's runtime user that owns /opt/data.
#
# Source of truth: kubernetes/apps/hermes-agent/hermes-agent/app/profiles/,
# mounted at /profiles as <name>.config.yaml + <name>.SOUL.md pairs.
#
# - config.yaml and SOUL.md are overwritten every start (Git wins, same rule
#   as the default profile's config.yaml).
# - memories/, skills/, cron/, sessions and state.db are never touched: they
#   are the profile's own state.
# - .env is rewritten every start. Profiles served by the multiplexed gateway
#   do NOT see the container's environment (agent/secret_scope.py), so each
#   one needs its own copy of the OpenRouter key. The key comes from a
#   mounted Secret file, not an env var.
# - A profile removed from Git is NOT deleted here (its memories would go
#   with it); remove it by hand with `hermes profile delete <name>`.
#
# This file sits in its own ConfigMap with Flux substitution disabled
# (kustomization.yaml), so shell variables below are safe as written. The
# profile files in /profiles are substituted (personal names come from the
# SOPS-encrypted cluster-secrets).
set -eu

HOME_DIR=/opt/data
SRC=/profiles
KEY_FILE=/secrets/openrouter/OPENROUTER_API_KEY
API_KEY_FILE=/secrets/openrouter/API_SERVER_KEY
# Profile-specific secrets: one file per value, named <profile>__<VAR>
# (ExternalSecret hermes-agent-profile-env). Each lands only in that
# profile's .env, so e.g. the GitLab token of "isms" is invisible to the
# other profiles' secret scope.
EXTRA_DIR=/secrets/profile-env

umask 077
mkdir -p "${HOME_DIR}/profiles"

# The default profile is the Chief of Staff; its SOUL.md sits at the top of
# HERMES_HOME. Written fresh (and therefore owned by this uid, 10000) every
# start -- hermes v0.23 refuses to initialise its home when SOUL.md exists
# but is not writable for it (it was left owned by uid 1000 before).
if [ -e "${SRC}/chief-of-staff.SOUL.md" ]; then
  cp "${SRC}/chief-of-staff.SOUL.md" "${HOME_DIR}/SOUL.md.tmp"
  chmod 0644 "${HOME_DIR}/SOUL.md.tmp"
  mv "${HOME_DIR}/SOUL.md.tmp" "${HOME_DIR}/SOUL.md"
  echo "default profile (chief of staff): SOUL.md seeded"
fi

found=0
for cfg in "${SRC}"/*.config.yaml; do
  [ -e "${cfg}" ] || continue
  name=$(basename "${cfg}" .config.yaml)
  dir="${HOME_DIR}/profiles/${name}"
  mkdir -p "${dir}/memories" "${dir}/skills" "${dir}/cron" "${dir}/scripts"
  cp "${cfg}" "${dir}/config.yaml"
  if [ -e "${SRC}/${name}.SOUL.md" ]; then
    cp "${SRC}/${name}.SOUL.md" "${dir}/SOUL.md"
  fi
  printf 'OPENROUTER_API_KEY=%s\n' "$(cat "${KEY_FILE}")" > "${dir}/.env.tmp"
  # API_SERVER_KEY authenticates /p/<profile>/v1 on the shared API listener
  # (Open WebUI); a served profile only accepts its own .env's key.
  if [ -s "${API_KEY_FILE}" ]; then
    printf 'API_SERVER_KEY=%s\n' "$(cat "${API_KEY_FILE}")" >> "${dir}/.env.tmp"
  fi
  extras=0
  for f in "${EXTRA_DIR}/${name}__"*; do
    [ -e "${f}" ] || continue
    var=${f##*__}
    printf '%s=%s\n' "${var}" "$(cat "${f}")" >> "${dir}/.env.tmp"
    extras=$((extras + 1))
  done
  mv "${dir}/.env.tmp" "${dir}/.env"
  found=$((found + 1))
  echo "profile ${name}: seeded (${extras} extra secret(s))"
done

echo "${found} profile(s) seeded"
