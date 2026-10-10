"""Guest OS catalog (SDD §9.5): one case table shared with the dashboard."""

import json
from pathlib import Path

import pytest

from seamless_migrate.guest_os import GuestOS, identify

CASES = json.loads((Path(__file__).parent / "fixtures" / "guest_os_cases.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c["in"]))
def test_identify_guest_os_cases(case):
    expected = {k: v for k, v in case.items() if k != "in"}
    assert identify(case["in"]).model_dump() == expected


def test_vmref_serializes_its_guest_os():
    from tests.factories import make_vm

    vm = make_vm(os_type="windows2019srvNext_64Guest")
    assert vm.guest_os == GuestOS(
        family="windows",
        distro="windows-server",
        version="2022",
        label="Windows Server 2022",
        lifecycle="current",
        v2v="supported",
    )
    assert vm.model_dump(mode="json")["guest_os"]["label"] == "Windows Server 2022"
