"""The checks a plan document passes before it is stored (SDD §12, §15): one place for
``POST``/``PATCH /plans`` and ``seamless plan apply``, in one order and with one wording."""

from __future__ import annotations

from typing import Literal

from .domain.models import PlanSpec, invalid_plan_settings, repeated_vm_ids
from .planning.estimator import invalid_estimator_overrides

#: SDD §12: the longest plan name and description a request may set; the stored model stays
#: permissive so plans written before the check keep loading
PLAN_NAME_MAX = 200
PLAN_DESCRIPTION_MAX = 2000


class PlanSpecError(ValueError):
    """A plan document the API refuses; ``code`` is the API's error code for it."""

    def __init__(self, code: Literal["validation_error", "bad_request"], message: str) -> None:
        super().__init__(message)
        self.code = code


def check_plan_spec(spec: PlanSpec) -> None:
    """Raise :class:`PlanSpecError` for the first check ``spec`` fails, in the API's order."""
    texts = []
    if len(spec.name) > PLAN_NAME_MAX:
        texts.append(f"name: at most {PLAN_NAME_MAX} characters (got {len(spec.name)})")
    if spec.description is not None and len(spec.description) > PLAN_DESCRIPTION_MAX:
        texts.append(
            f"description: at most {PLAN_DESCRIPTION_MAX} characters (got {len(spec.description)})"
        )
    if texts:
        raise PlanSpecError("validation_error", "; ".join(texts))
    # one VM, one migration: a repeated id would make two migrations cut it over
    repeated = repeated_vm_ids(spec.vm_ids)
    if repeated:
        raise PlanSpecError(
            "validation_error", f"vm_ids lists a VM more than once: {', '.join(repeated[:10])}"
        )
    settings = invalid_plan_settings(spec)
    if settings:
        raise PlanSpecError("validation_error", "; ".join(settings))
    overrides = invalid_estimator_overrides(spec.estimator_overrides)
    if overrides:
        raise PlanSpecError("bad_request", f"invalid estimator_overrides: {'; '.join(overrides)}")
