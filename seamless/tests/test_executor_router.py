from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, Strategy
from seamless_migrate.executors.ansible import AnsibleExecutor
from seamless_migrate.executors.handover import HandoverExecutor
from seamless_migrate.executors.router import ExecutorRouter
from seamless_migrate.executors.simulated import SimulatedExecutor


def test_router_picks_executor_per_strategy():
    router = ExecutorRouter(Settings(demo=False))
    assert isinstance(router.for_strategy(Strategy.cold), AnsibleExecutor)
    assert isinstance(router.for_strategy("vmware_warm"), AnsibleExecutor)
    assert isinstance(router.for_strategy(Strategy.storage_handover), HandoverExecutor)
    assert router.for_strategy(Strategy.warm) is router.for_strategy(Strategy.cold)
    assert isinstance(router.prestage_executor(ProviderKind.openstack), AnsibleExecutor)
    assert router.prestage_executor(ProviderKind.vmware) is None


def test_router_uses_the_simulator_in_demo_mode():
    router = ExecutorRouter(Settings(demo=True))
    assert all(isinstance(router.for_strategy(s), SimulatedExecutor) for s in Strategy)
    assert isinstance(router.prestage_executor(ProviderKind.vmware), SimulatedExecutor)
