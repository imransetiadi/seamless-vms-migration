"""Neutron's IPAM rules for a port's fixed IPs (SDD §6.6, §7.3 step 0), as the collection's
destination check applies them: what would fail after the source stops."""

from types import SimpleNamespace as NS

from seamless_migrate.planning.ipam import address_refusal, eui64


def subnet(cidr, mode=None):
    return NS(id=f"sub-{cidr}", cidr=cidr, ipv6_address_mode=mode, ipv6_ra_mode=None)


def test_eui64_is_the_address_slaac_derives_from_the_mac():
    assert str(eui64("fd00:20::/64", "fa:16:3e:00:00:01")) == "fd00:20::f816:3eff:fe00:1"
    assert str(eui64("fd00:20::/64", "FA:16:3E:00:00:01")) == "fd00:20::f816:3eff:fe00:1"


def test_address_refusal_follows_check_subnet_ip_for_the_ports_owner():
    v4 = [subnet("10.0.0.0/24"), subnet("10.255.0.0/31"), subnet("fd00:10::/64")]
    for host in ("10.0.0.5", "10.0.0.254", "10.255.0.1", "fd00:10::5"):
        assert address_refusal(host, v4, neutron=False, mac=None) is None, host
    for refused in ("10.0.0.0", "10.0.0.255", "10.255.0.0", "10.9.0.5", "fd00:10::"):
        assert "is no host address" in (
            address_refusal(refused, v4, neutron=False, mac=None) or ""
        ), refused
    # a port created through Neutron has no owner yet: an IPv6 subnet's first address is taken
    assert address_refusal("fd00:10::", v4, neutron=True, mac="fa:16:3e:00:00:01") is None
    assert address_refusal("nonsense", v4, neutron=False, mac=None) == "is not an IP address"


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
