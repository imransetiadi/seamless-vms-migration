import { Pencil } from 'lucide-react';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useInventory, useMigrations, usePatchPlan, useProviders } from '../api/hooks';
import type { Migration, Plan, Role } from '../api/types';
import { nicTarget, parseNicDrafts, toNicDraft, withVmNicOverrides, type NicDraft } from '../lib/nics';
import { planActions } from '../lib/planActions';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';
import { Modal } from './Modal';

/**
 * Network (SDD §16): each NIC of the VM — its source network, MAC and addresses — and where it goes in RHOSO, the plan's
 * mapping or the VM's override; Edit (an operator, OpenStack sources) sets the VM's `nic_overrides` entry, a plan edit.
 */
export function NetworkPanel({ migration: m, plan, role, onSaved }: { migration: Migration; plan: Plan | null | undefined; role: Role | undefined; onSaved: (text: string) => void }) {
  // the plan's edit rules (SDD §12), as for the probes: only a draft or validated plan is editable
  const editable = plan?.status === 'draft' || plan?.status === 'validated';
  const siblings = useMigrations({ plan_id: m.plan_id }, { enabled: editable });
  const providers = useProviders();
  const inventory = useInventory(plan?.destination_provider_id);
  const patch = usePatchPlan(plan?.id ?? '');
  const titleId = useId();
  const caption = useId();
  const problemsId = useId();
  const editRef = useRef<HTMLButtonElement>(null);
  const [drafts, setDrafts] = useState<NicDraft[] | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  if (!plan) return <p className="text-sm text-muted-foreground">Loading the plan…</p>;
  const vmware = providers.data?.find((p) => p.id === plan.source_provider_id)?.kind === 'vmware';
  const edit = planActions(plan, role, siblings.data ?? [m]).edit;
  const networks = inventory.data && !Array.isArray(inventory.data) ? Object.keys(inventory.data.networks).sort() : [];
  const nics = m.vm.nics;
  const hasEntry = Object.keys(plan.nic_overrides?.[m.vm.source_id] ?? {}).length > 0;

  const save = (entries: Parameters<typeof withVmNicOverrides>[2], said: string) =>
    patch.mutate(
      { nic_overrides: withVmNicOverrides(plan, m.vm.source_id, entries) },
      {
        onSuccess: () => {
          setDrafts(null);
          onSaved(`${said} The plan is a draft again: validate it before starting.`);
        },
      },
    );
  const open = () => {
    patch.reset();
    setProblems([]);
    setDrafts(nics.map((nic) => toNicDraft(plan, m.vm.source_id, nic)));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = parseNicDrafts(drafts ?? []);
    setProblems(parsed.problems);
    if (parsed.problems.length) return;
    save(parsed.entries, `${m.vm.name}'s network is saved.`);
  };
  const setDraft = (index: number, change: Partial<NicDraft>) => setDrafts((list) => (list ?? []).map((d, i) => (i === index ? { ...d, ...change } : d)));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 text-sm text-muted-foreground">
          {vmware
            ? "The migration kit keeps the guest's network settings: network overrides are for OpenStack sources."
            : hasEntry
              ? "This VM's overrides send some NICs elsewhere."
              : "Every NIC goes where the plan's mappings send its network, with its own addresses."}
        </p>
        {!vmware && (
          <Button ref={editRef} size="sm" icon={Pencil} disabledReason={edit.reason} onClick={open}>
            Edit network
          </Button>
        )}
      </div>
      {nics.length === 0 ? (
        <p className="text-sm text-muted-foreground">The VM has no NIC.</p>
      ) : (
        <div className="table-wrap rounded-md border border-border">
          <table className="data-table" aria-labelledby={caption}>
            <caption id={caption} className="sr-only">
              NICs of {m.vm.name}
            </caption>
            <thead>
              <tr>
                <th scope="col">NIC</th>
                <th scope="col">Source</th>
                <th scope="col">In RHOSO</th>
              </tr>
            </thead>
            <tbody>
              {nics.map((nic, i) => {
                const target = nicTarget(plan, m.vm.source_id, nic);
                return (
                  <tr key={nic.mac ?? i}>
                    <th scope="row" className="font-mono text-xs">
                      {nic.mac ?? '—'}
                    </th>
                    <td>
                      <span className="block">{nic.network}</span>
                      <span className="block font-mono text-xs text-muted-foreground">{nic.fixed_ips.join(', ') || 'no fixed IP'}</span>
                    </td>
                    <td>
                      <span className="block">
                        {vmware ? 'As the migration kit maps it' : target.network}
                        {target.overridden && !vmware && <span className="ml-2 rounded-sm bg-status-info/15 px-1.5 py-0.5 text-xs font-medium text-status-info">Override</span>}
                      </span>
                      {!vmware && <span className="block font-mono text-xs text-muted-foreground">{target.addresses.join(', ') || 'no fixed IP'}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <Modal open={drafts !== null} onClose={() => setDrafts(null)} labelledBy={titleId} dismissible={!patch.isPending} returnFocusRef={editRef}>
        <form onSubmit={submit} noValidate className="flex min-h-0 flex-col">
          <div className="border-b border-border px-5 py-4">
            <h2 id={titleId} className="text-lg font-semibold text-foreground">
              Network of {m.vm.name}
            </h2>
            <p className="text-sm text-muted-foreground">Saving edits the plan: it returns to draft and is validated again.</p>
          </div>
          <div className="flex min-h-0 flex-col gap-4 overflow-y-auto px-5 py-4">
            {patch.error && <ErrorBanner error={patch.error} title="The network was not saved" />}
            <div id={problemsId} role="status">
              {problems.length > 0 && (
                <ul className="field-error flex flex-col gap-1">
                  {problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
            </div>
            {(drafts ?? []).map((draft, i) => {
              const nic = nics[i]!;
              const mapped = plan.mappings.networks[nic.network] ?? nic.network;
              return (
                <fieldset key={draft.mac} className="flex flex-col gap-2 rounded-md border border-border p-3">
                  <legend className="px-1 text-sm font-medium text-foreground">
                    <span className="font-mono">{draft.mac}</span> on {nic.network} ({nic.fixed_ips.join(', ') || 'no fixed IP'})
                  </legend>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <label htmlFor={`${titleId}-net-${i}`} className="field-label">
                        RHOSO network <span className="sr-only">of {draft.mac}</span>
                      </label>
                      <select
                        id={`${titleId}-net-${i}`}
                        className="input"
                        value={draft.network}
                        onChange={(e) => setDraft(i, { network: e.target.value })}
                      >
                        <option value="">As the plan maps it ({mapped})</option>
                        {networks.map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label htmlFor={`${titleId}-ips-${i}`} className="field-label">
                        Addresses <span className="sr-only">of {draft.mac}</span>
                      </label>
                      <input
                        id={`${titleId}-ips-${i}`}
                        aria-describedby={`${titleId}-ips-${i}-hint ${problemsId}`}
                        className="input font-mono"
                        value={draft.addresses}
                        onChange={(e) => setDraft(i, { addresses: e.target.value })}
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <p id={`${titleId}-ips-${i}-hint`} className="mt-1 text-xs text-muted-foreground">
                        Comma separated. Empty keeps its own: {nic.fixed_ips.join(', ') || 'no fixed IP'}.
                      </p>
                    </div>
                  </div>
                </fieldset>
              );
            })}
            <p className="text-xs text-muted-foreground">Validation checks each address against the subnets of its RHOSO network, before anything stops.</p>
          </div>
          <div className="flex flex-col-reverse gap-2 border-t border-border px-5 py-3 sm:flex-row sm:justify-end">
            <Button type="button" size="lg" onClick={() => setDrafts(null)}>
              Cancel
            </Button>
            {hasEntry && (
              <Button type="button" size="lg" loading={patch.isPending} onClick={() => save({}, `${m.vm.name}'s NICs go where the plan maps them again.`)}>
                Use the plan&apos;s mapping
              </Button>
            )}
            <Button type="submit" variant="primary" size="lg" loading={patch.isPending}>
              Save network
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
