"""
title: Proxmox VE (read-only)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx
description: >
  Read-only view of the Proxmox VE host this cluster runs on, for
  hermes-agent's "infrastruktur" profile: node health, guests (the Talos
  VMs), storage and ZFS, backups, failed tasks, pending updates.

  Auth: an API token `hermes@pve!readonly` with Privilege Separation and
  the PVEAuditor role on `/` (Authorization: PVEAPIToken=...). PVEAuditor
  can read everything and change nothing. On top of that, this module only
  ever sends GET -- there is no write method and raw_get() is GET-only.
  Changes to the host belong in the proxmox-ansible playbook
  (kubernetes/apps/automation/proxmox-ansible/), proposed as a Git change.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

_CAPABILITIES = {
    "endpoints": """raw_get(path, params) on /api2/json (GET only), e.g.:
  /cluster/status  /cluster/resources?type=vm|node|storage|sdn
  /cluster/backup                 scheduled backup jobs
  /cluster/backup-info/not-backed-up
  /nodes/{node}/status            CPU, memory, load, kernel, uptime
  /nodes/{node}/tasks?errors=1    failed tasks
  /nodes/{node}/apt/update        pending package updates
  /nodes/{node}/disks/list        disks incl. SMART health
  /nodes/{node}/disks/zfs         ZFS pools
  /nodes/{node}/storage/{storage}/content?content=backup
  /nodes/{node}/qemu/{vmid}/status/current
  /nodes/{node}/qemu/{vmid}/config
  /nodes/{node}/certificates/info
  /nodes/{node}/syslog?limit=200  /nodes/{node}/journal?lastentries=200
  /version""",
}


class Tools:
    class Valves(BaseModel):
        PROXMOX_URL: str = Field(default_factory=lambda: os.getenv("PROXMOX_URL", ""))
        PROXMOX_API_TOKEN_ID: str = Field(default_factory=lambda: os.getenv("PROXMOX_API_TOKEN_ID", ""))
        PROXMOX_API_TOKEN_SECRET: str = Field(default_factory=lambda: os.getenv("PROXMOX_API_TOKEN_SECRET", ""))
        PROXMOX_VERIFY_TLS: bool = Field(default_factory=lambda: os.getenv("PROXMOX_VERIFY_TLS", "true").lower() == "true")

    def __init__(self):
        self.valves = self.Valves()

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        base = self.valves.PROXMOX_URL.rstrip("/")
        headers = {"Authorization": f"PVEAPIToken={self.valves.PROXMOX_API_TOKEN_ID}={self.valves.PROXMOX_API_TOKEN_SECRET}"}
        with httpx.Client(verify=self.valves.PROXMOX_VERIFY_TLS, timeout=30.0) as c:
            r = c.get(f"{base}/api2/json{path}", params=params or {}, headers=headers)
            if r.status_code >= 400:
                return {"error": r.status_code, "detail": r.text[:500]}
            return r.json().get("data")

    def _nodes(self) -> list[str]:
        nodes = self._get("/nodes")
        return [n["node"] for n in nodes] if isinstance(nodes, list) else []

    def capabilities(self, topic: str = "endpoints") -> str:
        """Reference notes: the Proxmox API paths reachable through raw_get."""
        return _CAPABILITIES.get(topic, _CAPABILITIES["endpoints"])

    def overview(self) -> dict:
        """Node health (CPU, memory, load, uptime, PVE version) plus all guests with status and resource use."""
        out: dict[str, Any] = {"version": self._get("/version"), "nodes": {}}
        for n in self._nodes():
            st = self._get(f"/nodes/{n}/status")
            if isinstance(st, dict) and "error" not in st:
                mem = st.get("memory", {})
                out["nodes"][n] = {
                    "cpu": round(st.get("cpu", 0) * 100, 1),
                    "loadavg": st.get("loadavg"),
                    "mem_used_gib": round(mem.get("used", 0) / 2**30, 1),
                    "mem_total_gib": round(mem.get("total", 0) / 2**30, 1),
                    "uptime_h": round(st.get("uptime", 0) / 3600, 1),
                    "kernel": (st.get("current-kernel") or {}).get("release") or st.get("kversion"),
                    "pveversion": st.get("pveversion"),
                }
            else:
                out["nodes"][n] = st
        guests = self._get("/cluster/resources", {"type": "vm"})
        if isinstance(guests, list):
            out["guests"] = [
                {
                    "vmid": g.get("vmid"), "name": g.get("name"), "node": g.get("node"),
                    "type": g.get("type"), "status": g.get("status"),
                    "cpu_pct": round((g.get("cpu") or 0) * 100, 1),
                    "mem_gib": round((g.get("mem") or 0) / 2**30, 1),
                    "maxmem_gib": round((g.get("maxmem") or 0) / 2**30, 1),
                    "uptime_h": round((g.get("uptime") or 0) / 3600, 1),
                }
                for g in guests
            ]
        return out

    def storage(self) -> dict:
        """Storage usage per storage plus ZFS pool health and disk SMART status per node."""
        out: dict[str, Any] = {"storages": [], "zfs": {}, "disks": {}}
        st = self._get("/cluster/resources", {"type": "storage"})
        if isinstance(st, list):
            out["storages"] = [
                {
                    "storage": s.get("storage"), "node": s.get("node"), "status": s.get("status"),
                    "used_pct": round(100 * (s.get("disk") or 0) / s["maxdisk"], 1) if s.get("maxdisk") else None,
                    "size_gib": round((s.get("maxdisk") or 0) / 2**30, 1),
                }
                for s in st
            ]
        for n in self._nodes():
            out["zfs"][n] = self._get(f"/nodes/{n}/disks/zfs")
            disks = self._get(f"/nodes/{n}/disks/list")
            if isinstance(disks, list):
                out["disks"][n] = [
                    {"dev": d.get("devpath"), "model": d.get("model"), "health": d.get("health"),
                     "wearout": d.get("wearout"), "size_gib": round((d.get("size") or 0) / 2**30)}
                    for d in disks
                ]
            else:
                out["disks"][n] = disks
        return out

    def problems(self, limit: int = 30) -> dict:
        """What needs attention: failed tasks per node, guests without a backup job, pending package updates."""
        out: dict[str, Any] = {"failed_tasks": {}, "pending_updates": {}}
        for n in self._nodes():
            tasks = self._get(f"/nodes/{n}/tasks", {"errors": 1, "limit": max(1, min(int(limit), 200))})
            if isinstance(tasks, list):
                out["failed_tasks"][n] = [
                    {"type": t.get("type"), "id": t.get("id"), "status": t.get("status"),
                     "starttime": t.get("starttime"), "user": t.get("user")}
                    for t in tasks
                ]
            else:
                out["failed_tasks"][n] = tasks
            upd = self._get(f"/nodes/{n}/apt/update")
            if isinstance(upd, list):
                out["pending_updates"][n] = {
                    "count": len(upd),
                    "packages": [f"{u.get('Package')} {u.get('OldVersion')} -> {u.get('Version')}" for u in upd[:40]],
                }
            else:
                out["pending_updates"][n] = upd
        out["not_backed_up"] = self._get("/cluster/backup-info/not-backed-up")
        out["backup_jobs"] = self._get("/cluster/backup")
        return out

    def raw_get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET any Proxmox API path below /api2/json (see capabilities()). Read-only: only GET is possible."""
        if not path.startswith("/") or ".." in path:
            return {"error": "path must be absolute and must not contain '..'"}
        return self._get(path, params)
