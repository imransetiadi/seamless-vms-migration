"""``seamless`` command line (SDD §15)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .domain.enums import ProviderKind, Role, Strategy, strategies_for
from .domain.models import Migration, Plan, PlanCreate, Provider, VMRef
from .planning.estimator import EstimatorParams, estimate, invalid_estimator_overrides
from .security.auth import TokenStore, auth_disabled_allowed, generate_token, is_loopback

log = logging.getLogger("seamless_migrate")


class CliError(Exception):
    """A user-facing error (printed to stderr, exit code 1)."""


# -- output helpers --------------------------------------------------------------------------
def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in cells
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())


#: uvicorn installs its own plain-text handlers; ``uvicorn_log_config`` routes them through the
#: root handler of :func:`configure_logging` so JSON logging covers access and error lines too.
def uvicorn_log_config() -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": True},
        },
    }


# -- store-backed helpers ---------------------------------------------------------------------
#: Stores opened by the running command; ``main()`` disposes them (no leaked connections).
_OPEN_STORES: list[Any] = []


def _store(settings: Settings) -> Any:
    from .store import Store

    store = Store(settings.db_url)
    store.create_schema()
    _OPEN_STORES.append(store)
    return store


def _dispose_stores() -> None:
    while _OPEN_STORES:
        store = _OPEN_STORES.pop()
        try:
            store.dispose()
        except Exception:  # pragma: no cover - best effort at exit
            pass


def _services(settings: Settings) -> Any:
    from .api.app import build_services

    return build_services(settings, _store(settings))


def _load_yaml(path: str) -> Any:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise CliError(f"{path} is not valid YAML: {exc}") from None


# -- commands ---------------------------------------------------------------------------------
def run_server(app: Any, settings: Settings, reload: bool) -> None:  # pragma: no cover - I/O
    import uvicorn

    if reload:
        os.environ.update(
            {
                "SEAMLESS_HOST": settings.host,
                "SEAMLESS_PORT": str(settings.port),
                "SEAMLESS_DEMO": str(settings.demo).lower(),
                "SEAMLESS_AUTH_DISABLED": str(settings.auth_disabled).lower(),
            }
        )
        uvicorn.run(
            "seamless_migrate.cli:reload_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            reload=True,
            log_level=settings.log_level.lower(),
            log_config=uvicorn_log_config(),
        )
        return
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=uvicorn_log_config(),
        proxy_headers=True,
    )


def build_app(settings: Settings) -> Any:
    from .api.app import create_app
    from .demo import seed_demo

    hooks = []
    if settings.demo:

        async def seed(services: Any) -> None:
            await seed_demo(services.store, services.settings, services.orchestrator)

        hooks.append(seed)
    return create_app(settings, on_startup=hooks)


def reload_app() -> Any:  # pragma: no cover - used by uvicorn --reload
    settings = Settings.from_env()
    configure_logging(settings)
    return build_app(settings)


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    updates: dict[str, Any] = {"demo": settings.demo or args.demo}
    if args.host is not None:
        updates["host"] = args.host
    if args.port is not None:
        updates["port"] = args.port
    host = updates.get("host", settings.host)
    if updates["demo"] and is_loopback(host) and "SEAMLESS_AUTH_DISABLED" not in os.environ:
        updates["auth_disabled"] = True  # demo on loopback: auth off unless explicitly set
    settings = settings.model_copy(update=updates)
    if settings.auth_disabled and not auth_disabled_allowed(settings.host):
        print(
            f"error: SEAMLESS_AUTH_DISABLED is only allowed on a loopback bind address "
            f"(127.0.0.1, ::1, localhost), not {settings.host!r}",
            file=sys.stderr,
        )
        return 2
    configure_logging(settings)
    if settings.auth_disabled:
        log.warning("authentication is DISABLED (loopback only)")
    run_server(build_app(settings), settings, args.reload)
    return 0


def cmd_token_create(args: argparse.Namespace, settings: Settings) -> int:
    token = generate_token()
    print(token)
    print(TokenStore.yaml_entry(args.name, Role(args.role), token), end="")
    return 0


def _resolve_providers(store: Any, spec: PlanCreate) -> None:
    from .store import NotFound

    for provider_id, role in (
        (spec.source_provider_id, "source"),
        (spec.destination_provider_id, "destination"),
    ):
        try:
            provider = store.get("provider", provider_id, Provider)
        except NotFound:
            raise CliError(f"{role} provider {provider_id!r} does not exist") from None
        if str(provider.role) != role:
            raise CliError(f"provider {provider_id!r} is not a {role} provider")


def cmd_plan_apply(args: argparse.Namespace, settings: Settings) -> int:
    from .events import EventBus, emit

    document = _load_yaml(args.file)
    if not isinstance(document, dict):
        raise CliError("the plan file must contain a mapping (PlanCreate)")
    plan_id = document.pop("id", None)
    try:
        spec = PlanCreate.model_validate(document)
    except ValidationError as exc:
        raise CliError(f"invalid plan: {exc}") from None
    store = _store(settings)
    _resolve_providers(store, spec)
    plans = store.list("plan", Plan)
    existing = next(
        (
            p
            for p in plans
            if (plan_id and p.id == plan_id) or (not plan_id and p.name == spec.name)
        ),
        None,
    )
    if existing is None:
        plan = Plan(**spec.model_dump(), **({"id": plan_id} if plan_id else {}))
        store.put("plan", plan, expected_version=0)
        kind, verb = "plan.created", "created"
    else:
        if str(existing.status) not in ("draft", "validated"):
            raise CliError(f"plan {existing.id} is {existing.status}; it cannot be changed")
        plan = Plan.model_validate(
            {**existing.model_dump(mode="json"), **spec.model_dump(mode="json"), "status": "draft"}
        )
        store.put("plan", plan)
        kind, verb = "plan.updated", "updated"
    asyncio.run(
        emit(
            store,
            EventBus(store),
            kind,
            f"plan {plan.name} {verb} from the CLI",
            plan_id=plan.id,
            actor="cli",
        )
    )
    print(f"{verb} {plan.id}")
    return 0


def cmd_plan_validate(args: argparse.Namespace, settings: Settings) -> int:
    services = _services(settings)
    report = asyncio.run(services.orchestrator.validate_plan(args.plan_id, "cli"))
    rows = []
    for item in report.migrations:
        chosen = next((e for e in item.estimates if e.strategy == item.strategy), None)
        rows.append(
            [
                item.vm_name,
                item.phase,
                item.strategy,
                _seconds(chosen.downtime_s if chosen else None),
                ", ".join(f"{f.code}({f.severity})" for f in item.findings) or "-",
            ]
        )
    print(table(["VM", "PHASE", "STRATEGY", "DOWNTIME_S", "FINDINGS"], rows))
    print()
    est_rows = [
        [
            item.vm_name,
            e.strategy,
            "yes" if e.eligible else "no",
            _seconds(e.downtime_s),
            _seconds(e.precopy_s),
            e.passes,
            "yes" if e.meets_slo else "no",
        ]
        for item in report.migrations
        for e in item.estimates
    ]
    print(
        table(
            ["VM", "STRATEGY", "ELIGIBLE", "DOWNTIME_S", "PRECOPY_S", "PASSES", "MEETS_SLO"],
            est_rows,
        )
    )
    print()
    print("OK: no blockers" if report.ok else "BLOCKED: resolve blocker findings before start")
    return 0 if report.ok else 3


def cmd_plan_start(args: argparse.Namespace, settings: Settings) -> int:
    services = _services(settings)
    plan = asyncio.run(services.orchestrator.start_plan(args.plan_id, "cli"))
    print(f"plan {plan.id} is {plan.status}")
    return 0


def _vms_from(document: Any) -> list[VMRef]:
    items = document.get("vms") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise CliError("the VM file must be a list of VMs or a mapping with a 'vms' list")
    try:
        return [VMRef.model_validate(item) for item in items]
    except ValidationError as exc:
        raise CliError(f"invalid VM: {exc}") from None


def cmd_estimate(args: argparse.Namespace, settings: Settings) -> int:
    vms = _vms_from(_load_yaml(args.file))
    overrides: dict[str, float] = {}
    if args.link_mbps is not None:
        overrides["link_bps"] = args.link_mbps * 1_000_000 / 8
    if args.scan_mibps is not None:
        overrides["scan_bps"] = args.scan_mibps * 2**20
    if args.change_mibps is not None:
        overrides["change_rate_bps"] = args.change_mibps * 2**20
    if args.parallel_disks is not None:
        overrides["parallel_disks"] = args.parallel_disks
    if args.max_passes is not None:
        overrides["max_passes"] = args.max_passes
    problems = invalid_estimator_overrides(
        {k: v for k, v in overrides.items() if k != "max_passes"}
    )
    if problems:
        print("error: " + "; ".join(problems), file=sys.stderr)
        return 2
    params = EstimatorParams(**overrides)  # type: ignore[arg-type]
    rows = []
    for vm in vms:
        vmware = vm.cbt_enabled is not None or any(d.kind == "vmdk" for d in vm.disks)
        kinds = strategies_for(ProviderKind.vmware if vmware else ProviderKind.openstack)
        strategies = [Strategy(args.strategy)] if args.strategy else list(kinds)
        for strategy in strategies:
            est = estimate(vm, strategy, params, args.slo)
            rows.append(
                [
                    vm.name,
                    strategy,
                    _gib(vm.disk_bytes),
                    _gib(vm.used_bytes),
                    _seconds(est.precopy_s),
                    est.passes,
                    _seconds(est.downtime_s),
                    _gib(est.final_delta_bytes),
                    "yes" if est.meets_slo else "no",
                ]
            )
    print(
        table(
            [
                "VM",
                "STRATEGY",
                "DISK_GIB",
                "USED_GIB",
                "PRECOPY_S",
                "PASSES",
                "DOWNTIME_S",
                "FINAL_DELTA_GIB",
                "MEETS_SLO",
            ],
            rows,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    filters = {"plan_id": args.plan} if args.plan else {}
    migrations = store.list("migration", Migration, **filters)
    plans = {p.id: p.name for p in store.list("plan", Plan)}
    rows = [
        [
            m.vm.name,
            plans.get(m.plan_id, m.plan_id),
            m.wave_id or "-",
            m.strategy,
            m.phase,
            f"{m.progress_pct:.0f}%",
            _seconds(m.actual_downtime_s),
            (m.error or "")[:60],
        ]
        for m in migrations
    ]
    print(
        table(["VM", "PLAN", "WAVE", "STRATEGY", "PHASE", "PROGRESS", "DOWNTIME_S", "ERROR"], rows)
    )
    return 0


def cmd_events_export(args: argparse.Namespace, settings: Settings) -> int:
    """Write events as JSON lines (one ``Event`` per line), in sequence order, paged."""
    store = _store(settings)
    out = sys.stdout if args.output in (None, "-") else open(args.output, "w", encoding="utf-8")
    written = 0
    try:
        since = int(args.since_seq)
        while True:
            page = store.events(since_seq=since, plan_id=args.plan, limit=1000)
            if not page:
                break
            for event in page:
                out.write(event.model_dump_json() + "\n")
            written += len(page)
            since = page[-1].seq
    finally:
        if out is not sys.stdout:
            out.close()
    print(f"exported {written} event(s)" + (f" to {args.output}" if out is not sys.stdout else ""),
          file=sys.stderr)  # fmt: skip
    return 0


def cmd_events_prune(args: argparse.Namespace, settings: Settings) -> int:
    """Delete events older than a date or an age; refuses without ``--confirm``."""
    from datetime import datetime, timedelta

    from .domain.models import utcnow

    if args.before:
        before = datetime.fromisoformat(args.before.replace("Z", "+00:00"))
    elif args.older_than_days is not None:
        before = utcnow() - timedelta(days=float(args.older_than_days))
    else:
        print("error: give --before <ISO datetime> or --older-than-days N", file=sys.stderr)
        return 2
    if not args.confirm:
        print(
            f"would delete events before {before.isoformat()}; export them first "
            "(seamless events export) and re-run with --confirm",
            file=sys.stderr,
        )
        return 2
    deleted = _store(settings).delete_events_before(before)
    print(f"deleted {deleted} event(s) before {before.isoformat()}")
    return 0


def cmd_version(args: argparse.Namespace, settings: Settings) -> int:
    print(__version__)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seamless", description="Seamless Migrate control plane")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API, orchestrator and dashboard")
    serve.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="port (default 8080)")
    serve.add_argument("--demo", action="store_true", help="simulated providers and executor")
    serve.add_argument("--reload", action="store_true", help="auto-reload (development)")
    serve.set_defaults(func=cmd_serve)

    ev = sub.add_parser("events", help="export or prune the audit event log")
    ev_sub = ev.add_subparsers(dest="events_command", required=True)
    export = ev_sub.add_parser("export", help="write events as JSON lines (stdout or a file)")
    export.add_argument("-o", "--output", default=None, help="file path (default: stdout)")
    export.add_argument("--since-seq", type=int, default=0, help="start after this sequence")
    export.add_argument("--plan", default=None, help="only events of this plan id")
    export.set_defaults(func=cmd_events_export)
    prune = ev_sub.add_parser("prune", help="delete events older than a date or an age")
    prune.add_argument("--before", default=None, help="ISO 8601 datetime (UTC when naive)")
    prune.add_argument("--older-than-days", type=float, default=None, help="age in days")
    prune.add_argument("--confirm", action="store_true", help="actually delete")
    prune.set_defaults(func=cmd_events_prune)

    token = sub.add_parser("token", help="API tokens").add_subparsers(dest="action", required=True)
    create = token.add_parser("create", help="print a new token and its tokens.yaml entry")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.set_defaults(func=cmd_token_create)

    plan = sub.add_parser("plan", help="plans").add_subparsers(dest="action", required=True)
    apply = plan.add_parser("apply", help="create or update a plan from YAML (PlanCreate)")
    apply.add_argument("-f", "--file", required=True)
    apply.set_defaults(func=cmd_plan_apply)
    validate = plan.add_parser("validate", help="validate a plan: findings and estimates")
    validate.add_argument("plan_id")
    validate.set_defaults(func=cmd_plan_validate)
    start = plan.add_parser("start", help="start a validated plan")
    start.add_argument("plan_id")
    start.set_defaults(func=cmd_plan_start)

    est = sub.add_parser("estimate", help="estimate downtime for VMs described in YAML")
    est.add_argument("-f", "--file", required=True)
    est.add_argument("--strategy", choices=[s.value for s in Strategy])
    est.add_argument("--slo", type=float, default=600.0, help="downtime SLO in seconds")
    est.add_argument("--link-mbps", type=float, default=None, help="link speed in Mbit/s")
    est.add_argument(
        "--scan-mibps",
        type=float,
        default=None,
        help="per-disk-stream scan rate in MiB/s (SDD §9.1 S; e.g. the bench_blocksync result)",
    )
    est.add_argument(
        "--change-mibps",
        type=float,
        default=None,
        help="guest write rate in MiB/s for VMs without change_rate_bps",
    )
    est.add_argument(
        "--parallel-disks", type=int, default=None, help="disks of one VM scanned in parallel"
    )
    est.add_argument("--max-passes", type=int, default=None, help="pre-copy pass cap")
    est.set_defaults(func=cmd_estimate)

    status = sub.add_parser("status", help="migration status")
    status.add_argument("--plan", default=None)
    status.set_defaults(func=cmd_status)

    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        return int(args.func(args, settings))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # orchestrator/store errors surface as messages, not tracebacks
        from .orchestrator import OrchestratorError
        from .providers.base import ProviderError
        from .store import NotFound

        if isinstance(exc, OrchestratorError | ProviderError | NotFound | ValueError):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        raise
    finally:
        _dispose_stores()
