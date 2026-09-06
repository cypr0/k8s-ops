#!/usr/bin/env python3
"""
Generic bridge: turns an Open WebUI "Tools"-class-shaped .py file into a
standalone MCP (Model Context Protocol) server over streamable-HTTP.

Every public method (not starting with "_") on the loaded Tools instance
is registered as an MCP tool, using its existing type hints + docstring
for the tool's schema/description -- no changes needed in the source
file itself, since these Tools classes are already plain Python + httpx +
pydantic underneath (their Valves' `os.getenv(...)` defaults work exactly
the same way outside Open WebUI as inside it).

This same bridge script is duplicated (identically) in
kubernetes/apps/hermes-agent/{paperless-mcp,nextcloud-mcp}/app/ -- kustomize's
configMapGenerator refuses file paths that escape its own kustomization
directory, so a single shared copy isn't possible; keep both in sync if
this file changes.

Environment:
  TOOL_MODULE_PATH   path to the mounted Tools .py source file
  MCP_SERVER_NAME    name reported to MCP clients
  MCP_PORT           port to listen on (default 8000)
  LOG_LEVEL          default INFO
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("owui-tool-mcp-bridge")


def load_tools_instance(module_path: str):
    spec = importlib.util.spec_from_file_location("owui_tool", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Tools()


def main() -> None:
    module_path = os.environ["TOOL_MODULE_PATH"]
    server_name = os.environ.get("MCP_SERVER_NAME", "owui-tool")
    port = int(os.environ.get("MCP_PORT", "8000"))

    tools = load_tools_instance(module_path)

    mcp = FastMCP(server_name, host="0.0.0.0", port=port)

    registered = 0
    for name, member in inspect.getmembers(tools, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        mcp.add_tool(member, name=name)
        registered += 1
        log.debug("Registered tool: %s", name)

    log.info(
        "Registered %d tool(s) from %s as MCP server '%s' on :%d (streamable-http, path /mcp)",
        registered,
        module_path,
        server_name,
        port,
    )

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
