"""What the VMware migration kit is given for a NIC (SDD §7.2): the key it maps the NIC by, the
guest addresses it sends Neutron, and its network_map value."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from seamless_migrate.domain.models import Nic
from seamless_migrate.planning.networks import (
    ROLES_UNREADABLE,
    kit_ipv4,
    kit_network_key,
    kit_network_value,
    kit_subnet,
    policy_profile_of,
    port_refusal,
    port_refusal_at_run,
)

NETWORK = {"id": "net-prod", "name": "dc2-prod", "own": True, "shared": False}


def subnet(sid, cidr):
    return {"id": sid, "name": sid, "network": "dc2-prod", "network_id": "net-prod", "cidr": cidr}


def nic(*addresses, key=None):
    return Nic(
        network="DC2-Prod", mac="00:50:56:aa:bb:cc", fixed_ips=list(addresses), network_key=key
    )


def test_kit_network_key_is_the_nics_recorded_key_or_none():
    assert kit_network_key(nic(key="dvportgroup-7")) == "dvportgroup-7"
    # a VM validated before the key was recorded: never its port group's name, which the kit
    # does not look a distributed port group's NIC up by (SDD §7.2, review RT-12)
    assert kit_network_key(nic()) is None
    assert kit_network_key(nic(key="")) is None


def test_kit_ipv4_keeps_what_the_kit_sends_neutron():
    """The kit's FixedIPsForNeutron: IPv4 addresses only, an IPv4-mapped IPv6 one as its IPv4."""
    assert kit_ipv4(["10.2.0.15", "fe80::1", "::ffff:10.2.0.16", "not-an-ip", "fd00::5"]) == [
        "10.2.0.15",
        "10.2.0.16",
    ]


def test_kit_subnet_is_the_one_holding_the_nics_ipv4_addresses():
    v6 = subnet("sub-v6", "fd00::/64")
    first, second = subnet("sub-a", "10.2.0.0/24"), subnet("sub-b", "10.2.1.0/24")
    # the subnet that holds them, not the network's first: the kit would take subnets[0]
    assert kit_subnet(nic("10.2.1.5", "fe80::1"), [v6, first, second]) == (second, None)
    # no IPv4 address: any of its subnets, an IPv4 one first; none at all: no subnet
    assert kit_subnet(nic("fe80::1"), [v6, first]) == (first, None)
    assert kit_subnet(nic(), [v6]) == (v6, None)
    assert kit_subnet(nic(), []) == (None, None)


def test_kit_subnet_refuses_addresses_the_kits_port_cannot_take():
    first, second = subnet("sub-a", "10.2.0.0/24"), subnet("sub-b", "10.2.1.0/24")
    # an address in no subnet, or no host address of it (network, broadcast)
    for address in ("192.0.2.5", "10.2.0.0", "10.2.0.255"):
        assert kit_subnet(nic(address), [first, second]) == (
            None,
            f"{address} is no host address of any subnet of the port's network",
        )
    # two subnets: the kit gives a NIC's IPv4 addresses one
    assert kit_subnet(nic("10.2.0.5", "10.2.1.5"), [first, second]) == (
        None,
        "10.2.0.5, 10.2.1.5 lie in different subnets of the port's network: the migration kit "
        "gives a NIC's IPv4 addresses one subnet",
    )
    # both in one subnet
    assert kit_subnet(nic("10.2.0.5", "10.2.0.6"), [first, second]) == (first, None)


def test_kit_network_value_names_the_network_and_the_subnet_by_id():
    assert kit_network_value(NETWORK, subnet("sub-a", "10.2.0.0/24")) == "net-prod,sub-a"
    assert kit_network_value(NETWORK, None) == "net-prod"


def test_port_refusal_asks_the_admin_role_for_an_unshared_external_network():
    """SDD §6.6, review RT-03: Nova (2023.1 network:attach_external_network, admin by default)
    attaches an external network that is not shared only for the admin role - the credential's
    own included, and advsvc does not count; Neutron's half is judged first for another
    project's network, and no reported roles judge neither."""
    member, advsvc, admin = ["member", "reader"], ["advsvc"], ["admin", "member"]
    own_external = {"own": True, "shared": False, "external": True}
    other_external = {"own": False, "shared": False, "external": True}
    nova = (
        "it is external and not shared, and Nova lets only the admin role attach such a network "
        "to a server; the credential's token lacks it"
    )
    assert port_refusal(own_external, member) == nova
    assert port_refusal(own_external, advsvc) == nova
    assert port_refusal(own_external, admin) is None
    assert port_refusal(other_external, advsvc) == nova
    assert port_refusal(other_external, admin) is None
    assert "Neutron lets only the admin or advsvc role" in (
        port_refusal(other_external, member) or ""
    )
    assert port_refusal({**own_external, "shared": True}, member) is None
    assert port_refusal({"own": True, "shared": False, "external": False}, member) is None
    for network in (own_external, other_external):
        assert port_refusal(network, None) is None


def test_a_run_refuses_a_port_needing_a_role_when_no_roles_were_read():
    """SDD §7.2, §7.3 step 0 (reviews RV19-01, RV20-07): before the source stops, a run judges
    §6.6's rule as port_refusal does with the roles it read. With none read - neither the token
    nor the check report names any - a port that needs a role is refused, where validation leaves
    that half unjudged; a port that needs none still goes, and a refusal that needs no roles keeps
    its own reason."""
    shared = {"own": False, "shared": True, "external": False}
    unshared_external = {"own": True, "shared": False, "external": True}
    own = {"own": True, "shared": False, "external": False}
    private = {"own": False, "shared": False, "external": False}
    for network in (shared, unshared_external):
        assert port_refusal(network, None) is None  # validation leaves the half unjudged
        assert port_refusal_at_run(network, None) == ROLES_UNREADABLE
        assert port_refusal_at_run(network, None, "secure_rbac") == ROLES_UNREADABLE
        assert port_refusal_at_run(network, ["admin"]) is None
        assert port_refusal_at_run(network, ["member"]) == port_refusal(network, ["member"])
    assert port_refusal_at_run(own, None) is None
    assert port_refusal_at_run(private, None) == (
        "it is neither shared with the credential's project nor external"
    )
    # the one message the VMware kit's map and the handover's step 0 give
    assert ROLES_UNREADABLE == (
        "the credential's token roles could not be read, and a port there needs a role: check the "
        "destination provider, then retry"
    )


PORT_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "port_refusal_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case",
    PORT_CASES["cases"],
    ids=lambda case: f"{case['network']}, {case['profile']}, {case['roles']}",
)
def test_the_shared_port_refusal_cases(case):
    """SDD §6.6, §21.5: the case table the dashboard's portRefusal runs too (the NIC-override picker
    and the mock's pre-flight, plan T8)."""
    network = dict(PORT_CASES["networks"][case["network"]], name="n")
    assert port_refusal(network, case["roles"], case["profile"]) == case["expected"]


@pytest.mark.parametrize("case", PORT_CASES["profile_cases"], ids=lambda case: case["why"])
def test_the_shared_policy_profile_cases(case):
    """SDD §21.5: the profile a check report gives, and for a report that names none the
    provider's declarations (review RV18-04) - the dashboard's policyProfileOf runs these too."""
    provider = SimpleNamespace(**case["provider"])
    assert policy_profile_of(case["capabilities"], provider) == case["expected"]
