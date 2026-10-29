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
