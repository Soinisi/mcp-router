"""Synthetic MCP servers used by the context-savings benchmark.

Each process serves one product domain, selected by ``BENCH_DOMAIN``. Every
domain exposes realistic CRUD tools with a name, a description and a JSON
inputSchema, so a direct connection costs the same kind of context a production
MCP server would. The benchmark runs this file N times as N separate stdio
servers; the tools are never called -- only their definitions are measured.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

DOMAINS: dict[str, dict[str, Any]] = {
    "github": {
        "product": "GitHub",
        "resources": [
            ("repository", "repositories", "the owner/name pair"),
            ("issue", "issues", "the issue number"),
            ("pull request", "pull requests", "the pull request number"),
        ],
    },
    "slack": {
        "product": "Slack",
        "resources": [
            ("channel", "channels", "the channel ID"),
            ("message", "messages", "the message timestamp"),
            ("user", "users", "the user ID"),
        ],
    },
    "postgres": {
        "product": "PostgreSQL",
        "resources": [
            ("database", "databases", "the database name"),
            ("table", "tables", "the schema-qualified table name"),
            ("row", "rows", "the primary key"),
        ],
    },
    "sentry": {
        "product": "Sentry",
        "resources": [
            ("project", "projects", "the project slug"),
            ("issue", "issues", "the issue ID"),
            ("event", "events", "the event ID"),
        ],
    },
    "stripe": {
        "product": "Stripe",
        "resources": [
            ("customer", "customers", "the customer ID"),
            ("invoice", "invoices", "the invoice ID"),
            ("payment", "payments", "the payment intent ID"),
        ],
    },
    "jira": {
        "product": "Jira",
        "resources": [
            ("project", "projects", "the project key"),
            ("issue", "issues", "the issue key"),
            ("sprint", "sprints", "the sprint ID"),
        ],
    },
    "notion": {
        "product": "Notion",
        "resources": [
            ("page", "pages", "the page ID"),
            ("database", "databases", "the database ID"),
            ("block", "blocks", "the block ID"),
        ],
    },
    "pagerduty": {
        "product": "PagerDuty",
        "resources": [
            ("service", "services", "the service ID"),
            ("incident", "incidents", "the incident ID"),
            ("schedule", "schedules", "the schedule ID"),
        ],
    },
    "datadog": {
        "product": "Datadog",
        "resources": [
            ("monitor", "monitors", "the monitor ID"),
            ("dashboard", "dashboards", "the dashboard ID"),
            ("incident", "incidents", "the incident ID"),
        ],
    },
    "linear": {
        "product": "Linear",
        "resources": [
            ("team", "teams", "the team key"),
            ("issue", "issues", "the issue identifier"),
            ("project", "projects", "the project ID"),
        ],
    },
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _integer(description: str) -> dict[str, str]:
    return {"type": "integer", "description": description}


def _tools_for(domain: str, spec: dict[str, Any]) -> list[types.Tool]:
    product = spec["product"]
    tools: list[types.Tool] = []
    for singular, plural, identifier in spec["resources"]:
        slug = plural.replace(" ", "_")
        tools.append(
            types.Tool(
                name=f"list_{slug}",
                description=(
                    f"List {plural} in {product}, newest first, with optional "
                    f"pagination."
                ),
                inputSchema=_schema(
                    {
                        "limit": _integer("Maximum number of results to return."),
                        "cursor": _string("Pagination cursor from a previous response."),
                    }
                ),
            )
        )
        tools.append(
            types.Tool(
                name=f"get_{singular.replace(' ', '_')}",
                description=(
                    f"Fetch a single {singular} from {product} by "
                    f"{identifier}, including its status and metadata."
                ),
                inputSchema=_schema(
                    {"id": _string(f"The {singular}'s {identifier}.")}, ["id"]
                ),
            )
        )
        tools.append(
            types.Tool(
                name=f"create_{singular.replace(' ', '_')}",
                description=(
                    f"Create a {singular} in {product} with the supplied fields "
                    f"and return the stored record."
                ),
                inputSchema=_schema(
                    {
                        "title": _string(f"Short title for the new {singular}."),
                        "details": _string(f"Markdown body describing the {singular}."),
                        "metadata": {
                            "type": "object",
                            "description": f"Arbitrary key/value labels for the {singular}.",
                        },
                    },
                    ["title"],
                ),
            )
        )
        tools.append(
            types.Tool(
                name=f"update_{singular.replace(' ', '_')}",
                description=(
                    f"Update an existing {singular} in {product} and return the "
                    f"updated record."
                ),
                inputSchema=_schema(
                    {
                        "id": _string(f"The {singular}'s {identifier}."),
                        "title": _string(f"New title for the {singular}."),
                        "details": _string(f"New Markdown body for the {singular}."),
                    },
                    ["id"],
                ),
            )
        )
    return tools


def build_server(domain: str) -> Server:
    spec = DOMAINS[domain]
    tools = _tools_for(domain, spec)
    server = Server(f"bench-{domain}")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        payload = {"tool": name, "arguments": arguments or {}, "ok": True}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))]
        )

    return server


async def _serve(domain: str) -> None:
    server = build_server(domain)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    domain = os.environ.get("BENCH_DOMAIN", "github")
    if domain not in DOMAINS:
        known = ", ".join(sorted(DOMAINS))
        raise SystemExit(f"Unknown BENCH_DOMAIN {domain!r}. Known domains: {known}.")
    asyncio.run(_serve(domain))


if __name__ == "__main__":
    main()
