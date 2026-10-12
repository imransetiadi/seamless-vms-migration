"""Neutron's IPAM rules for a port's fixed IPs (SDD §6.6, §7.3 step 0), as the collection's
destination check applies them: what would fail after the source stops."""

from types import SimpleNamespace as NS

from seamless_migrate.planning.ipam import address_refusal, auto_address, eui64, requested_ips


def subnet(cidr, mode=None):
    return NS(id=f"sub-{cidr}", cidr=cidr, ipv6_address_mode=mode, ipv6_ra_mode=None)


def test_eui64_is_the_address_slaac_derives_from_the_mac():
    assert str(eui64("fd00:20::/64", "fa:16:3e:00:00:01")) == "fd00:20::f816:3eff:fe00:1"
    assert str(eui64("fd00:20::/64", "FA:16:3E:00:00:01")) == "fd00:20::f816:3eff:fe00:1"


def test_address_refusal_follows_check_subnet_ip_for_a_port_without_an_owner():
    v4 = [subnet("10.0.0.0/24"), subnet("10.255.0.0/31"), subnet("fd00:10::/64")]
    for host in ("10.0.0.5", "10.0.0.254", "10.255.0.1", "fd00:10::5"):
        assert address_refusal(host, v4, neutron=False, mac=None) is None, host
    for refused in ("10.0.0.0", "10.0.0.255", "10.255.0.0", "10.9.0.5"):
        assert "is no host address" in (
            address_refusal(refused, v4, neutron=False, mac=None) or ""
        ), refused
    # Nova and the import create ports with no owner, and check_subnet_ip takes an IPv6 subnet's
    # first address for one (review NC-08)
    for neutron in (False, True):
        assert address_refusal("fd00:10::", v4, neutron=neutron, mac="fa:16:3e:00:00:01") is None
    assert address_refusal("nonsense", v4, neutron=False, mac=None) == "is not an IP address"


def test_requested_ips_leave_out_what_neutron_derives_from_the_mac():
    """SDD §6.6, §7.3, review NC-01: Neutron 2023.1 allocates a SLAAC or DHCPv6-stateless
    subnet's EUI-64 address of the MAC on every port it creates, also when it is sent, and the
    second allocation of one address fails: such addresses are never sent."""
    v4 = subnet("10.0.0.0/24")
    slaac = subnet("fd00:20::/64", "slaac")
    stateless = NS(
        id="sub-st", cidr="fd00:30::/64", ipv6_address_mode=None, ipv6_ra_mode="dhcpv6-stateless"
    )
    stateful = subnet("fd00:40::/64", "dhcpv6-stateful")
    assert [auto_address(s) for s in (v4, slaac, stateless, stateful)] == [False, True, True, False]
    fixed = [
        {"ip_address": "10.0.0.5"},
        {"ip_address": "fd00:20::f816:3eff:fe00:1"},
        {"subnet_id": "sub-st", "ip_address": "fd00:30::f816:3eff:fe00:1"},
        {"ip_address": "fd00:40::5"},
        {"ip_address": "192.0.2.9"},
    ]
    # an address no subnet holds stays: the address rules refuse it before anything is created
    assert requested_ips(fixed, [v4, slaac, stateless, stateful]) == [fixed[0], fixed[3], fixed[4]]
    assert requested_ips(fixed[1:2], [slaac]) == []


def test_address_refusal_on_auto_address_subnets():
    slaac = [subnet("fd00:20::/64", "slaac")]
    stateless = [subnet("fd00:20::/64", "dhcpv6-stateless")]
    mac = "fa:16:3e:00:00:01"
    for subnets in (slaac, stateless):
        assert address_refusal("fd00:20::f816:3eff:fe00:1", subnets, neutron=True, mac=mac) is None
        refused = address_refusal("fd00:20::5", subnets, neutron=True, mac=mac)
        assert refused and refused.startswith("is not fd00:20::f816:3eff:fe00:1")
        refused = address_refusal("fd00:20::f816:3eff:fe00:1", subnets, neutron=False, mac=mac)
        assert refused and "SLAAC or DHCPv6-stateless" in refused
    ra_only = [NS(id="ra", cidr="fd00:20::/64", ipv6_address_mode=None, ipv6_ra_mode="slaac")]
    assert address_refusal("fd00:20::5", ra_only, neutron=True, mac=mac)
