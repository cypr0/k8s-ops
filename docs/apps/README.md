# App Documentation

Per-app operational docs, one file per app, generated per the contract in
[`../prompts/app-docs-campaign.md`](../prompts/app-docs-campaign.md) from the
skeleton at [`../templates/app-readme.md`](../templates/app-readme.md).

Every app under `kubernetes/apps/` has a doc as of 2026-08-16 (54 apps,
matching every leaf directory in `kubernetes/apps/` one-for-one). New apps
added after this date won't have one yet until the campaign catches up —
check here before assuming a gap means "not interesting."

## Index

| App | Namespace | Doc |
| --- | --- | --- |
| 1Password Connect | security | [onepassword-connect.md](onepassword-connect.md) |
| Alloy | monitoring | [alloy.md](alloy.md) |
| Authentik | security | [authentik.md](authentik.md) |
| cert-manager | cert-manager | [cert-manager.md](cert-manager.md) |
| Cilium | kube-system | [cilium.md](cilium.md) |
| ClamAV | nextcloud | [clamav.md](clamav.md) |
| Cloudflare DNS (external-dns) | network | [cloudflare-dns.md](cloudflare-dns.md) |
| Cloudflare Tunnel | network | [cloudflare-tunnel.md](cloudflare-tunnel.md) |
| CloudNativePG | database | [cloudnative-pg.md](cloudnative-pg.md) |
| Collabora | nextcloud | [collabora.md](collabora.md) |
| CoreDNS | kube-system | [coredns.md](coredns.md) |
| csi-driver-nfs | kube-system | [csi-driver-nfs.md](csi-driver-nfs.md) |
| Dragonfly | database | [dragonfly.md](dragonfly.md) |
| Echo | echo | [echo.md](echo.md) |
| Elasticsearch (Nextcloud) | nextcloud | [elasticsearch.md](elasticsearch.md) |
| Envoy Gateway | network | [envoy-gateway.md](envoy-gateway.md) |
| external-secrets | security | [external-secrets.md](external-secrets.md) |
| Falco & Falcosidekick | falco | [falco.md](falco.md) |
| firecrawl | hermes-agent | [firecrawl.md](firecrawl.md) |
| Fluent Bit | logging | [fluent-bit.md](fluent-bit.md) |
| Flux Instance | flux-system | [flux-instance.md](flux-instance.md) |
| flux-operator | flux-system | [flux-operator.md](flux-operator.md) |
| flux-operator-mcp | flux-system | [flux-operator-mcp.md](flux-operator-mcp.md) |
| Gatus | monitoring | [gatus.md](gatus.md) |
| Gotenberg | paperless | [gotenberg.md](gotenberg.md) |
| Grafana | monitoring | [grafana.md](grafana.md) |
| hermes-agent | hermes-agent | [hermes-agent.md](hermes-agent.md) |
| Immich | immich | [immich.md](immich.md) |
| k8s-gateway | network | [k8s-gateway.md](k8s-gateway.md) |
| kube-prometheus-stack | monitoring | [kube-prometheus-stack.md](kube-prometheus-stack.md) |
| Kyverno | kyverno | [kyverno.md](kyverno.md) |
| Loki | monitoring | [loki.md](loki.md) |
| Metrics Server | kube-system | [metrics-server.md](metrics-server.md) |
| Nextcloud | nextcloud | [nextcloud.md](nextcloud.md) |
| nextcloud-exporter | nextcloud | [nextcloud-exporter.md](nextcloud-exporter.md) |
| nextcloud-mcp | hermes-agent | [nextcloud-mcp.md](nextcloud-mcp.md) |
| OIDC RBAC | security | [oidc-rbac.md](oidc-rbac.md) |
| Open Terminal | open-webui | [open-terminal.md](open-terminal.md) |
| Open WebUI | open-webui | [open-webui.md](open-webui.md) |
| OpenSearch Cluster | logging | [opensearch-cluster.md](opensearch-cluster.md) |
| OpenSearch Operator | logging | [opensearch-operator.md](opensearch-operator.md) |
| paperless-mcp | hermes-agent | [paperless-mcp.md](paperless-mcp.md) |
| paperless-ngx | paperless | [paperless-ngx.md](paperless-ngx.md) |
| philipp-rosch-site | portfolio | [philipp-rosch-site.md](philipp-rosch-site.md) |
| plugin-barman-cloud | database | [plugin-barman-cloud.md](plugin-barman-cloud.md) |
| Proxmox Ansible | automation | [proxmox-ansible.md](proxmox-ansible.md) |
| Reloader | kube-system | [reloader.md](reloader.md) |
| sogo-mcp | hermes-agent | [sogo-mcp.md](sogo-mcp.md) |
| Spegel | kube-system | [spegel.md](spegel.md) |
| Tika (open-webui) | open-webui | [open-webui-tika.md](open-webui-tika.md) |
| Tika (Paperless) | paperless | [paperless-tika.md](paperless-tika.md) |
| Trivy Operator | trivy-system | [trivy.md](trivy.md) |
| velero | velero | [velero.md](velero.md) |
| Whiteboard | nextcloud | [whiteboard.md](whiteboard.md) |
