#!/usr/bin/env python3
"""Refreshes a world-readable kubeconfig, embedding this pod's own
ServiceAccount token/CA inline. Runs on a loop inside the
`kubeconfig-refresher` sidecar (deployment.yaml), NOT as a hermes cron
job -- see that container's own comment for the full path history:

- hermes's terminal tool / cron scheduler (agent-mode AND --no-agent
  alike) runs everything as a fixed unprivileged uid (10000, no
  supplementary groups) once s6-overlay finishes its own root
  bootstrap -- confirmed live, so nothing hermes-managed can ever read
  the real token (root:<fsGroup> 0640 -- Kubernetes' own
  serviceAccountToken projected-volume source hard-codes that mode
  regardless of requested defaultMode, also confirmed live).
- The obvious fix target, $HOME/.kube/config, sits under /opt/data,
  whose root is 0700 owner-only and -- confirmed live -- not even
  root can chmod it ("Operation not permitted" on this CSI/NFS-backed
  volume's mount root specifically).

So this writes to a path OUTSIDE /opt/data entirely (a plain emptyDir,
no such restriction), and a KUBECONFIG env var on the main container
points kubectl there -- confirmed live that non-PATH env vars (unlike
PATH itself, which Tirith's sandbox resets to a fixed default) DO
propagate into sandboxed commands.

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
KUBECONFIG_PATH = os.environ["KUBECONFIG_PATH_OVERRIDE"]

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
