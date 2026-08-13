#!/usr/bin/env python3
"""Cluster health watchdog for hermes-agent's `homelab-waechter` cron job.

Runs via `hermes cron create --no-agent --script` -- no LLM involved at
all, since every check here is a deterministic pass/fail, not something
needing judgment. Per hermes's own no-agent convention: empty stdout is
treated as silent (nothing delivered); any stdout gets delivered
verbatim. So this script prints NOTHING when the cluster is clean, and
a compact plain-text report otherwise.

Uses the `hermes-agent` ServiceAccount's own read-only `view` ClusterRole
(+ a separate Nodes-view ClusterRole, see rbac.yaml) via the `kubectl`
binary already installed by the tools-install init container -- no
kubeconfig needed, kubectl auto-detects in-cluster config from the
mounted SA token. Confirmed live that this SA can list Flux
Kustomizations/HelmReleases and cert-manager Certificates: several
operators' CRDs carry the `rbac.authorization.k8s.io/aggregate-to-view`
label, so the built-in `view` role covers more than just core/apps/batch.

Deliberately does NOT check Job/CronJob failures: this cluster has a
few known-flaky retry-based Jobs (firecrawl's nuq-cleanup/nuq-reaper)
that fail and succeed on retry as part of normal operation -- surfacing
every transient Job failure would make this watchdog noisy enough to
get ignored, defeating its purpose.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

KUBECTL_TIMEOUT = 30
CERT_EXPIRY_WARN_DAYS = 7
BAD_WAITING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}


def kubectl_json(*args):
    try:
        result = subprocess.run(
            ["kubectl", "get", *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=KUBECTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "kubectl timeout"
    if result.returncode != 0:
        return None, result.stderr.strip().splitlines()[-1] if result.stderr else "kubectl error"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON from kubectl: {e}"


def ready_status(conditions):
    for c in conditions or []:
        if c.get("type") == "Ready":
            return c.get("status"), c.get("message", "")
    return None, ""


def check_flux(resource, label):
    data, err = kubectl_json(resource, "-A")
    if err:
        return [f"{label}: kubectl-Fehler ({err})"]
    problems = []
    for item in data.get("items", []):
        if item.get("spec", {}).get("suspend"):
            continue
        status, message = ready_status(item.get("status", {}).get("conditions"))
        if status != "True":
            ns = item["metadata"]["namespace"]
            name = item["metadata"]["name"]
            problems.append(f"{ns}/{name}: {message[:150] or 'kein Ready-Status'}")
    return problems


def check_certificates():
    data, err = kubectl_json("certificates.cert-manager.io", "-A")
    if err:
        return [f"Zertifikate: kubectl-Fehler ({err})"]
    problems = []
    now = datetime.now(timezone.utc)
    for item in data.get("items", []):
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        conditions = item.get("status", {}).get("conditions", [])
        status, message = ready_status(conditions)
        if status != "True":
            problems.append(f"{ns}/{name}: nicht Ready -- {message[:150]}")
            continue
        not_after = item.get("status", {}).get("notAfter")
        if not_after:
            try:
                expires = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                days_left = (expires - now).days
                if days_left < CERT_EXPIRY_WARN_DAYS:
                    problems.append(f"{ns}/{name}: läuft in {days_left} Tag(en) ab")
            except ValueError:
                pass
    return problems


def check_nodes():
    data, err = kubectl_json("nodes")
    if err:
        return [f"Nodes: kubectl-Fehler ({err})"]
    problems = []
    for item in data.get("items", []):
        status, _ = ready_status(item.get("status", {}).get("conditions"))
        if status != "True":
            problems.append(f"Node {item['metadata']['name']}: nicht Ready")
    return problems


def check_pods():
    data, err = kubectl_json("pods", "-A")
    if err:
        return [f"Pods: kubectl-Fehler ({err})"]
    problems = []
    for item in data.get("items", []):
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        status = item.get("status", {})
        if status.get("phase") == "Failed":
            problems.append(f"{ns}/{name}: Pod-Phase Failed")
        for cs in status.get("containerStatuses", []):
            reason = cs.get("state", {}).get("waiting", {}).get("reason")
            if reason in BAD_WAITING_REASONS:
                problems.append(f"{ns}/{name} ({cs['name']}): {reason}")
    return problems


def check_pvcs():
    data, err = kubectl_json("persistentvolumeclaims", "-A")
    if err:
        return [f"PVCs: kubectl-Fehler ({err})"]
    problems = []
    for item in data.get("items", []):
        phase = item.get("status", {}).get("phase")
        if phase != "Bound":
            ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
            problems.append(f"{ns}/{name}: Phase {phase}")
    return problems


def main() -> int:
    sections = [
        ("Flux Kustomizations", check_flux("kustomizations.kustomize.toolkit.fluxcd.io", "Kustomizations")),
        ("Flux HelmReleases", check_flux("helmreleases.helm.toolkit.fluxcd.io", "HelmReleases")),
        ("Zertifikate", check_certificates()),
        ("Nodes", check_nodes()),
        ("Pods", check_pods()),
        ("PVCs", check_pvcs()),
    ]

    total = sum(len(problems) for _, problems in sections)
    if total == 0:
        return 0  # clean run -- empty stdout is the no-agent silence convention

    print(f"Cluster-Wächter: {total} Auffälligkeit(en)")
    for label, problems in sections:
        if not problems:
            continue
        print(f"\n{label}:")
        for p in problems:
            print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
