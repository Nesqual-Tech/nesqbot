"""Vendor drivers — the real call behind a connector action.

`connectors._invoke_vendor` picks a driver here, and runs it only when a
credential resolved *and* the connector is configured for real calls. Every
other case falls back to the mock, which stays the normalised contract.

Adding a first-party driver is: write the module, register it in `DRIVERS`.
Anything not registered — every custom connector — is driven by its manifest
through `generic_http`.
"""

from __future__ import annotations

from app.services.vendors import crm, generic_http, graph, ticketing
from app.services.vendors.base import (
    DEFAULT_API_KEY_HEADER,
    RETRY_ATTEMPTS,
    VendorCallError,
    VendorDriver,
    VendorOutcome,
    VendorResponse,
    call_vendor,
    get_default_transport,
    is_retryable,
    redact,
    set_default_transport,
)

DRIVERS: dict[str, VendorDriver] = {
    graph.driver.name: graph.driver,
    crm.driver.name: crm.driver,
    ticketing.driver.name: ticketing.driver,
}


def select_driver(connector_id: str) -> VendorDriver:
    """The driver for a connector. Unregistered ids are manifest-driven."""
    return DRIVERS.get(connector_id, generic_http.driver)


__all__ = [
    "DEFAULT_API_KEY_HEADER",
    "DRIVERS",
    "RETRY_ATTEMPTS",
    "VendorCallError",
    "VendorDriver",
    "VendorOutcome",
    "VendorResponse",
    "call_vendor",
    "crm",
    "generic_http",
    "get_default_transport",
    "graph",
    "is_retryable",
    "redact",
    "select_driver",
    "set_default_transport",
    "ticketing",
]
