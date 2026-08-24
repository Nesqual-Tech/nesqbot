"""A placeholder ACI address must never be banked as a desktop endpoint.

Production regression. `az container show` reported the group at `10.60.4.4`
and the container `Running` with a clean image pull, the NSG allowed
`10.60.0.0/23 -> 6901,7910`, and a TCP probe from inside the API container
reached both ports. Everything was healthy — and both the live stream and the
screenshot fallback failed with `upstream_error`.

The stored endpoint was `http://0.0.0.0:6901`. ARM reports `0.0.0.0` while a
VNet-injected group is still being placed; the start poll accepted it as "has
an IP" and stopped waiting, writing the row nine seconds before the container
started. Dialling `0.0.0.0` from a container means "this host", so the API was
calling itself.

Nothing about the failure pointed at the address: the desktop was genuinely up,
the path was genuinely open, and the error said the *stream* was unreachable.
"""

from __future__ import annotations

import pytest

from app.services.desktop import ACI_PLACEHOLDER_IPS, aci_private_ip


class _Ip:
    def __init__(self, ip):
        self.ip = ip


class _Group:
    def __init__(self, ip):
        self.ip_address = _Ip(ip) if ip is not None else None


@pytest.mark.parametrize("placeholder", sorted(ACI_PLACEHOLDER_IPS))
def test_a_placeholder_address_reads_as_no_address(placeholder):
    assert aci_private_ip(_Group(placeholder)) == ""


@pytest.mark.parametrize("placeholder", sorted(ACI_PLACEHOLDER_IPS))
def test_whitespace_around_a_placeholder_does_not_smuggle_it_through(placeholder):
    assert aci_private_ip(_Group(f"  {placeholder} ")) == ""


def test_a_real_private_address_is_returned():
    assert aci_private_ip(_Group("10.60.4.4")) == "10.60.4.4"


def test_absent_and_empty_addresses_read_as_no_address():
    assert aci_private_ip(_Group(None)) == ""
    assert aci_private_ip(_Group("")) == ""
    assert aci_private_ip(_Group("   ")) == ""


def test_the_start_poll_treats_a_placeholder_as_not_ready():
    """The behaviour that actually matters: keep waiting, do not bank it.

    `aci_private_ip` returning falsy is the whole readiness signal the start
    loop uses, so this is the contract that keeps a placeholder from becoming a
    stream URL.
    """
    assert not aci_private_ip(_Group("0.0.0.0"))  # noqa: S104
    assert aci_private_ip(_Group("10.60.4.9"))
