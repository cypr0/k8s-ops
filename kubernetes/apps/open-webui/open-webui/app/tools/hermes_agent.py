"""
title: Hermes Agent (Delegate)
author: cypr0
version: 1.0.0
license: MIT
requirements: httpx
description: >
  Hand a task to this cluster's hermes-agent (kubernetes/apps/hermes-agent/),
  the long-lived personal agent that has capabilities Open WebUI does not:
  a read-only Kubernetes terminal via its own ServiceAccount, persistent
  memory across sessions under HERMES_HOME, scheduled cron scripts, a
  Telegram/WhatsApp front-end, and the paperless/nextcloud/mailu/immich MCP
  servers in its own namespace.

  ── This is FIRE-AND-FORGET, and that is the agent's design, not a gap ──
  hermes-agent's webhook platform is asynchronous. POST /webhooks/<route>
  validates the shared secret, renders the route's prompt template with the
  payload, hands it to the agent as a background lane, and returns
  immediately with {"status": ..., "delivery_id": ...} -- the agent's actual
  ANSWER is then delivered to whatever that route's `deliver:` target is
  (Telegram, for the routes configured here), NOT back over this HTTP call
  (confirmed against the running container's
  /opt/hermes/gateway/platforms/webhook.py). A task can run for minutes and
  up to agent.max_turns: 45 turns, which is exactly why it is not a
  request/response API.

  So: the return value of delegate_task() is a receipt, never a result.
  Tell the user the task was handed over and where the answer will show up.
  Do not wait for it, do not poll for it, and do not invent it.

  Use this for work that genuinely needs hermes-agent -- "check the cluster
  and tell me what's unhealthy", "remember this for later", "watch this and
  ping me". For anything Open WebUI can answer in-chat, answering in-chat
  is better: this round trip costs a whole second agent run and lands the
  answer in a different app.

  Auth model: the same WEBHOOK_SHARED_SECRET hermes-agent validates for
  every webhook caller, sent as the X-Gitlab-Token header (that header name
  is not GitLab-specific here -- it is the plain-token comparison mode
  hermes-agent supports alongside computed HMAC signatures, and the name
  Paperless-ngx's Workflow Webhook action happens to send). Fed from an env
  var on the Open WebUI pod -- see externalsecret-hermes-token.yaml, which
  reads the SAME 1Password "hermes-agent" item field hermes-agent's own
  ExternalSecret already reads. There is no per-caller credential: anything
  holding this secret can trigger any configured route.

  No MCP mirror: hermes-agent IS the MCP client here, so a hermes-agent MCP
  server for hermes-agent would just be a loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Tools:
    class Valves(BaseModel):
        """Admin-configured, shared by every user.

        WEBHOOK_SECRET defaults to the HERMES_WEBHOOK_SECRET environment
        variable on the Open WebUI pod (populated from the same 1Password
        item hermes-agent's own ExternalSecret reads -- see
        externalsecret-hermes-token.yaml).
        """

        HERMES_BASE_URL: str = Field(
            default="http://hermes-agent.hermes-agent.svc.cluster.local:8644",
            description="hermes-agent webhook platform base URL, in-cluster "
            "Service DNS (no trailing slash).",
        )
        WEBHOOK_SECRET: str = Field(
            default_factory=lambda: os.getenv("HERMES_WEBHOOK_SECRET", ""),
            description="Shared secret sent as X-Gitlab-Token -- auto-filled "
            "from the cluster secret; override only to rotate/test.",
        )
        TASK_ROUTE: str = Field(
            default="owui-task",
            description="Webhook route delegate_task() posts to. Must exist "
            "in hermes-agent's config.yaml under platforms.webhook.extra."
            "routes -- see kubernetes/apps/hermes-agent/hermes-agent/app/"
            "configmap.yaml.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=15,
            description="Short on purpose: this call only waits for the "
            "agent to ACCEPT the task, never for it to finish.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # internal helpers (not exposed to the model)
    # ------------------------------------------------------------------
    def _post_webhook(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.valves.WEBHOOK_SECRET:
            return {
                "error": "No hermes-agent webhook secret configured. This "
                "should be auto-filled from the cluster secret -- if "
                "missing, check HERMES_WEBHOOK_SECRET on the Open WebUI "
                "pod, or set it manually in this tool's Valves (gear icon)."
            }
        url = f"{self.valves.HERMES_BASE_URL.rstrip('/')}/webhooks/{route}"
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers={"X-Gitlab-Token": self.valves.WEBHOOK_SECRET},
                timeout=self.valves.REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as e:
            return {"error": f"Could not reach hermes-agent: {e}"}
        if resp.status_code == 401:
            return {"error": "hermes-agent rejected the shared secret (401)."}
        if resp.status_code == 403:
            return {"error": f"Route '{route}' is disabled or unauthenticated (403)."}
        if resp.status_code == 404:
            return {
                "error": f"hermes-agent has no webhook route '{route}'. Routes are "
                "declared in its config.yaml (platforms.webhook.extra.routes) and "
                "the pod must be restarted after a change -- that ConfigMap is not "
                "hash-suffixed, so editing it does not roll the Deployment."
            }
        if resp.status_code == 429:
            return {"error": f"Route '{route}' is rate-limited right now (429). Try again later."}
        if resp.status_code >= 400:
            return {"error": f"hermes-agent returned HTTP {resp.status_code}: {resp.text[:300]}"}
        try:
            return resp.json()
        except ValueError:
            return {"status": "accepted", "http_status": resp.status_code}

    # ==================================================================
    # Public methods
    # ==================================================================
    def delegate_task(self, task: str, context: str = "") -> str:
        """Hand a task to hermes-agent to work on in the background. Returns a receipt, NOT an answer -- the agent replies over Telegram when it is done, which can take minutes. Say that to the user instead of waiting or guessing at the result.

        :param task: What the agent should do, in plain language. It has a read-only Kubernetes terminal, persistent memory, web scraping and its own Paperless/Nextcloud/Mailu/Immich tools.
        :param context: Anything from this conversation the agent needs but cannot see -- it starts with no knowledge of this chat.
        """
        if not task.strip():
            return str({"error": "task must not be empty."})
        payload: dict[str, Any] = {"task": task}
        if context:
            payload["context"] = context
        result = self._post_webhook(self.valves.TASK_ROUTE, payload)
        if "error" in result:
            return str(result)
        return str(
            {
                "status": "handed over",
                "route": self.valves.TASK_ROUTE,
                "delivery_id": result.get("delivery_id"),
                "note": "hermes-agent accepted the task and is working on it in the "
                "background. Its answer arrives over Telegram, not here. There is "
                "nothing to poll.",
            }
        )

    def trigger_route(self, route_name: str, payload: Optional[dict[str, Any]] = None) -> str:
        """Fire one of hermes-agent's other configured webhook routes, for the cases delegate_task does not cover. Same fire-and-forget contract.

        :param route_name: Route name as declared in hermes-agent's config.yaml under platforms.webhook.extra.routes.
        :param payload: JSON object whose keys the route's prompt template interpolates, e.g. {"document_id": 123}.
        """
        if not route_name.strip():
            return str({"error": "route_name must not be empty."})
        return str(self._post_webhook(route_name.strip(), payload or {}))

    def agent_status(self) -> str:
        """Check whether hermes-agent's webhook platform is up and reachable. Says nothing about whether a delegated task succeeded."""
        url = f"{self.valves.HERMES_BASE_URL.rstrip('/')}/health"
        try:
            resp = httpx.get(url, timeout=self.valves.REQUEST_TIMEOUT_SECONDS)
        except httpx.RequestError as e:
            return str({"reachable": False, "error": f"Could not reach hermes-agent: {e}"})
        return str(
            {
                "reachable": resp.status_code == 200,
                "http_status": resp.status_code,
                "body": resp.text[:300],
            }
        )
