"""
title: OPNsense (read-only)
author: cypr0
version: 1.1.0
license: MIT
requirements: httpx
description: >
  Read-only view of the OPNsense firewall in front of this cluster, for
  hermes-agent's "netzwerk" profile: system and firmware status,
  interfaces, gateways, firewall rules and aliases, DHCP leases, WireGuard
  and Suricata (IDS) alerts.

  OPNsense has no read-only API role: an API key carries the page
  privileges of its user, and the same pages also accept the POST calls
  that change things. So the guarantee lives HERE: this module only sends
  GET, raw_get() is GET-only, and GET paths whose last segment is an action
  verb (set/add/del/toggle/apply/reconfigure/start/stop/restart/reboot/
  update/upgrade/...) are refused even though OPNsense would answer them
  only on POST. Give the API user nothing but the pages it should read.

  Auth: OPNSENSE_API_KEY / OPNSENSE_API_SECRET as HTTP basic auth.

  TLS: the firewall serves its self-signed web GUI certificate and is
  addressed by IP, so neither the public CA store nor a hostname check can
  work. OPNSENSE_CA_PEM_B64 pins exactly that certificate as the only trust
  anchor (hostname check off -- the pin replaces it); any other
  certificate, including a MITM's, fails the handshake. It comes from the
  SOPS-encrypted cluster-secrets (SECRET_OPNSENSE_CERT_B64) because the
  certificate names the private domain. When the GUI certificate is
  renewed (current one expires 2027-07-29), update that secret.
  Security events from OPNsense/Suricata also land in Wazuh (syslog) --
  the "sicherheit" profile reads those; this tool is about configuration
  and state.
"""

from __future__ import annotations

import base64
import os
import re
import ssl
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

_ACTION = re.compile(
    r"/(set|add|del|delete|toggle|apply|reconfigure|start|stop|restart|reboot|halt|"
    r"update|upgrade|install|remove|reinstall|lock|unlock|flush|kill|reset|import|"
    r"savepoint|revert|cancelRollback|poweroff|register|addItem|setItem|delItem|"
    r"toggleItem|addRule|setRule|delRule|toggleRule)[A-Za-z]*(/|$)",
    re.IGNORECASE,
)

_CAPABILITIES = {
    "endpoints": """raw_get(path, params) on /api (GET only, action paths refused), e.g.:
  /core/firmware/status                    installed version, pending updates
  /diagnostics/system/systemInformation    /systemResources  /systemDisk
  /interfaces/overview/interfacesInfo      /diagnostics/interface/getArp
  /routes/gateway/status                   /diagnostics/interface/getRoutes
  /firewall/filter/searchRule  /firewall/alias/searchItem  /firewall/alias/listCategories
  /dhcpv4/leases/searchLease               (ISC)   /kea/leases4/search (Kea)
  /unbound/diagnostics/stats   /unbound/settings/get
  /wireguard/service/show      /wireguard/server/searchServer
  /ids/service/queryAlerts     /ids/service/status
  /diagnostics/firewall/pf_states  /diagnostics/firewall/log
  /core/service/search                     running services""",
}


class Tools:
    class Valves(BaseModel):
        OPNSENSE_URL: str = Field(default_factory=lambda: os.getenv("OPNSENSE_URL", ""))
        OPNSENSE_API_KEY: str = Field(default_factory=lambda: os.getenv("OPNSENSE_API_KEY", ""))
        OPNSENSE_API_SECRET: str = Field(default_factory=lambda: os.getenv("OPNSENSE_API_SECRET", ""))
        OPNSENSE_VERIFY_TLS: bool = Field(default_factory=lambda: os.getenv("OPNSENSE_VERIFY_TLS", "true").lower() == "true")
        OPNSENSE_CA_PEM_B64: str = Field(default_factory=lambda: os.getenv("OPNSENSE_CA_PEM_B64", ""))

    def __init__(self):
        self.valves = self.Valves()

    def _verify(self):
        if self.valves.OPNSENSE_CA_PEM_B64:
            ctx = ssl.create_default_context(cadata=base64.b64decode(self.valves.OPNSENSE_CA_PEM_B64).decode())
            ctx.check_hostname = False  # addressed by IP; the pinned certificate is the check
            return ctx
        return self.valves.OPNSENSE_VERIFY_TLS

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if _ACTION.search(path):
            return {"error": "refused: path looks like an action endpoint; this tool is read-only"}
        base = self.valves.OPNSENSE_URL.rstrip("/")
        with httpx.Client(verify=self._verify(), timeout=30.0) as c:
            r = c.get(
                f"{base}/api{path}",
                params=params or {},
                auth=(self.valves.OPNSENSE_API_KEY, self.valves.OPNSENSE_API_SECRET),
            )
            if r.status_code >= 400:
                return {"error": r.status_code, "detail": r.text[:500]}
            try:
                return r.json()
            except ValueError:
                return {"text": r.text[:2000]}

    @staticmethod
    def _rows(res: Any, limit: int) -> Any:
        if isinstance(res, dict) and isinstance(res.get("rows"), list):
            return {"total": res.get("total", len(res["rows"])), "rows": res["rows"][:limit]}
        return res

    def capabilities(self, topic: str = "endpoints") -> str:
        """Reference notes: the OPNsense API paths reachable through raw_get."""
        return _CAPABILITIES.get(topic, _CAPABILITIES["endpoints"])

    def status(self) -> dict:
        """Firmware/version and pending updates, system resources (CPU, memory, disk) and running services."""
        return {
            "firmware": self._get("/core/firmware/status"),
            "system": self._get("/diagnostics/system/systemInformation"),
            "resources": self._get("/diagnostics/system/systemResources"),
            "disk": self._get("/diagnostics/system/systemDisk"),
            "services": self._rows(self._get("/core/service/search"), 100),
        }

    def interfaces_and_gateways(self) -> dict:
        """Interface overview (status, addresses, traffic counters) and gateway status (latency, loss, up/down)."""
        return {
            "interfaces": self._rows(self._get("/interfaces/overview/interfacesInfo"), 50),
            "gateways": self._get("/routes/gateway/status"),
        }

    def firewall_rules(self, search: str = "", limit: int = 100) -> dict:
        """Firewall filter rules (automation/MVC rules) and aliases. search: optional text filter."""
        params = {"searchPhrase": search, "rowCount": max(1, min(int(limit), 500))}
        return {
            "rules": self._rows(self._get("/firewall/filter/searchRule", params), limit),
            "aliases": self._rows(self._get("/firewall/alias/searchItem", {"searchPhrase": search, "rowCount": 500}), 200),
        }

    def dhcp_leases(self, search: str = "", limit: int = 100) -> dict:
        """Current DHCP leases (tries ISC dhcpd first, then Kea). search: hostname/IP/MAC filter."""
        params = {"searchPhrase": search, "rowCount": max(1, min(int(limit), 500))}
        res = self._get("/dhcpv4/leases/searchLease", params)
        if isinstance(res, dict) and res.get("error"):
            res = self._get("/kea/leases4/search", params)
        return self._rows(res, limit)

    def vpn_status(self) -> dict:
        """WireGuard service, tunnels and peers with last handshake."""
        return {"wireguard": self._get("/wireguard/service/show")}

    def ids_alerts(self, search: str = "", limit: int = 50) -> dict:
        """Recent Suricata (IDS/IPS) alerts on the firewall itself. search: optional text filter."""
        params = {"searchPhrase": search, "rowCount": max(1, min(int(limit), 500)), "current": 1}
        return {"status": self._get("/ids/service/status"), "alerts": self._rows(self._get("/ids/service/queryAlerts", params), limit)}

    def raw_get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET any OPNsense API path below /api (see capabilities()). Read-only: only GET, action paths are refused."""
        if not path.startswith("/") or ".." in path:
            return {"error": "path must be absolute and must not contain '..'"}
        return self._get(path, params)
