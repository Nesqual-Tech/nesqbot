"""CRM — a first-party *placeholder*, not a vendor integration.

There is no "Nesq CRM" API. This connector exists so the risk model, the
approval flow and the UI have a realistic second connector to exercise, and its
actions have always returned mock data.

So this driver invents nothing. If the connector's manifest is given a
`base_url` (pointing at whatever CRM the deployment actually runs), the call is
performed by `generic_http` exactly as described there. Without one, the
connector keeps mocking, which is what every deployment gets today.
"""

from __future__ import annotations

from app.services.vendors.generic_http import GenericHttpDriver


class CrmDriver(GenericHttpDriver):
    name = "crm"


driver = CrmDriver()
