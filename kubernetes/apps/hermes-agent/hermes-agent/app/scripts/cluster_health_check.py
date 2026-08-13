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
    """Only Ready == "False" counts as a problem. Ready == "Unknown" just
    means "still reconciling" -- completely normal right after any git push
    or during a dependency chain settling, confirmed live (a Kustomization
    sat at Unknown/"Reconciliation in progress" for several minutes with
    every underlying HelmRelease already healthy, purely because Flux's own
    post-install health check hadn't finished its wait window yet). A
    reconcile that's genuinely stuck eventually flips to an explicit False
    once Flux's own retry/health-check timeout elapses, so filtering to
    False only doesn't miss real failures -- it just stops flagging normal
    in-flight state as if it were one."""
    data, err = kubectl_json(resource, "-A")
    if err:
        return [f"{label}: kubectl-Fehler ({err})"]
    problems = []
    for item in data.get("items", []):
        if item.get("spec", {}).get("suspend"):
            continue
        status, message = ready_status(item.get("status", {}).get("conditions"))
        if status == "False":
            ns = item["metadata"]["namespace"]
            name = item["metadata"]["name"]
            problems.append(f"{ns}/{name}: {message[:150] or 'nicht Ready'}")
    return problems


def check_certificates():
    """Only real problems: Ready == False (an Unknown status, e.g. right
    after issuance, is normal and self-resolving -- same reasoning as
    check_flux above), an overdue renewal (cert-manager's own renewalTime
    has passed without notAfter moving forward), or an actually-expired
    notAfter. Ready=True with notAfter a few days out is NORMAL for
    short-lived certs (e.g. Let's Encrypt's ~90-day certs renew
    automatically well before expiry) -- alerting on days-left alone (an
    earlier version of this check did) produces a false positive on every
    single one of those, every renewal cycle."""
    data, err = kubectl_json("certificates.cert-manager.io", "-A")
    if err:
        return [f"Zertifikate: kubectl-Fehler ({err})"]
    problems = []
    now = datetime.now(timezone.utc)
    for item in data.get("items", []):
        ns, name = item["metadata"]["namespace"], item["metadata"]["name"]
        conditions = item.get("status", {}).get("conditions", [])
        status, message = ready_status(conditions)
        if status == "False":
            problems.append(f"{ns}/{name}: nicht Ready -- {message[:150]}")
            continue
        status_block = item.get("status", {})
        not_after = _parse_dt(status_block.get("notAfter"))
        renewal_time = _parse_dt(status_block.get("renewalTime"))
        if not_after and not_after < now:
            problems.append(f"{ns}/{name}: abgelaufen (notAfter {status_block['notAfter']})")
        elif renewal_time and renewal_time < now:
            problems.append(f"{ns}/{name}: Erneuerung überfällig (geplant für {status_block['renewalTime']})")
    return problems


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
