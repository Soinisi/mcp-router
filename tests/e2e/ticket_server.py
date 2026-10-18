"""A fictional customer-ticket MCP used by the end-to-end tests.

Tickets are persisted to the JSON file named by ``TICKET_STORE`` so a test can
verify the side effect independently of the model's prose. Descriptions are
written like a real support product, not like test instructions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

STORE = Path(os.environ["TICKET_STORE"])

mcp = FastMCP("tickets")


def _load() -> list[dict]:
    if not STORE.exists():
        return []
    return json.loads(STORE.read_text(encoding="utf-8"))


def _save(tickets: list[dict]) -> None:
    STORE.write_text(json.dumps(tickets, indent=2), encoding="utf-8")


@mcp.tool()
def create_ticket(
    customer: str,
    subject: str,
    body: str,
    priority: str = "normal",
) -> dict:
    """Create a customer support ticket and return it."""
    tickets = _load()
    ticket = {
        "id": f"TCK-{len(tickets) + 1:04d}",
        "customer": customer,
        "subject": subject,
        "body": body,
        "priority": priority,
        "status": "open",
    }
    tickets.append(ticket)
    _save(tickets)
    return ticket


@mcp.tool()
def get_ticket(ticket_id: str) -> dict:
    """Look up a customer support ticket by its id."""
    for ticket in _load():
        if ticket["id"] == ticket_id:
            return ticket
    raise ValueError(f"Ticket {ticket_id} was not found.")


@mcp.tool()
def list_tickets(customer: str | None = None, status: str | None = None) -> list[dict]:
    """List customer support tickets, optionally filtered by customer or status."""
    tickets = _load()
    if customer:
        tickets = [t for t in tickets if t["customer"].lower() == customer.lower()]
    if status:
        tickets = [t for t in tickets if t["status"] == status]
    return tickets


@mcp.tool()
def close_ticket(ticket_id: str) -> dict:
    """Close an open customer support ticket."""
    tickets = _load()
    for ticket in tickets:
        if ticket["id"] == ticket_id:
            ticket["status"] = "closed"
            _save(tickets)
            return ticket
    raise ValueError(f"Ticket {ticket_id} was not found.")


if __name__ == "__main__":
    mcp.run()
