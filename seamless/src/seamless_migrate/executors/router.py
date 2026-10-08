"""Choose the executor for a strategy (simulated in demo mode)."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..domain.enums import ProviderKind, Strategy
from .ansible import AnsibleExecutor
from .base import Executor
from .handover import HandoverExecutor
from .simulated import SimulatedExecutor


class ExecutorRouter:
    def __init__(
        self,
        settings: Settings,
        *,
        simulated: Any = None,
        ansible: Any = None,
        handover: Any = None,
        providers: Any = None,
    ) -> None:
        self.settings = settings
        self._simulated = simulated
        self._ansible = ansible
        self._handover = handover
        self._providers = providers

    @property
    def simulated(self) -> Any:
        if self._simulated is None:
            self._simulated = SimulatedExecutor(self.settings)
        return self._simulated

    @property
    def ansible(self) -> Any:
        if self._ansible is None:
            self._ansible = AnsibleExecutor(self.settings, providers=self._providers)
        return self._ansible

    @property
    def handover(self) -> Any:
        if self._handover is None:
            self._handover = HandoverExecutor(self.settings)
        return self._handover

    def for_strategy(self, strategy: Strategy | str) -> Executor:
        strategy = Strategy(strategy)
        if self.settings.demo:
            return self.simulated
        if strategy == Strategy.storage_handover:
            return self.handover
        return self.ansible

    def prestage_executor(self, source_kind: ProviderKind | str) -> Executor | None:
        """The executor that pre-stages plan resources (``None`` when nothing to do)."""
        if self.settings.demo:
            return self.simulated
        if ProviderKind(source_kind) == ProviderKind.vmware:
            return None
        return self.ansible
