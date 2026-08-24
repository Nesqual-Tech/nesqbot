"""Support ticketing — a first-party *placeholder*, not a vendor integration.

Same story as `crm.py`: there is no ticketing vendor behind this connector, so
there is no vendor API to write a client for. Give the connector's manifest a
`base_url` (and per-action `method` / `path` / `body`) describing the help desk
the deployment really uses and `generic_http` performs the call; otherwise the
actions mock, as they always have.

Its manifest declares `auth: "api_key"`, so the resolved secret is sent in the
manifest's `api_key_header` rather than as a bearer token.
"""

from __future__ import annotations

from app.services.vendors.generic_http import GenericHttpDriver


class TicketingDriver(GenericHttpDriver):
    name = "ticketing"


driver = TicketingDriver()
