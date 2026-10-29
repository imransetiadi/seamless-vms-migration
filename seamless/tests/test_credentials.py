"""The selection (SDD §8, §27.5, plan task TC5): one function gives each step its credential."""

import pytest

from seamless_migrate.credentials import (
    TABLE,
    Step,
    credential_project,
    verification_view,
)
from seamless_migrate.domain.enums import ProviderRole
from tests.credential_support import CredentialRegistry, of_project, own, project_credentials
from tests.credential_support import tenant_project as project
from tests.factories import make_plan, make_provider

SOURCE, DESTINATION = ProviderRole.source, ProviderRole.destination


def test_each_step_takes_the_credential_sdd_27_5_names():
    """SDD §27.5's table, row by row: the plan's project on a side where the row names the plan's
    - its source_project at the source, its destination_project at the destination -, the
    provider's own (None) where it names the provider's, and on a side whose plan names no
    project; a side the row has none of is no step at all."""
    a, c = project(), project()
    plan = make_plan(source_project=a, destination_project=c)
    plain = make_plan()
    expected = {
        Step.CHECK: (None, None),
        Step.GPU: ("-", None),
        Step.INVENTORY: (a, c),
        Step.PRESTAGE: (a, c),
        Step.FLAVORS: (a, None),
        Step.MIGRATION: (a, c),
        Step.HANDOVER: (None, c),
        Step.NETWORK: (a, c),
        Step.DNS: ("-", c),
        Step.KIT: (None, None),
    }
    assert set(expected) == set(Step) == set(TABLE)
    for step, (source, destination) in expected.items():
        for side, wanted in ((SOURCE, source), (DESTINATION, destination)):
            if wanted == "-":
                with pytest.raises(ValueError, match="has no source side"):
                    credential_project(step, side, plan)
                continue
            assert credential_project(step, side, plan) == wanted, (step, side)
            assert credential_project(step, side, plain) is None, (step, side)
            assert credential_project(step, side, None) is None, (step, side)
    only_source = make_plan(source_project=a)
    assert credential_project(Step.MIGRATION, DESTINATION, only_source) is None
    assert credential_project(Step.MIGRATION, SOURCE, only_source) == a


def test_a_steps_conversion_host_pin_is_its_credentials():
    """SDD §4.2, §10, §26.4 (§26.10 decision 11, the owner's decision of 2026-10-29 after plan
    task TC5): the pin a step reads on a side is the pin of the credential it acts with there -
    the provider's own for its own credential, a project credential's own for that project -,
    never another's: none for a project the provider holds no credential for. A write puts the
    pin there alone, every other pin as it was; for a project whose credential is gone it puts
    none."""
    from seamless_migrate.credentials import host_pin, with_host_pin
    from seamless_migrate.domain.models import HostKeyPin, utcnow
    from tests.key_support import bare, public_key

    def pin(server, seed):
        keys = [bare(public_key(seed))]
        return HostKeyPin(server_id=server, keys=keys, source="injected", pinned_at=utcnow())

    a, b, gone = project(), project(), project()
    own_pin, pin_a, pin_b = pin("srv-own", 1), pin("srv-a-1", 2), pin("srv-b-1", 3)
    provider = make_provider(
        id="src-osp",
        conversion_host_pin=own_pin,
        project_credentials=project_credentials("src-osp", a, b),
    )
    assert host_pin(provider, None) == own_pin
    assert host_pin(provider, a) is None and host_pin(provider, gone) is None

    with_a = with_host_pin(provider, a, pin_a)
    assert (host_pin(with_a, a), host_pin(with_a, b), host_pin(with_a, None)) == (
        pin_a,
        None,
        own_pin,
    )
    assert with_a.project_credentials[a].secret == provider.project_credentials[a].secret
    assert with_a.project_credentials[a].updated_at == provider.project_credentials[a].updated_at
    assert host_pin(provider, a) is None, "the provider it was given stays as it was"
    both = with_host_pin(with_a, b, pin_b)
    assert (host_pin(both, a), host_pin(both, b), host_pin(both, None)) == (pin_a, pin_b, own_pin)
    renewed = pin("srv-own-2", 4)
    own_again = with_host_pin(both, None, renewed)
    assert (host_pin(own_again, a), host_pin(own_again, b), host_pin(own_again, None)) == (
        pin_a,
        pin_b,
        renewed,
    )
    assert with_host_pin(both, gone, pin("srv-gone", 5)) is None

    # the step's pin: the selection of §27.5, then its credential's pin
    plan = make_plan(source_project=a)
    assert host_pin(both, credential_project(Step.MIGRATION, SOURCE, plan)) == pin_a
    assert host_pin(both, credential_project(Step.KIT, SOURCE, plan)) == own_pin
    assert host_pin(both, credential_project(Step.MIGRATION, SOURCE, make_plan())) == own_pin


async def test_the_verification_reads_placement_with_the_providers_credential():
    """SDD §7.5, §22.8, §27.5: the verification of a plan with a destination project reads the
    server with the project's credential and its allocations - Placement's - with the destination
    provider's own; a plan without one reads both with the provider's own, as before."""

    class Destination:
        async def get_server(self, server_id):
            return {"id": server_id}

        async def server_allocations(self, server_id):
            return {"VGPU": 1}

    c = project()
    destination = make_provider(
        id="dst-rhoso",
        role=DESTINATION,
        project_credentials=project_credentials("dst-rhoso", c),
    )
    registry = CredentialRegistry(None, Destination())
    view = verification_view(registry, destination, make_plan(destination_project=c))
    await view.get_server("srv-1")
    await view.server_allocations("srv-1")
    assert registry.calls == [
        (of_project(destination, c), "get_server"),
        (own(destination), "server_allocations"),
    ]
    registry.calls.clear()
    view = verification_view(registry, destination, make_plan())
    await view.get_server("srv-1")
    await view.server_allocations("srv-1")
    assert registry.calls == [
        (own(destination), "get_server"),
        (own(destination), "server_allocations"),
    ]
