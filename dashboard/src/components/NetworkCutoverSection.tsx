import { ArrowUp, CircleAlert, Plus, Trash2 } from 'lucide-react';
import { useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useHooks } from '../api/hooks';
import type { HookPoint } from '../api/types';
import { HOOK_POINTS, NEW_DNS_TARGET, targetInPlan, type CutoverDraft, type DnsTargetDraft, type PoolMappingDraft } from '../lib/cutover';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';
import { SelectField, TextField } from './Field';
import { TechPreviewBadge } from './TechPreviewBadge';

export interface NetworkCutoverSectionProps {
  /** The section's id: an error summary moves focus here. */
  id: string;
  value: CutoverDraft;
  onChange: (value: CutoverDraft) => void;
  /** The plan's selected VMs, which a DNS target names. */
  vms: ReadonlyArray<{ source_id: string; name: string }>;
  /** What the form refuses in the section, once submitted. */
  error?: string | null;
}

/**
 * The plan form's network cut-over (SDD §16, §23.3-§23.7, plan task N12b): the floating IP move, each point's hooks
 * in their run order, the DNS targets and the Octavia pool mappings, with the network Technology Preview badge.
 */
export function NetworkCutoverSection({ id, value, onChange, vms, error }: NetworkCutoverSectionProps) {
  const legendId = useId();
  const errorId = useId();
  const catalog = useHooks();
  const set = (patch: Partial<CutoverDraft>) => onChange({ ...value, ...patch });
  return (
    <fieldset id={id} tabIndex={-1} aria-labelledby={legendId} aria-describedby={error ? errorId : undefined} className="flex min-w-0 flex-col gap-4 outline-hidden">
      <div className="flex flex-wrap items-center gap-2">
        <legend id={legendId} className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Network cut-over
        </legend>
        <TechPreviewBadge feature="network" />
      </div>
      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="field-label">Floating IPs</legend>
        {(
          [
            ['auto', 'New addresses', "os-migrate's: the destination may give a floating IP a new address."],
            ['keep_address', 'Keep each address', 'The cutover moves each floating IP with its address to the new server.'],
          ] as const
        ).map(([mode, label, hint]) => (
          <label key={mode} className="text-sm">
            <input type="radio" name={`${id}-floating`} value={mode} checked={value.floatingIps === mode} onChange={() => set({ floatingIps: mode })} className="mr-2 align-middle" />
            <span className="font-medium">{label}</span> <span className="text-muted-foreground">{hint}</span>
          </label>
        ))}
      </fieldset>
      {HOOK_POINTS.map(([point, label]) => (
        <PointHooks
          key={point}
          point={point}
          label={label}
          names={value.hooks[point] ?? []}
          catalog={catalog}
          onChange={(names) => set({ hooks: { ...value.hooks, [point]: names } })}
        />
      ))}
      <DnsTargets drafts={value.dnsTargets} vms={vms} onChange={(dnsTargets) => set({ dnsTargets })} />
      <PoolMappings drafts={value.poolMappings} onChange={(poolMappings) => set({ poolMappings })} />
      {error && (
        <p id={errorId} className="field-error">
          <CircleAlert aria-hidden className="size-3.5 shrink-0" />
          {error}
        </p>
      )}
    </fieldset>
  );
}

function PointHooks({
  point,
  label,
  names,
  catalog,
  onChange,
}: {
  point: HookPoint;
  label: string;
  names: string[];
  catalog: ReturnType<typeof useHooks>;
  onChange: (names: string[]) => void;
}) {
  const [choice, setChoice] = useState('');
  const legendRef = useRef<HTMLLegendElement>(null);
  const offered = (catalog.data ?? []).map((hook) => hook.name).filter((name) => !names.includes(name));
  const picked = offered.includes(choice) ? choice : '';
  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend ref={legendRef} tabIndex={-1} className="field-label outline-hidden">
        {label} ({point})
      </legend>
      {names.length > 0 && (
        <ol className="flex flex-col gap-1">
          {names.map((name, i) => (
            <li key={name} aria-label={name} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-2 py-1 text-sm">
              <span>
                {i + 1}. {name}
              </span>
              <span className="flex gap-1">
                {i > 0 && (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    icon={ArrowUp}
                    aria-label={`Move ${name} up`}
                    onClick={() => onChange(names.map((n, j) => (j === i - 1 ? name : j === i ? names[i - 1]! : n)))}
                  />
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label={`Remove ${name}`}
                  onClick={() => {
                    onChange(names.filter((n) => n !== name));
                    legendRef.current?.focus();
                  }}
                />
              </span>
            </li>
          ))}
        </ol>
      )}
      {catalog.error ? (
        <ErrorBanner error={catalog.error} title="The hook catalog could not be read" onRetry={() => void catalog.refetch()} />
      ) : catalog.data && catalog.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No hooks registered: an admin registers them on the <Link to="/hooks" className="underline underline-offset-4">Hooks page</Link>.
        </p>
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            label="Hook to add"
            value={picked}
            onChange={(event) => setChoice(event.target.value)}
            options={[{ value: '', label: offered.length ? 'Choose a hook' : 'Every hook runs here' }, ...offered.map((name) => ({ value: name, label: name }))]}
            disabled={!catalog.data}
          />
          <Button
            type="button"
            size="sm"
            icon={Plus}
            disabledReason={!picked ? 'Choose a hook to add.' : null}
            onClick={() => {
              onChange([...names, picked]);
              setChoice('');
            }}
          >
            Add hook
          </Button>
        </div>
      )}
    </fieldset>
  );
}

function DnsTargets({ drafts, vms, onChange }: { drafts: DnsTargetDraft[]; vms: NetworkCutoverSectionProps['vms']; onChange: (drafts: DnsTargetDraft[]) => void }) {
  const legendRef = useRef<HTMLLegendElement>(null);
  const set = (index: number, patch: Partial<DnsTargetDraft>) => onChange(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  // numbered as shown, each with its place among the drafts
  const selected = new Set(vms.map((vm) => vm.source_id));
  const shown = drafts.flatMap((draft, index) => (targetInPlan(draft, selected) ? [{ draft, index }] : []));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend ref={legendRef} tabIndex={-1} className="field-label outline-hidden">
        DNS targets
      </legend>
      <p className="-mt-1 text-xs text-muted-foreground">
        The record of a VM moves to the address that takes its place, by a compare-and-swap: Designate with the destination provider&apos;s credential, or an RFC 2136 server with a TSIG key.
      </p>
      {shown.length > 0 && (
        <ol className="flex flex-col gap-2">
          {shown.map(({ draft, index }, i) => {
            const n = i + 1;
            const prefix = (
              <>
                <span className="sr-only">DNS target {n}</span>{' '}
              </>
            );
            return (
              <li key={index} className="grid grid-cols-1 items-end gap-2 rounded-md border border-border p-2 sm:grid-cols-2">
                <SelectField
                  label={<>{prefix}VM</>}
                  value={draft.vmId}
                  onChange={(e) => set(index, { vmId: e.target.value })}
                  options={[{ value: '', label: 'Choose a VM' }, ...vms.map((vm) => ({ value: vm.source_id, label: vm.name }))]}
                />
                <SelectField
                  label={<>{prefix}kind</>}
                  value={draft.kind}
                  onChange={(e) => set(index, { kind: e.target.value as DnsTargetDraft['kind'] })}
                  options={[
                    { value: 'designate', label: 'Designate' },
                    { value: 'rfc2136', label: 'RFC 2136' },
                  ]}
                />
                <TextField label={<>{prefix}zone</>} value={draft.zone} onChange={(e) => set(index, { zone: e.target.value })} spellCheck={false} placeholder="example.com." />
                <TextField label={<>{prefix}record</>} value={draft.record} onChange={(e) => set(index, { record: e.target.value })} spellCheck={false} />
                {draft.kind === 'rfc2136' && (
                  <>
                    <TextField label={<>{prefix}server</>} value={draft.server} onChange={(e) => set(index, { server: e.target.value })} spellCheck={false} placeholder="host or host:port" />
                    <TextField label={<>{prefix}TSIG secret</>} value={draft.tsigSecret} onChange={(e) => set(index, { tsigSecret: e.target.value })} spellCheck={false} hint="The name of the secret holding the TSIG key." />
                  </>
                )}
                <div className="sm:col-span-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    icon={Trash2}
                    aria-label={`Remove DNS target ${n}`}
                    onClick={() => {
                      onChange(drafts.filter((_, j) => j !== index));
                      legendRef.current?.focus();
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
      <div>
        <Button type="button" size="sm" icon={Plus} onClick={() => onChange([...drafts, { ...NEW_DNS_TARGET }])}>
          Add DNS target
        </Button>
      </div>
    </fieldset>
  );
}

function PoolMappings({ drafts, onChange }: { drafts: PoolMappingDraft[]; onChange: (drafts: PoolMappingDraft[]) => void }) {
  const legendRef = useRef<HTMLLegendElement>(null);
  const set = (index: number, patch: Partial<PoolMappingDraft>) => onChange(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend ref={legendRef} tabIndex={-1} className="field-label outline-hidden">
        Octavia pool mappings
      </legend>
      <p className="-mt-1 text-xs text-muted-foreground">
        A source pool&apos;s members are drained before their VM stops and join the destination pool, created by an operator, after the verification.
      </p>
      {drafts.length > 0 && (
        <ol className="flex flex-col gap-2">
          {drafts.map((draft, i) => {
            const n = i + 1;
            const prefix = (
              <>
                <span className="sr-only">Pool mapping {n}</span>{' '}
              </>
            );
            return (
              <li key={i} className="grid grid-cols-1 items-end gap-2 rounded-md border border-border p-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                <TextField label={<>{prefix}source pool</>} value={draft.source} onChange={(e) => set(i, { source: e.target.value })} spellCheck={false} />
                <TextField label={<>{prefix}destination pool</>} value={draft.destination} onChange={(e) => set(i, { destination: e.target.value })} spellCheck={false} />
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label={`Remove pool mapping ${n}`}
                  onClick={() => {
                    onChange(drafts.filter((_, j) => j !== i));
                    legendRef.current?.focus();
                  }}
                />
              </li>
            );
          })}
        </ol>
      )}
      <div>
        <Button type="button" size="sm" icon={Plus} onClick={() => onChange([...drafts, { source: '', destination: '' }])}>
          Add pool mapping
        </Button>
      </div>
    </fieldset>
  );
}
