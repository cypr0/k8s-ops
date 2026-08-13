#!/usr/bin/env python3
"""Refreshes a world-readable kubeconfig at $HOME/.kube/config, embedding
the pod's own ServiceAccount token/CA inline.

Why this exists: Tirith (the terminal tool's security sandbox) runs
LLM-issued shell commands as a dedicated, unprivileged user with no
supplementary groups (confirmed live: uid=10000, groups=10000 only).
Kubernetes' serviceAccountToken projected-volume source ignores
`defaultMode` and is hard-coded to file mode 0640 root:<fsGroup>
regardless of what the volume spec requests (a known, longstanding
kubelet limitation, confirmed by testing several defaultMode values
against this exact pod) -- so that sandboxed user can never read
/var/run/secrets/.../token directly, no matter how the volume is
declared. Runs via `hermes cron create --no-agent --script`, i.e. as
this main process itself (root, confirmed via this container's own
securityContext), which CAN read the real, auto-rotating token -- and
mirrors its current value into a kubeconfig at a path ($HOME/.kube/
config) that the sandboxed user already has full access to (it's
inside their own $HOME -- confirmed live via the topic-radar cron's
state files landing there without issue).

The `view`-only, no-Secrets ClusterRole (rbac.yaml) is the actual
security boundary; this only works around a file-permission
implementation detail, not around RBAC.
"""

import base64
import os
import sys

TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
API_SERVER = "https://kubernetes.default.svc"
KUBECONFIG_PATH = os.path.expanduser("~/.kube/config")

KUBECONFIG_TEMPLATE = """\
apiVersion: v1
kind: Config
clusters:
  - name: in-cluster
    cluster:
      server: {api_server}
      certificate-authority-data: {ca_data}
contexts:
  - name: in-cluster
    context:
      cluster: in-cluster
      user: hermes-agent
current-context: in-cluster
users:
  - name: hermes-agent
    user:
      token: {token}
"""


def main() -> int:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            token = f.read().strip()
        with open(CA_PATH, "rb") as f:
            ca_data = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        print(f"Konnte ServiceAccount-Token/CA nicht lesen: {e}")
        return 0

    kubeconfig = KUBECONFIG_TEMPLATE.format(api_server=API_SERVER, ca_data=ca_data, token=token)

    os.makedirs(os.path.dirname(KUBECONFIG_PATH), exist_ok=True)
    tmp_path = KUBECONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(kubeconfig)
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, KUBECONFIG_PATH)  # atomic -- kubectl never sees a half-written file
    return 0


if __name__ == "__main__":
    sys.exit(main())
