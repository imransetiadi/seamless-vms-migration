"""The suite's warning filters hide openstacksdk's own deprecation noise, never ours.

openstacksdk 4.25 warns from inside itself on calls our code makes correctly (``proxy.py``'s
``service_type``, ``resource.py``'s ``_compute_attributes``, ``cloud_region.py``'s influxdb);
some 430 such lines buried the few warnings that mean something, such as an unclosed SQLite
connection. The filter in ``pyproject.toml`` is scoped to the module that issues the warning,
so an SDK deprecation that points at a ``seamless_migrate`` call site still shows.
"""

from __future__ import annotations

import warnings

import pytest
from _pytest.config import apply_warning_filters

sdk_warnings = pytest.importorskip("openstack.warnings")
RemovedInSDK50Warning = sdk_warnings.RemovedInSDK50Warning
RemovedInSDK60Warning = sdk_warnings.RemovedInSDK60Warning


def _shown(request: pytest.FixtureRequest, category: type[Warning], module: str) -> bool:
    """Whether a ``category`` warning issued from ``module`` survives the suite's filters."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        apply_warning_filters(request.config.getini("filterwarnings"), [])
        warnings.warn_explicit("deprecated", category, f"{module}.py", 1, module=module)
    return bool(caught)


@pytest.mark.parametrize("category", [RemovedInSDK50Warning, RemovedInSDK60Warning])
@pytest.mark.parametrize(
    "module", ["openstack.proxy", "openstack.resource", "openstack.config.cloud_region"]
)
def test_deprecations_openstacksdk_raises_inside_itself_are_hidden(request, category, module):
    assert not _shown(request, category, module)


@pytest.mark.parametrize("category", [RemovedInSDK50Warning, RemovedInSDK60Warning])
@pytest.mark.parametrize(
    "module", ["seamless_migrate.providers.openstack", "seamless_migrate.preflight", "tests.x"]
)
def test_deprecations_our_own_calls_trigger_stay_visible(request, category, module):
    assert _shown(request, category, module)


@pytest.mark.parametrize("module", ["openstack.proxy", "seamless_migrate.store"])
def test_resource_warnings_stay_visible(request, module):
    assert _shown(request, ResourceWarning, module)


def test_pending_deprecations_our_code_raises_stay_visible(request):
    assert _shown(request, PendingDeprecationWarning, "seamless_migrate.store")
