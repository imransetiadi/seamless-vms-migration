import { CircleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCreatePlan, useInventory, usePatchPlan, useProviders } from '../api/hooks';
import { isDestinationInventory, type Plan, type PlanCreate, type PlanPatch, type ProviderKind, type SelectionPolicy, type Strategy } from '../api/types';
import { formatMappings, parseMappings } from '../lib/mappings';
import { FAMILY_LABELS, handoverTargets, resolveDestination, splitHost, storageBackends, volumeTypeFamilies } from '../lib/storage';
import { providerStatusMeta, STRATEGY_LABELS } from '../lib/status';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';
import { SelectField, TextAreaField, TextField } from './Field';
import { Modal } from './Modal';
import { LoadingBlock } from './Skeleton';
import { VmTable } from './VmTable';

const GiB = 1024 ** 3;
const MiB = 1024 ** 2;

interface FormState {
  name: string;
  description: string;
  sourceId: string;
  destinationId: string;
  vmIds: Set<string>;
  defaultStrategy: Strategy | 'auto';
  policy: SelectionPolicy;
  sloMinutes: string;
  requireApproval: boolean;
  autoCutover: boolean;
  windowStart: string;
  windowEnd: string;
  networks: string;
  flavors: string;
  volumeTypes: string;
  linkMiBps: string;
  thresholdGiB: string;
  maxPasses: string;
  /** Optional estimator overrides (SDD §9.1): empty = planning default. */
  scanMiBps: string;
  parallelDisks: string;
  tcpPorts: string;
  windowsTcpPorts: string;
  autoRollback: boolean;
  /** Storage handover (SDD §7.3): volume type -> RHOSO backend chosen by the operator. */
  handoverEnabled: boolean;
  handoverMap: Record<string, string>;
}

type FieldKey = 'windowsPorts' | 'name' | 'source' | 'destination' | 'vms' | 'slo' | 'window' | 'networks' | 'flavors' | 'volumeTypes' | 'link' | 'threshold' | 'passes' | 'scan' | 'parallel' | 'ports' | 'handover';
type Errors = Partial<Record<FieldKey, string>>;

function initialForm(sourceId = '', vmIds: string[] = []): FormState {
  return {
    name: '',
    description: '',
    sourceId,
    destinationId: '',
    vmIds: new Set(vmIds),
    defaultStrategy: 'auto',
    policy: 'min_downtime',
    sloMinutes: '10',
    requireApproval: true,
    autoCutover: false,
    windowStart: '',
    windowEnd: '',
    networks: '',
    flavors: '',
    volumeTypes: '',
    linkMiBps: '125',
    thresholdGiB: '1',
    maxPasses: '5',
    scanMiBps: '',
    parallelDisks: '',
    tcpPorts: '22',
    windowsTcpPorts: '3389',
    autoRollback: true,
    handoverEnabled: false,
    handoverMap: {},
  };
}

const toLocalInput = (iso: string): string => {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const plain = (n: number): string => String(Number(n.toFixed(3)));

const DEFAULT_VERIFICATION = {
  probe_address: 'fixed' as const,
  console_success_patterns: ['login:', 'Cloud-init v\\. .* finished', 'Reached target .*Multi-User'],
  timeout_s: 600,
  use_advisor: true,
};

/** Estimator overrides the form does not edit (it edits scan_bps and parallel_disks). */
function withoutFormOverrides(overrides: Record<string, number> | undefined): Record<string, number> {
  const { scan_bps: _scan, parallel_disks: _parallel, ...rest } = overrides ?? {};
  return rest;
}

/** The form of an existing plan (edit mode, PATCH /plans/{id}). */
function formFromPlan(plan: Plan): FormState {
  const v = plan.verification;
  return {
    ...initialForm(plan.source_provider_id, plan.vm_ids),
    name: plan.name,
    description: plan.description ?? '',
    destinationId: plan.destination_provider_id,
    defaultStrategy: plan.default_strategy,
    policy: plan.selection_policy,
    sloMinutes: plain(plan.downtime_slo_s / 60),
    requireApproval: plan.require_approval,
    autoCutover: plan.auto_cutover,
    windowStart: plan.cutover_window ? toLocalInput(plan.cutover_window.start) : '',
    windowEnd: plan.cutover_window ? toLocalInput(plan.cutover_window.end) : '',
    networks: formatMappings(plan.mappings.networks),
    flavors: formatMappings(plan.mappings.flavors),
    volumeTypes: formatMappings(plan.mappings.volume_types),
    linkMiBps: plain(plan.link_bps / MiB),
    thresholdGiB: plain(plan.convergence_threshold_bytes / GiB),
    maxPasses: String(plan.max_sync_passes),
    scanMiBps: plan.estimator_overrides.scan_bps ? plain(plan.estimator_overrides.scan_bps / MiB) : '',
    parallelDisks: plan.estimator_overrides.parallel_disks ? String(plan.estimator_overrides.parallel_disks) : '',
    tcpPorts: v.tcp_ports.join(', '),
    windowsTcpPorts: (v.windows_tcp_ports ?? []).join(', '),
    autoRollback: v.auto_rollback,
    handoverEnabled: plan.handover.enabled,
    handoverMap: { ...plan.handover.backend_map },
  };
}

/** JSON with sorted object keys, so equal values compare equal whatever their key order. */
function stable(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
}

function strategiesFor(kind: ProviderKind | undefined): Strategy[] {
  if (kind === 'vmware') return ['vmware_cold', 'vmware_warm'];
  return ['cold', 'warm', 'storage_handover'];
}

function Checkbox({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string }) {
  return (
    <label className="flex min-h-11 cursor-pointer items-start gap-2.5 py-1">
      <input type="checkbox" className="mt-1 size-4 shrink-0 cursor-pointer accent-accent" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="text-sm text-foreground">
        {label}
        <span className="block text-xs text-muted-foreground">{hint}</span>
      </span>
    </label>
  );
}

function Fieldset({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-3">
      <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{legend}</legend>
      {children}
    </fieldset>
  );
}

export interface PlanCreateDialogProps {
  open: boolean;
  onClose: () => void;
  initialSourceId?: string;
  initialVmIds?: string[];
  /** Edit this plan instead of creating one (PATCH /plans/{id}; only the changed fields are sent). */
  plan?: Plan;
}

/** Create a draft plan (POST /plans, operator). Validation happens on submit with an error summary. */
export function PlanCreateDialog({ open, onClose, initialSourceId, initialVmIds, plan }: PlanCreateDialogProps) {
  const titleId = useId();
  const id = (key: string) => `plan-create-${key}`;
  const providers = useProviders();
  const create = useCreatePlan();
  const patch = usePatchPlan(plan?.id ?? '');
  const editing = Boolean(plan);
  const saving = editing ? patch : create;
  const navigate = useNavigate();
  const [form, setForm] = useState<FormState>(() => (plan ? formFromPlan(plan) : initialForm(initialSourceId, initialVmIds)));
  const [errors, setErrors] = useState<Errors>({});
  const summaryRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const inventory = useInventory(form.sourceId || undefined);

  useEffect(() => {
    if (!open) return;
    setForm(plan ? formFromPlan(plan) : initialForm(initialSourceId, initialVmIds));
    setErrors({});
    create.reset();
    patch.reset();
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const sources = (providers.data ?? []).filter((p) => p.role === 'source');
  const destinations = (providers.data ?? []).filter((p) => p.role === 'destination');
  const source = sources.find((p) => p.id === form.sourceId);
  const vms = inventory.data && !isDestinationInventory(inventory.data) ? inventory.data : [];
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));

  // storage handover (SDD §7.3, §7.3.1): one RHOSO backend per volume type of the selected VMs
  const destination = destinations.find((p) => p.id === form.destinationId);
  const selectedVms = vms.filter((vm) => form.vmIds.has(vm.source_id));
  const sourceStorage = source ? storageBackends(source.capabilities) : [];
  const destinationStorage = destination ? storageBackends(destination.capabilities) : [];
  const typeFamilies = volumeTypeFamilies(selectedVms, sourceStorage);
  const handoverTypes = [...typeFamilies.keys()];
  const targetsFor = (type: string) => handoverTargets(typeFamilies.get(type) ?? null, destinationStorage);
  const chosenTarget = (type: string, f: FormState = form): string => {
    const targets = targetsFor(type);
    return f.handoverMap[type] ?? (targets.length === 1 ? targets[0]! : '');
  };
  /** Destination pools (or the reasons a volume cannot be handed over) for one volume type. */
  const landing = (type: string, f: FormState = form): { pools: string[]; errors: string[] } => {
    const target = chosenTarget(type, f);
    const families = new Map(sourceStorage.map((b) => [b.pool, b.family]));
    const pools = new Set<string>();
    const problems = new Set<string>();
    if (!target) return { pools: [], errors: [] };
    for (const vm of selectedVms) {
      for (const disk of vm.disks) {
        if (disk.kind !== 'volume' || disk.volume_type !== type || !disk.pool || !families.has(disk.pool)) continue;
        const { host, error } = resolveDestination(families.get(disk.pool)!, splitHost(disk.pool)[1], target, destinationStorage);
        if (host) pools.add(host);
        if (error) problems.add(`${vm.name}: ${error}`);
      }
    }
    return { pools: [...pools].sort(), errors: [...problems] };
  };

  const validate = (f: FormState = form): { errors: Errors; body: PlanCreate | null } => {
    const e: Errors = {};
    if (!f.name.trim()) e.name = 'Enter a plan name.';
    if (!f.sourceId) e.source = 'Choose the source provider.';
    if (!f.destinationId) e.destination = 'Choose the RHOSO destination.';
    if (f.vmIds.size === 0) e.vms = 'Select at least one VM.';
    const slo = Number(f.sloMinutes);
    if (!(slo > 0)) e.slo = 'Enter the downtime SLO in minutes (more than 0).';
    if (Boolean(f.windowStart) !== Boolean(f.windowEnd)) e.window = 'Set both the window start and end, or neither.';
    else if (f.windowStart && Date.parse(f.windowEnd) <= Date.parse(f.windowStart)) e.window = 'The window must end after it starts.';
    const networks = parseMappings(f.networks);
    const flavors = parseMappings(f.flavors);
    const volumeTypes = parseMappings(f.volumeTypes);
    const formatHint = 'Use one "source = destination" pair per line.';
    if (!networks) e.networks = formatHint;
    if (!flavors) e.flavors = formatHint;
    if (!volumeTypes) e.volumeTypes = formatHint;
    const link = Number(f.linkMiBps);
    if (!(link > 0)) e.link = 'Enter the link bandwidth in MiB/s (more than 0).';
    const threshold = Number(f.thresholdGiB);
    if (!(threshold > 0)) e.threshold = 'Enter the convergence threshold in GiB (more than 0).';
    const passes = Number(f.maxPasses);
    if (!Number.isInteger(passes) || passes < 1 || passes > 50) e.passes = 'Enter a whole number of passes from 1 to 50.';
    const overrides: Record<string, number> = {};
    if (f.scanMiBps.trim()) {
      const scan = Number(f.scanMiBps);
      if (!(scan > 0)) e.scan = 'Enter the scan rate in MiB/s (more than 0), or leave it empty.';
      else overrides.scan_bps = scan * MiB;
    }
    if (f.parallelDisks.trim()) {
      const parallel = Number(f.parallelDisks);
      if (!Number.isInteger(parallel) || parallel < 1 || parallel > 64) e.parallel = 'Enter a whole number of disks from 1 to 64, or leave it empty.';
      else overrides.parallel_disks = parallel;
    }
    const ports = f.tcpPorts.split(/[\s,]+/).filter(Boolean).map(Number);
    if (ports.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) e.ports = 'Use port numbers from 1 to 65535, separated by commas.';
    const windowsPorts = f.windowsTcpPorts.split(/[\s,]+/).filter(Boolean).map(Number);
    if (windowsPorts.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) e.windowsPorts = 'Use port numbers from 1 to 65535, separated by commas.';
    const backendMap: Record<string, string> = {};
    if (f.handoverEnabled && source?.kind !== 'vmware') {
      const missing = handoverTypes.filter((t) => !chosenTarget(t, f));
      const problems = handoverTypes.flatMap((t) => landing(t, f).errors);
      // a new plan maps every type; an existing one may keep unmapped types (those VMs are not eligible)
      if (missing.length && !plan) e.handover = `Choose a RHOSO backend for ${missing.join(', ')}.`;
      else if (problems.length) e.handover = problems[0];
      for (const t of handoverTypes) if (chosenTarget(t, f)) backendMap[t] = chosenTarget(t, f);
    }
    if (Object.keys(e).length) return { errors: e, body: null };
    return {
      errors: e,
      body: {
        name: f.name.trim(),
        description: f.description.trim() || null,
        source_provider_id: f.sourceId,
        destination_provider_id: f.destinationId,
        vm_ids: [...f.vmIds],
        default_strategy: f.defaultStrategy,
        selection_policy: f.policy,
        downtime_slo_s: Math.round(slo * 60),
        require_approval: f.requireApproval,
        auto_cutover: f.autoCutover,
        cutover_window: f.windowStart ? { start: new Date(f.windowStart).toISOString(), end: new Date(f.windowEnd).toISOString() } : null,
        mappings: { networks: networks ?? {}, flavors: flavors ?? {}, volume_types: volumeTypes ?? {}, projects: plan?.mappings.projects ?? {} },
        handover: { enabled: f.handoverEnabled && source?.kind !== 'vmware', backend_map: backendMap },
        link_bps: link * MiB,
        convergence_threshold_bytes: Math.round(threshold * GiB),
        max_sync_passes: passes,
        estimator_overrides: { ...withoutFormOverrides(plan?.estimator_overrides), ...overrides },
        verification: {
          ...DEFAULT_VERIFICATION,
          ...plan?.verification,
          tcp_ports: ports,
          windows_tcp_ports: windowsPorts,
          auto_rollback: f.autoRollback,
        },
      },
    };
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const { errors: found, body } = validate();
    setErrors(found);
    if (!body) {
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    if (plan) {
      // only what the operator changed: compare with the body the untouched form produces, so rounding
      // and fields the form does not show never count as changes (and never touch approver-only policy)
      const baseline = (validate(formFromPlan(plan)).body ?? {}) as Record<string, unknown>;
      const changes = Object.fromEntries(Object.entries(body).filter(([key, value]) => stable(value) !== stable(baseline[key])));
      if (Object.keys(changes).length === 0) {
        onClose();
        return;
      }
      patch.mutate(changes as PlanPatch, { onSuccess: () => onClose() });
      return;
    }
    create.mutate(body, {
      onSuccess: (created) => {
        onClose();
        navigate(`/plans/${created.id}`);
      },
    });
  };

  const errorEntries = Object.entries(errors) as Array<[FieldKey, string]>;
  const targetFor: Record<FieldKey, string> = {
    name: id('name'),
    source: id('source'),
    destination: id('destination'),
    vms: id('vms'),
    slo: id('slo'),
    window: id('window-start'),
    networks: id('networks'),
    flavors: id('flavors'),
    volumeTypes: id('volume-types'),
    link: id('link'),
    threshold: id('threshold'),
    passes: id('passes'),
    scan: id('scan'),
    parallel: id('parallel'),
    ports: id('ports'),
    windowsPorts: id('windows-ports'),
    handover: handoverTypes.length ? id(`handover-${handoverTypes[0]}`) : id('handover'),
  };
  const advancedHasErrors = ['window', 'networks', 'flavors', 'volumeTypes', 'link', 'threshold', 'passes', 'scan', 'parallel', 'ports', 'windowsPorts', 'handover'].some((k) => k in errors);

  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} size="lg" dismissible={!saving.isPending} initialFocusRef={nameRef}>
      <form onSubmit={submit} noValidate className="flex flex-col">
        <div className="border-b border-border px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold text-foreground">
            {plan ? 'Edit plan' : 'New migration plan'}
          </h2>
          <p className="text-sm text-muted-foreground">
            {!plan
              ? 'Creates a draft. Validate it next to get findings and downtime estimates.'
              : plan.status === 'validated'
                ? 'Saving returns the plan to draft: validate it again before starting.'
                : 'Only the settings you change are saved.'}
          </p>
        </div>

        <div className="flex flex-col gap-6 px-5 py-4">
          {errorEntries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" aria-labelledby={id('summary')} className="rounded-md border border-status-danger/40 bg-status-danger/10 p-3 outline-hidden">
              <p id={id('summary')} className="flex items-center gap-2 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                Fix {errorEntries.length} problem{errorEntries.length === 1 ? '' : 's'} to {plan ? 'save' : 'create'} the plan
              </p>
              <ul className="mt-1 list-disc pl-6 text-sm">
                {errorEntries.map(([key, message]) => (
                  <li key={key}>
                    <a
                      href={`#${targetFor[key]}`}
                      className="text-foreground underline underline-offset-4"
                      onClick={(e) => {
                        e.preventDefault();
                        if (advancedHasErrors) (document.getElementById(id('advanced')) as HTMLDetailsElement | null)?.setAttribute('open', '');
                        document.getElementById(targetFor[key])?.focus();
                      }}
                    >
                      {message}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <Fieldset legend="Plan">
            <TextField ref={nameRef} id={id('name')} label="Name" required value={form.name} onChange={(e) => set('name', e.target.value)} error={errors.name} autoComplete="off" />
            <TextAreaField id={id('description')} label="Description" value={form.description} onChange={(e) => set('description', e.target.value)} rows={2} />
          </Fieldset>

          <Fieldset legend="Clouds">
            <div className="grid gap-3 sm:grid-cols-2">
              <SelectField
                id={id('source')}
                label="Source provider"
                required
                value={form.sourceId}
                onChange={(e) => setForm((f) => ({ ...f, sourceId: e.target.value, vmIds: new Set(), defaultStrategy: 'auto' }))}
                disabled={editing}
                hint={editing ? 'The source of an existing plan stays the same; create a new plan for another source.' : undefined}
                error={errors.source}
                options={[
                  { value: '', label: 'Choose a source…' },
                  ...sources.map((p) => ({ value: p.id, label: `${p.name} · ${providerStatusMeta(p.status).label}` })),
                ]}
              />
              <SelectField
                id={id('destination')}
                label="Destination (RHOSO 18.0)"
                required
                value={form.destinationId}
                onChange={(e) => set('destinationId', e.target.value)}
                error={errors.destination}
                options={[
                  { value: '', label: 'Choose a destination…' },
                  ...destinations.map((p) => ({ value: p.id, label: `${p.name} · ${providerStatusMeta(p.status).label}` })),
                ]}
              />
            </div>
          </Fieldset>

          <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={errors.vms ? id('vms-error') : undefined}>
            <legend id={id('vms')} tabIndex={-1} className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground outline-hidden">
              VMs <span aria-hidden className="text-status-danger">*</span>
            </legend>
            {errors.vms && (
              <p id={id('vms-error')} className="field-error">
                <CircleAlert aria-hidden className="size-3.5" />
                {errors.vms}
              </p>
            )}
            {!form.sourceId && <p className="text-sm text-muted-foreground">Choose a source provider to list its VMs.</p>}
            {form.sourceId && inventory.isPending && <LoadingBlock label="Reading the source inventory…" rows={3} />}
            {inventory.error && <ErrorBanner error={inventory.error} title="Cannot read the source inventory" onRetry={() => void inventory.refetch()} />}
            {source && vms.length > 0 && (
              <VmTable
                vms={vms}
                providerKind={source.kind}
                selected={form.vmIds}
                onSelectedChange={(next) => set('vmIds', next)}
                caption={`VMs on ${source.name}`}
                pageSize={25}
              />
            )}
          </fieldset>

          <Fieldset legend="Strategy and downtime">
            <div className="grid gap-3 sm:grid-cols-3">
              <SelectField
                id={id('strategy')}
                label="Default strategy"
                value={form.defaultStrategy}
                onChange={(e) => set('defaultStrategy', e.target.value as Strategy | 'auto')}
                hint="Auto picks the eligible strategy per VM."
                options={[{ value: 'auto', label: 'Auto' }, ...strategiesFor(source?.kind).map((s) => ({ value: s, label: STRATEGY_LABELS[s] }))]}
              />
              <SelectField
                id={id('policy')}
                label="Selection policy"
                value={form.policy}
                onChange={(e) => set('policy', e.target.value as SelectionPolicy)}
                options={[
                  { value: 'min_downtime', label: 'Minimum downtime' },
                  { value: 'simplest_meeting_slo', label: 'Simplest meeting the SLO' },
                ]}
              />
              <TextField
                id={id('slo')}
                label="Downtime SLO (minutes)"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                required
                value={form.sloMinutes}
                onChange={(e) => set('sloMinutes', e.target.value)}
                error={errors.slo}
              />
            </div>
            <div className="grid gap-x-4 sm:grid-cols-2">
              <Checkbox checked={form.requireApproval} onChange={(v) => set('requireApproval', v)} label="Require approval" hint="An approver must approve every cutover." />
              <Checkbox checked={form.autoCutover} onChange={(v) => set('autoCutover', v)} label="Automatic cutover" hint="Cut over as soon as the gate opens, without a request." />
            </div>
          </Fieldset>

          <details id={id('advanced')} className="rounded-md border border-border px-3 py-2" open={advancedHasErrors || undefined}>
            <summary className="flex min-h-9 cursor-pointer items-center text-sm font-medium text-foreground">Advanced: window, mappings, sync, verification and storage handover</summary>
            <div className="mt-3 flex flex-col gap-4 pb-2">
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField id={id('window-start')} label="Cutover window start" type="datetime-local" value={form.windowStart} onChange={(e) => set('windowStart', e.target.value)} error={errors.window} hint="Local time; leave empty for any time." />
                <TextField id={id('window-end')} label="Cutover window end" type="datetime-local" value={form.windowEnd} onChange={(e) => set('windowEnd', e.target.value)} />
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <TextAreaField id={id('networks')} label="Network mappings" rows={3} placeholder="tenant-net = tenant-net-ovn" value={form.networks} onChange={(e) => set('networks', e.target.value)} error={errors.networks} />
                <TextAreaField id={id('flavors')} label="Flavor mappings" rows={3} placeholder="m1.xlarge = m2.xlarge" value={form.flavors} onChange={(e) => set('flavors', e.target.value)} error={errors.flavors} />
                <TextAreaField id={id('volume-types')} label="Volume type mappings" rows={3} placeholder="tripleo-ceph = ceph-ssd" value={form.volumeTypes} onChange={(e) => set('volumeTypes', e.target.value)} error={errors.volumeTypes} />
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <TextField id={id('link')} label="Link bandwidth (MiB/s)" type="number" inputMode="decimal" min={0} step="any" value={form.linkMiBps} onChange={(e) => set('linkMiBps', e.target.value)} error={errors.link} />
                <TextField id={id('threshold')} label="Convergence threshold (GiB)" type="number" inputMode="decimal" min={0} step="any" value={form.thresholdGiB} onChange={(e) => set('thresholdGiB', e.target.value)} error={errors.threshold} />
                <TextField id={id('passes')} label="Max sync passes" type="number" inputMode="numeric" min={1} max={50} value={form.maxPasses} onChange={(e) => set('maxPasses', e.target.value)} error={errors.passes} />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField
                  id={id('scan')}
                  label="Scan rate per disk stream (MiB/s, optional)"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="any"
                  placeholder="500 (planning default)"
                  value={form.scanMiBps}
                  onChange={(e) => set('scanMiBps', e.target.value)}
                  error={errors.scan}
                  hint="Measured with bench_blocksync on the conversion host flavor; delta passes recalibrate it per VM"
                />
                <TextField
                  id={id('parallel')}
                  label="Disks scanned in parallel (optional)"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={64}
                  placeholder="4 (planning default)"
                  value={form.parallelDisks}
                  onChange={(e) => set('parallelDisks', e.target.value)}
                  error={errors.parallel}
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField id={id('ports')} label="Verification TCP ports" value={form.tcpPorts} onChange={(e) => set('tcpPorts', e.target.value)} error={errors.ports} hint="Linux and other guests. Comma separated, e.g. 22, 443." />
                <TextField
                  id={id('windows-ports')}
                  label="Windows verification TCP ports"
                  value={form.windowsTcpPorts}
                  onChange={(e) => set('windowsTcpPorts', e.target.value)}
                  error={errors.windowsPorts}
                  hint="RDP 3389 or WinRM 5985. Windows guests skip the console check."
                />
                <Checkbox checked={form.autoRollback} onChange={(v) => set('autoRollback', v)} label="Roll back automatically" hint="When verification fails after the source was stopped." />
              </div>
              {source?.kind !== 'vmware' && (
                <Fieldset legend="Storage handover">
                  <input id={id('handover')} type="hidden" />
                  <Checkbox
                    checked={form.handoverEnabled}
                    onChange={(v) => set('handoverEnabled', v)}
                    label="Hand volumes over without copying"
                    hint="RHOSO takes over the volumes where they are (Cinder unmanage and manage). Both clouds need admin rights and must reach the same Ceph pool or NetApp ONTAP SVM."
                  />
                  {form.handoverEnabled &&
                    (handoverTypes.length === 0 ? (
                      <p className="text-sm text-muted-foreground">Select VMs with Cinder volumes to map their volume types.</p>
                    ) : (
                      <div className="grid gap-3 sm:grid-cols-2">
                        {handoverTypes.map((type, index) => {
                          const family = typeFamilies.get(type) ?? null;
                          const targets = targetsFor(type);
                          const where = landing(type);
                          const hint = where.pools.length
                            ? `Lands on ${where.pools.join(', ')}`
                            : editing && !chosenTarget(type)
                              ? 'Not mapped: VMs with this volume type are not eligible for storage handover.'
                            : targets.length === 0
                              ? 'The destination reports no compatible backend.'
                              : family === 'netapp_nfs' || family === 'netapp_block'
                                ? 'The export or FlexVol is matched per volume.'
                                : undefined;
                          return (
                            <SelectField
                              key={type}
                              id={id(`handover-${type}`)}
                              label={`${type}${family ? ` on ${FAMILY_LABELS[family]}` : ''}`}
                              value={chosenTarget(type)}
                              onChange={(e) => set('handoverMap', { ...form.handoverMap, [type]: e.target.value })}
                              options={[{ value: '', label: 'Choose a RHOSO backend' }, ...targets.map((t) => ({ value: t, label: t }))]}
                              hint={hint}
                              error={index === 0 ? errors.handover : undefined}
                            />
                          );
                        })}
                      </div>
                    ))}
                </Fieldset>
              )}
            </div>
          </details>

          {saving.error && <ErrorBanner error={saving.error} title={plan ? 'The plan was not saved' : 'The plan was not created'} />}
        </div>

        <div className="sticky bottom-0 flex flex-col-reverse gap-2 border-t border-border bg-card px-5 py-3 sm:flex-row sm:justify-end">
          <Button size="lg" onClick={onClose} disabled={saving.isPending}>
            Cancel
          </Button>
          <Button size="lg" type="submit" variant="primary" loading={saving.isPending}>
            {plan ? 'Save changes' : `Create plan${form.vmIds.size ? ` with ${form.vmIds.size} VM${form.vmIds.size === 1 ? '' : 's'}` : ''}`}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
