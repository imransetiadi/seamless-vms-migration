import { CircleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCreatePlan, useInventory, useProviders } from '../api/hooks';
import { isDestinationInventory, type PlanCreate, type ProviderKind, type SelectionPolicy, type Strategy } from '../api/types';
import { parseMappings } from '../lib/mappings';
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
  autoRollback: boolean;
}

type FieldKey = 'name' | 'source' | 'destination' | 'vms' | 'slo' | 'window' | 'networks' | 'flavors' | 'volumeTypes' | 'link' | 'threshold' | 'passes' | 'scan' | 'parallel' | 'ports';
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
    autoRollback: true,
  };
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
}

/** Create a draft plan (POST /plans, operator). Validation happens on submit with an error summary. */
export function PlanCreateDialog({ open, onClose, initialSourceId, initialVmIds }: PlanCreateDialogProps) {
  const titleId = useId();
  const id = (key: string) => `plan-create-${key}`;
  const providers = useProviders();
  const create = useCreatePlan();
  const navigate = useNavigate();
  const [form, setForm] = useState<FormState>(() => initialForm(initialSourceId, initialVmIds));
  const [errors, setErrors] = useState<Errors>({});
  const summaryRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const inventory = useInventory(form.sourceId || undefined);

  useEffect(() => {
    if (!open) return;
    setForm(initialForm(initialSourceId, initialVmIds));
    setErrors({});
    create.reset();
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const sources = (providers.data ?? []).filter((p) => p.role === 'source');
  const destinations = (providers.data ?? []).filter((p) => p.role === 'destination');
  const source = sources.find((p) => p.id === form.sourceId);
  const vms = inventory.data && !isDestinationInventory(inventory.data) ? inventory.data : [];
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));

  const validate = (): { errors: Errors; body: PlanCreate | null } => {
    const e: Errors = {};
    if (!form.name.trim()) e.name = 'Enter a plan name.';
    if (!form.sourceId) e.source = 'Choose the source provider.';
    if (!form.destinationId) e.destination = 'Choose the RHOSO destination.';
    if (form.vmIds.size === 0) e.vms = 'Select at least one VM.';
    const slo = Number(form.sloMinutes);
    if (!(slo > 0)) e.slo = 'Enter the downtime SLO in minutes (more than 0).';
    if (Boolean(form.windowStart) !== Boolean(form.windowEnd)) e.window = 'Set both the window start and end, or neither.';
    else if (form.windowStart && Date.parse(form.windowEnd) <= Date.parse(form.windowStart)) e.window = 'The window must end after it starts.';
    const networks = parseMappings(form.networks);
    const flavors = parseMappings(form.flavors);
    const volumeTypes = parseMappings(form.volumeTypes);
    const formatHint = 'Use one "source = destination" pair per line.';
    if (!networks) e.networks = formatHint;
    if (!flavors) e.flavors = formatHint;
    if (!volumeTypes) e.volumeTypes = formatHint;
    const link = Number(form.linkMiBps);
    if (!(link > 0)) e.link = 'Enter the link bandwidth in MiB/s (more than 0).';
    const threshold = Number(form.thresholdGiB);
    if (!(threshold > 0)) e.threshold = 'Enter the convergence threshold in GiB (more than 0).';
    const passes = Number(form.maxPasses);
    if (!Number.isInteger(passes) || passes < 1 || passes > 50) e.passes = 'Enter a whole number of passes from 1 to 50.';
    const overrides: Record<string, number> = {};
    if (form.scanMiBps.trim()) {
      const scan = Number(form.scanMiBps);
      if (!(scan > 0)) e.scan = 'Enter the scan rate in MiB/s (more than 0), or leave it empty.';
      else overrides.scan_bps = scan * MiB;
    }
    if (form.parallelDisks.trim()) {
      const parallel = Number(form.parallelDisks);
      if (!Number.isInteger(parallel) || parallel < 1 || parallel > 64) e.parallel = 'Enter a whole number of disks from 1 to 64, or leave it empty.';
      else overrides.parallel_disks = parallel;
    }
    const ports = form.tcpPorts.split(/[\s,]+/).filter(Boolean).map(Number);
    if (ports.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) e.ports = 'Use port numbers from 1 to 65535, separated by commas.';
    if (Object.keys(e).length) return { errors: e, body: null };
    return {
      errors: e,
      body: {
        name: form.name.trim(),
        description: form.description.trim() || null,
        source_provider_id: form.sourceId,
        destination_provider_id: form.destinationId,
        vm_ids: [...form.vmIds],
        default_strategy: form.defaultStrategy,
        selection_policy: form.policy,
        downtime_slo_s: Math.round(slo * 60),
        require_approval: form.requireApproval,
        auto_cutover: form.autoCutover,
        cutover_window: form.windowStart ? { start: new Date(form.windowStart).toISOString(), end: new Date(form.windowEnd).toISOString() } : null,
        mappings: { networks: networks ?? {}, flavors: flavors ?? {}, volume_types: volumeTypes ?? {}, projects: {} },
        link_bps: link * MiB,
        convergence_threshold_bytes: Math.round(threshold * GiB),
        max_sync_passes: passes,
        estimator_overrides: overrides,
        verification: {
          tcp_ports: ports,
          probe_address: 'fixed',
          console_success_patterns: ['login:', 'Cloud-init v\\. .* finished', 'Reached target .*Multi-User'],
          timeout_s: 600,
          auto_rollback: form.autoRollback,
          use_advisor: true,
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
    create.mutate(body, {
      onSuccess: (plan) => {
        onClose();
        navigate(`/plans/${plan.id}`);
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
  };
  const advancedHasErrors = ['window', 'networks', 'flavors', 'volumeTypes', 'link', 'threshold', 'passes', 'ports'].some((k) => k in errors);

  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} size="lg" dismissible={!create.isPending} initialFocusRef={nameRef}>
      <form onSubmit={submit} noValidate className="flex flex-col">
        <div className="border-b border-border px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold text-foreground">
            New migration plan
          </h2>
          <p className="text-sm text-muted-foreground">Creates a draft. Validate it next to get findings and downtime estimates.</p>
        </div>

        <div className="flex flex-col gap-6 px-5 py-4">
          {errorEntries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" aria-labelledby={id('summary')} className="rounded-md border border-status-danger/40 bg-status-danger/10 p-3 outline-hidden">
              <p id={id('summary')} className="flex items-center gap-2 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                Fix {errorEntries.length} problem{errorEntries.length === 1 ? '' : 's'} to create the plan
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
            <summary className="flex min-h-9 cursor-pointer items-center text-sm font-medium text-foreground">Advanced: window, mappings, sync and verification</summary>
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
                <TextField id={id('ports')} label="Verification TCP ports" value={form.tcpPorts} onChange={(e) => set('tcpPorts', e.target.value)} error={errors.ports} hint="Comma separated, e.g. 22, 443." />
                <Checkbox checked={form.autoRollback} onChange={(v) => set('autoRollback', v)} label="Roll back automatically" hint="When verification fails after the source was stopped." />
              </div>
            </div>
          </details>

          {create.error && <ErrorBanner error={create.error} title="The plan was not created" />}
        </div>

        <div className="sticky bottom-0 flex flex-col-reverse gap-2 border-t border-border bg-card px-5 py-3 sm:flex-row sm:justify-end">
          <Button size="lg" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button size="lg" type="submit" variant="primary" loading={create.isPending}>
            Create plan{form.vmIds.size ? ` with ${form.vmIds.size} VM${form.vmIds.size === 1 ? '' : 's'}` : ''}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
