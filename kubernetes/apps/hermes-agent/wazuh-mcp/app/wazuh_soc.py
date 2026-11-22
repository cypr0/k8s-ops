"""
title: Wazuh (SOC, read-only)
author: cypr0
version: 1.0.1
license: MIT
requirements: httpx
description: >
  Read-only view of this cluster's Wazuh SIEM (kubernetes/apps/security/wazuh/)
  for hermes-agent's "sicherheit" profile. Wazuh watches the hosts OUTSIDE the
  cluster: the Proxmox host, the MacBook, and OPNsense including its Suricata
  IDS (syslog into the manager). In-cluster findings (Trivy, Kyverno, Falco)
  come from flux-operator-mcp instead.

  Two backends, both read-only by construction:
    * Wazuh server API (manager, :55000): agents, SCA, file integrity,
      rules. User `hermes-soc` with the built-in `readonly` RBAC role.
    * Wazuh indexer (OpenSearch, :9200): alerts (wazuh-alerts-*) and
      vulnerability state (wazuh-states-vulnerabilities-*). Internal user
      `hermes-soc` mapped to the `readall` role (externalsecret-
      securityconfig.yaml in the wazuh app).

  This module only ever sends GET to the manager and POST to an index's
  _search/_count endpoint on the indexer -- there is no code path that
  writes, and raw_get() refuses anything but GET. The credentials are
  read-only as well, so a prompt-injected alert text (attackers control
  log content!) cannot turn this into a write channel.

  TLS: the manager's API certificate is self-signed by Wazuh and the
  indexer's comes from a CA that only exists as a Secret in the wazuh
  namespace, so verification is off (WAZUH_VERIFY_TLS=false). Both
  endpoints are only reachable from this pod through CiliumNetworkPolicy.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

_MAX_TEXT = 400  # per-field cap on free text coming back from alerts

_CAPABILITIES: dict[str, str] = {
    "alerts": """search_alerts filters (all optional, AND-combined):
  hours        look-back window (default 24, max 720)
  min_level    Wazuh rule level 0-15 (default 7; 10+ = high, 12+ = critical)
  agent        agent name (exact), e.g. the Proxmox host or the Mac
  group        rule group, e.g. authentication_failed, sshd, suricata, ids,
               syscheck, vulnerability-detector, sca, rootcheck, web
  text         free text matched against rule.description and full_log
Alert fields returned (slimmed): timestamp, agent, rule.id, rule.level,
rule.description, rule.groups, srcip, location, full_log (truncated).
OPNsense/Suricata events arrive via syslog on the manager itself, so their
agent name is the manager's, and the source host is in `location`.""",
    "manager_endpoints": """raw_get(path, params) on the Wazuh server API (GET only):
  /agents  /agents/summary/status  /agents/outdated
  /sca/{agent_id}   /sca/{agent_id}/checks/{policy_id}
  /syscheck/{agent_id}        file integrity events per agent
  /rootcheck/{agent_id}
  /syscollector/{agent_id}/packages|os|hardware|ports|processes
  /rules?rule_ids=...  /rules?search=...   /decoders?search=...
  /manager/status  /manager/info  /manager/logs/summary
  /cluster/status  /overview/agents""",
}


class Tools:
    class Valves(BaseModel):
        WAZUH_API_URL: str = Field(default_factory=lambda: os.getenv("WAZUH_API_URL", "https://wazuh.wazuh.svc.cluster.local:55000"))
        WAZUH_API_USER: str = Field(default_factory=lambda: os.getenv("WAZUH_API_USER", ""))
        WAZUH_API_PASSWORD: str = Field(default_factory=lambda: os.getenv("WAZUH_API_PASSWORD", ""))
        WAZUH_INDEXER_URL: str = Field(default_factory=lambda: os.getenv("WAZUH_INDEXER_URL", "https://indexer.wazuh.svc.cluster.local:9200"))
        WAZUH_INDEXER_USER: str = Field(default_factory=lambda: os.getenv("WAZUH_INDEXER_USER", ""))
        WAZUH_INDEXER_PASSWORD: str = Field(default_factory=lambda: os.getenv("WAZUH_INDEXER_PASSWORD", ""))
        WAZUH_VERIFY_TLS: bool = Field(default_factory=lambda: os.getenv("WAZUH_VERIFY_TLS", "false").lower() == "true")

    def __init__(self):
        self.valves = self.Valves()
        self._token: Optional[str] = None
        self._token_at: float = 0.0

    # ── plumbing ──────────────────────────────────────────────────────────
    def _client(self) -> httpx.Client:
        return httpx.Client(verify=self.valves.WAZUH_VERIFY_TLS, timeout=30.0)

    def _manager_token(self) -> str:
        # Tokens live 900s by default; renew a little early.
        if self._token and time.monotonic() - self._token_at < 600:
            return self._token
        with self._client() as c:
            r = c.post(
                f"{self.valves.WAZUH_API_URL}/security/user/authenticate",
                auth=(self.valves.WAZUH_API_USER, self.valves.WAZUH_API_PASSWORD),
            )
            r.raise_for_status()
            self._token = r.json()["data"]["token"]
            self._token_at = time.monotonic()
            return self._token

    def _manager_get(self, path: str, params: Optional[dict] = None) -> Any:
        with self._client() as c:
            r = c.get(
                f"{self.valves.WAZUH_API_URL}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {self._manager_token()}"},
            )
            if r.status_code >= 400:
                return {"error": r.status_code, "detail": r.text[:500]}
            return r.json().get("data", r.json())

    def _search(self, index: str, body: dict) -> Any:
        with self._client() as c:
            r = c.post(
                f"{self.valves.WAZUH_INDEXER_URL}/{index}/_search",
                json=body,
                auth=(self.valves.WAZUH_INDEXER_USER, self.valves.WAZUH_INDEXER_PASSWORD),
            )
            if r.status_code >= 400:
                return {"error": r.status_code, "detail": r.text[:500]}
            return r.json()

    @staticmethod
    def _cut(v: Any) -> Any:
        if isinstance(v, str) and len(v) > _MAX_TEXT:
            return v[:_MAX_TEXT] + "…"
        return v

    def _slim_alert(self, hit: dict) -> dict:
        s = hit.get("_source", {})
        rule = s.get("rule", {})
        data = s.get("data", {}) or {}
        return {
            "id": hit.get("_id"),
            "timestamp": s.get("timestamp"),
            "agent": (s.get("agent") or {}).get("name"),
            "rule_id": rule.get("id"),
            "level": rule.get("level"),
            "description": rule.get("description"),
            "groups": rule.get("groups"),
            "mitre": (rule.get("mitre") or {}).get("technique"),
            "srcip": data.get("srcip") or (data.get("src_ip")),
            "location": s.get("location"),
            "full_log": self._cut(s.get("full_log")),
        }

    @staticmethod
    def _alert_query(hours: int, min_level: int, agent: Optional[str], group: Optional[str], text: Optional[str]) -> dict:
        hours = max(1, min(int(hours), 720))
        must: list[dict] = [
            {"range": {"timestamp": {"gte": f"now-{hours}h"}}},
            {"range": {"rule.level": {"gte": int(min_level)}}},
        ]
        if agent:
            must.append({"term": {"agent.name": agent}})
        if group:
            must.append({"term": {"rule.groups": group}})
        if text:
            must.append({"multi_match": {"query": text, "fields": ["rule.description", "full_log"]}})
        return {"bool": {"must": must}}

    # ── tools ─────────────────────────────────────────────────────────────
    def capabilities(self, topic: str = "") -> str:
        """Reference notes for this tool. topic: 'alerts' or 'manager_endpoints'; empty lists topics."""
        if not topic:
            return "Topics: " + ", ".join(sorted(_CAPABILITIES))
        return _CAPABILITIES.get(topic, f"Unknown topic. Topics: {', '.join(sorted(_CAPABILITIES))}")

    def agents_overview(self) -> dict:
        """All Wazuh agents with status (active/disconnected/never_connected), OS, version and last keep-alive."""
        summary = self._manager_get("/agents/summary/status")
        agents = self._manager_get(
            "/agents",
            {"select": "id,name,ip,status,os.name,os.version,version,lastKeepAlive", "limit": 500},
        )
        items = agents.get("affected_items", agents) if isinstance(agents, dict) else agents
        return {"summary": summary, "agents": items}

    def search_alerts(self, hours: int = 24, min_level: int = 7, agent: str = "", group: str = "", text: str = "", size: int = 30) -> dict:
        """Search Wazuh alerts, newest first. hours: look-back window. min_level: rule level 0-15. agent: agent name. group: rule group (e.g. sshd, suricata, authentication_failed). text: free text. size: max 100."""
        body = {
            "size": max(1, min(int(size), 100)),
            "track_total_hits": True,  # otherwise totals cap at 10,000
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": self._alert_query(hours, min_level, agent or None, group or None, text or None),
        }
        res = self._search("wazuh-alerts-*", body)
        if "error" in res:
            return res
        hits = res.get("hits", {})
        return {"total": (hits.get("total") or {}).get("value"), "alerts": [self._slim_alert(h) for h in hits.get("hits", [])]}

    def alert_statistics(self, hours: int = 24, min_level: int = 3) -> dict:
        """Counts of alerts in the window: per level, per agent, top rules and top source IPs. Use this first to see where to look."""
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": self._alert_query(hours, min_level, None, None, None),
            "aggs": {
                "by_level": {"terms": {"field": "rule.level", "size": 16, "order": {"_key": "desc"}}},
                "by_agent": {"terms": {"field": "agent.name", "size": 20}},
                "top_rules": {"terms": {"field": "rule.description", "size": 15}},
                "top_srcip": {"terms": {"field": "data.srcip", "size": 10}},
            },
        }
        res = self._search("wazuh-alerts-*", body)
        if "error" in res:
            return res
        aggs = res.get("aggregations", {})
        flat = {k: [(b["key"], b["doc_count"]) for b in v.get("buckets", [])] for k, v in aggs.items()}
        flat["total"] = (res.get("hits", {}).get("total") or {}).get("value")
        return flat

    def get_alert(self, alert_id: str) -> dict:
        """One alert in full (all fields), by the id returned from search_alerts."""
        res = self._search("wazuh-alerts-*", {"size": 1, "query": {"ids": {"values": [alert_id]}}})
        if "error" in res:
            return res
        hits = res.get("hits", {}).get("hits", [])
        return hits[0].get("_source", {}) if hits else {"error": "not found"}

    def vulnerabilities(self, agent: str = "", min_severity: str = "High", size: int = 30) -> dict:
        """Open vulnerabilities from Wazuh's vulnerability detection. agent: agent name (empty = all). min_severity: Critical, High, Medium or Low. Returns counts per agent/severity and the top CVEs."""
        order = ["Critical", "High", "Medium", "Low"]
        sev = order[: order.index(min_severity) + 1] if min_severity in order else order[:2]
        must: list[dict] = [{"terms": {"vulnerability.severity": sev}}]
        if agent:
            must.append({"term": {"agent.name": agent}})
        body = {
            "size": max(0, min(int(size), 100)),
            "track_total_hits": True,
            "query": {"bool": {"must": must}},
            "sort": [{"vulnerability.score.base": {"order": "desc", "unmapped_type": "float"}}],
            "aggs": {
                "by_agent": {"terms": {"field": "agent.name", "size": 20}},
                "by_severity": {"terms": {"field": "vulnerability.severity", "size": 4}},
            },
        }
        res = self._search("wazuh-states-vulnerabilities-*", body)
        if "error" in res:
            return res
        vulns = []
        for h in res.get("hits", {}).get("hits", []):
            s = h.get("_source", {})
            v, p = s.get("vulnerability", {}), s.get("package", {})
            vulns.append({
                "agent": (s.get("agent") or {}).get("name"),
                "cve": v.get("id"),
                "severity": v.get("severity"),
                "score": (v.get("score") or {}).get("base"),
                "package": f"{p.get('name')} {p.get('version')}",
                "fixed_in": self._cut(v.get("reference")),
            })
        aggs = res.get("aggregations", {})
        return {
            "total": (res.get("hits", {}).get("total") or {}).get("value"),
            "by_agent": [(b["key"], b["doc_count"]) for b in aggs.get("by_agent", {}).get("buckets", [])],
            "by_severity": [(b["key"], b["doc_count"]) for b in aggs.get("by_severity", {}).get("buckets", [])],
            "top": vulns,
        }

    def sca_results(self, agent_id: str) -> dict:
        """Security Configuration Assessment (CIS benchmark) score per policy for one agent id (from agents_overview)."""
        return self._manager_get(f"/sca/{agent_id}")

    def raw_get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET any Wazuh server API path (see capabilities('manager_endpoints')). Read-only: only GET is possible."""
        if not path.startswith("/") or ".." in path:
            return {"error": "path must be absolute and must not contain '..'"}
        return self._manager_get(path, params)
