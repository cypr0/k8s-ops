# immich-mcp

> **Namespace**  hermes-agent
> **Source**     plain manifests, hand-rolled — generic `python:3.14-slim` image + ConfigMap-mounted script + init-container `pip install` (`kubernetes/apps/hermes-agent/immich-mcp/app/deployment.yaml`), same pattern as sibling apps `paperless-mcp`, `nextcloud-mcp` and `mailu-mcp` in this namespace
> **Hostname**   none — ClusterIP only, ingress restricted to `hermes-agent` pods + Gatus (`kubernetes/apps/hermes-agent/immich-mcp/app/ciliumnetworkpolicy.yaml`)

## What it does here
A standalone MCP (Model Context Protocol) server that gives the `hermes-agent` app tool access to the cluster's Immich photo library (`kubernetes/apps/immich/immich/`): CLIP-backed natural-language photo search, structured metadata search, albums, people, and asset/thumbnail download. It runs `immich_photos.py`, a byte-identical mirror of the Open WebUI Tool at `kubernetes/apps/open-webui/open-webui/app/tools/immich_photos.py` — the same arrangement `paperless-mcp`/`nextcloud-mcp` have with their Open WebUI counterparts.

**This is the one MCP server in this namespace that cannot destroy anything.** `immich_photos.py` exposes no delete method and its `raw_request()` escape hatch refuses the `DELETE` verb outright for any path — a deliberate asymmetry with `paperless-mcp` (deletes to a recoverable trash), `nextcloud-mcp` (trashbin + versions, and a super-admin credential behind it) and `mailu-mcp` (Radicale has no trash at all). Photos are irreplaceable and the failure mode of a misread instruction is not worth the convenience, so deleting stays a deliberate act in the Immich UI (`immich_photos.py`'s module docstring).

## Architecture at a glance
- **Depends on:** the in-cluster Immich instance (`kubernetes/apps/immich/immich/`), reached at `http://immich-immich-server.immich.svc.cluster.local:2283` over plain HTTP — no TLS-SAN workaround needed here, unlike `mailu-mcp`'s public-hostname detour. No database, cache or object storage of its own; the Deployment is stateless and has no PVC.
- **Depended on by:** `hermes-agent` only, as an MCP client — registered under the `immich` key in `kubernetes/apps/hermes-agent/hermes-agent/app/configmap.yaml`'s `mcp_servers` section, pointed at `http://immich-mcp.hermes-agent.svc.cluster.local:8000/mcp`.

## Repo layout
| File | Purpose |
| --- | --- |
| `kubernetes/apps/hermes-agent/immich-mcp/app/deployment.yaml` | Init container `pip install`s the `mcp`/`httpx`/`pydantic` SDKs into an `emptyDir`; main container runs the generic bridge against the Immich tool module. No `icalendar` here, unlike `mailu-mcp` |
| `kubernetes/apps/hermes-agent/immich-mcp/app/owui_tool_mcp_bridge.py` | Generic bridge: loads an Open-WebUI-`Tools`-shaped `.py` and republishes every public method as an MCP tool over streamable-HTTP (`/mcp`) — byte-identical copy also lives in `paperless-mcp`/`nextcloud-mcp`/`mailu-mcp` (kustomize's `configMapGenerator` can't reference files outside its own directory, so a shared copy isn't possible) |
| `kubernetes/apps/hermes-agent/immich-mcp/app/immich_photos.py` | The Immich `Tools` class — 14 methods, config via env-var-backed `Valves`. Byte-identical mirror of the Open WebUI Tool of the same name |
| `kubernetes/apps/hermes-agent/immich-mcp/app/externalsecret.yaml` | Pulls `IMMICH_API_KEY` from the existing 1Password `immich` item |
| `kubernetes/apps/hermes-agent/immich-mcp/app/service.yaml` | ClusterIP, port 8000 |
| `kubernetes/apps/hermes-agent/immich-mcp/app/ciliumnetworkpolicy.yaml` | Ingress from `hermes-agent` pods + Gatus; egress to DNS + Immich (`immich` namespace, :2283) + world:443 (pip install) |
| `kubernetes/apps/hermes-agent/immich-mcp/app/kustomization.yaml` | Content-hash-suffixed `configMapGenerator` for the two `.py` files — any script change renames the ConfigMap, changes the Deployment's volume ref, and triggers a rollout automatically |
| `kubernetes/apps/hermes-agent/immich-mcp/ks.yaml` | Flux Kustomization |

## Secrets
| Key (in `immich-mcp-secret`) | 1Password source | Consumed by |
| --- | --- | --- |
| `IMMICH_API_KEY` | item `immich`, field `IMMICH_API_KEY` | `envFrom` on the main container; auto-fills `immich_photos.py`'s `Valves.API_KEY`, sent as the `x-api-key` header |

**This key has to be created by hand, once, and it is the one prerequisite this app cannot provision for itself.** Immich has no service-account concept: API keys are minted only by a signed-in user under *Account Settings → API Keys*, and this instance is OIDC-only with `passwordLogin.enabled: false` (`kubernetes/apps/immich/immich/app/externalsecret.yaml`'s `immich.json`). So:

1. Sign in to `media.${SECRET_DOMAIN}` as the user whose photos these tools should see.
2. Create an API key scoped to `asset.read`, `asset.view`, `asset.update`, `album.read`, `album.create`, `album.update`, `person.read`, `server.about` — nothing else.
3. Add it as a new `IMMICH_API_KEY` field on the **existing** 1Password `immich` item (the same item `kubernetes/apps/immich/immich/app/externalsecret.yaml` already reads).

Until that field exists this ExternalSecret stays in error and the Deployment's pod will not start. The same 1Password field is read independently by `kubernetes/apps/open-webui/open-webui/app/externalsecret-immich-token.yaml` for the Open WebUI side — that one is wired with `optional: true` on its `envFrom` precisely so a missing key degrades one tool instead of taking the chat UI down; there is no equivalent need here, since this pod has no other job.

An Immich API key inherits its owning user's library scope — it sees that user's photos plus what is shared with them. There is no instance-wide key, so "which user created it" is the real access boundary.

## Routing & access
- **ClusterIP only, no HTTPRoute** — same as its sibling MCP servers. Nothing outside the cluster can reach it.
- **CiliumNetworkPolicy** (`ciliumnetworkpolicy.yaml`): ingress only from `hermes-agent` pods (:8000) and Gatus; egress to CoreDNS, Immich (`immich` namespace, :2283) and `world:443` for the init container's `pip install`.
- The Immich side needs a matching ingress rule, which lives in Immich's own policy (`kubernetes/apps/immich/immich/app/ciliumnetworkpolicy.yaml`) and covers this app and the Open WebUI pod in one block.
- No SSO — there is no user-facing UI to gate.

## Storage
None. Both volumes are `emptyDir`s (pip target, scratch `/tmp`) and are excluded from Velero's cluster-wide Kopia fs-backup via `backup.velero.io/backup-volumes-excludes` — nothing worth keeping, and including them only buys restore-test flakiness.

## Known quirks
- **`immich_photos.py` was built against the routes of the deployed Immich version, read off the running container's compiled controllers, not from upstream docs.** Immich v3 reshaped the search API: every search endpoint accepts *both* a legacy flat body (`personIds`, `takenAfter`, `city`, …) and a new nested `{filter, orderBy, cursor}` body, and `withShapeExclusivity` in `server/dist/dtos/search.dto.js` makes the two mutually exclusive **per request** — mixing a flat field with `filter` is a validation error, not a merge. The tool sends the flat shape throughout.
- **Search results are slimmed before they reach the model.** An Immich asset record carries a full EXIF block, people with face bounding boxes, tags, stack and checksums, and smart search returns up to 1000 of them. `_slim_asset()` projects each hit to id/filename/type/date/place/favorite/people; `verbose=True` opts back into the full records. This is a response-side token decision, separate from the method-count one described in `docs/apps/open-webui.md`.
- **`get_server_statistics()` needs an admin-scoped key.** A 403 there means the key is scoped too narrowly (or its user isn't an admin), not that the endpoint is wrong.
- **The tool mirrors are kept in sync by hand.** `kustomize`'s `configMapGenerator` refuses file paths outside its own kustomization directory, so `immich_photos.py` and `owui_tool_mcp_bridge.py` exist as duplicate copies here and under `kubernetes/apps/open-webui/open-webui/app/tools/`. Both carry a MIRROR NOTICE in their header.
- **Deps come from PyPI at pod start,** not from a built image — there is no CI/registry for this app. A PyPI outage means the init container fails and the pod doesn't start.

## Common operations
- Change what the tool can do: edit `immich_photos.py` **and its mirror** under `kubernetes/apps/open-webui/open-webui/app/tools/`, commit, push. The ConfigMap hash changes, the Deployment rolls automatically; the Open WebUI side re-syncs via its own Job.
- Rotate the API key: mint a new one in Immich, update the `IMMICH_API_KEY` field on the 1Password `immich` item, then `kubectl annotate externalsecret immich-mcp -n hermes-agent force-sync=$(date +%s)` and `kubectl rollout restart deployment/immich-mcp -n hermes-agent`. Do the same for `open-webui-immich-token` in the `open-webui` namespace.
- Verify it's serving: `kubectl logs -n hermes-agent deployment/immich-mcp` — the bridge logs how many tools it registered at startup.
- Pause reconciliation: `flux suspend kustomization immich-mcp -n flux-system`.

## TODOs / unknowns
- Not exercised end-to-end from hermes-agent at the time of writing: the manifests, the tool's method surface and its spec generation were validated, but no live agent tool call has been observed through this server yet.
- Whether Immich's CLIP search quality is good enough in German prompts (the model is `ViT-B-32__openai`, `kubernetes/apps/immich/immich/app/externalsecret.yaml`) has not been tested.

---
_See also: `docs/apps/immich.md` for the photo library itself, `docs/apps/hermes-agent.md` for the MCP client, and `docs/apps/open-webui.md` for the mirrored Open WebUI Tool and the token-budget reasoning behind its shape._
