import { Info, Pencil, Trash2, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useInventory, useMigrationSummaries, usePatchPlan, useProviders } from '../api/hooks';
import type { Migration, Plan, Role } from '../api/types';
import { capabilityRoles, policyProfileOf, portRefusal, resolveNetwork } from '../lib/networks';
import { nicTarget, parseNicDrafts, toNicDraft, withVmNicOverrides, type NicDraft } from '../lib/nics';
import { planActions } from '../lib/planActions';
import { Button } from './Button';
import { ScrollRegion } from './ScrollRegion';
import { ErrorBanner } from './ErrorBanner';
import { Modal } from './Modal';

/**
 * Network (SDD §16): each NIC of the VM — its source network, MAC and addresses — and where it goes in RHOSO, the plan's
 * mapping or the VM's override; Edit (an operator, OpenStack sources) sets the VM's `nic_overrides` entry, a plan edit. A
 * VMware VM takes none: one that still has entries offers to remove them.
 */
export function NetworkPanel({ migration: m, plan, role, onSaved }: { migration: Migration; plan: Plan | null | undefined; role: Role | undefined; onSaved: (text: string) => void }) {
  // the plan's edit rules (SDD §12), as for the probes: only a draft or validated plan is editable
  const editable = plan?.status === 'draft' || plan?.status === 'validated';
  // whether the plan can be edited: its migrations as summaries (SDD §16, perf audit PF-02)
  const siblings = useMigrationSummaries({ plan_id: m.plan_id }, { enabled: editable });
  const providers = useProviders();
  const inventory = useInventory(plan?.destination_provider_id);
  const patch = usePatchPlan(plan?.id ?? '');
  const titleId = useId();
  const caption = useId();
  const editRef = useRef<HTMLButtonElement>(null);
  const addressRefs = useRef<Array<HTMLInputElement | null>>([]);
  const [drafts, setDrafts] = useState<NicDraft[] | null>(null);
  // what the API would refuse, by MAC: each address field carries its own (SDD §16)
  const [problems, setProblems] = useState<Record<string, string[]>>({});
  const [focusField, setFocusField] = useState<number | null>(null);
  // the first refused field gets the focus once it is marked invalid and described by its problem
  useEffect(() => {
    if (focusField === null) return;
    addressRefs.current[focusField]?.focus();
    setFocusField(null);
  }, [focusField]);
  if (!plan) return <p className="text-sm text-muted-foreground">Loading the plan…</p>;
  const vmware = providers.data?.find((p) => p.id === plan.source_provider_id)?.kind === 'vmware';
  const edit = planActions(plan, role, siblings.data ?? [m]).edit;
  const listed = Boolean(inventory.data && !Array.isArray(inventory.data));
  const networks = inventory.data && !Array.isArray(inventory.data) ? Object.keys(inventory.data.networks).sort() : [];
  const details = inventory.data && !Array.isArray(inventory.data) ? (inventory.data.network_details ?? []) : [];
  // the destination token's roles, as its check reported them, and the roles its policy profile counts: whether it may put
  // a port on another project's network (SDD §6.6, §21.5)
  const destination = providers.data?.find((p) => p.id === plan.destination_provider_id);
  const roles = capabilityRoles(destination?.capabilities);
  const profile = policyProfileOf(destination?.capabilities, destination);
  // a name only another project's network carries, one the credential may not use, or one the cutover cannot resolve (SDD §16, §7.3 step 0)
  const networkLabel = (name: string) => {
    if (!details.length) return name;
    const { network, why } = resolveNetwork(details, name);
    if (why) return `${name} (several networks)`;
    if (network && portRefusal(network, roles, profile)) return `${name} (not allowed)`;
    return network && !network.own ? `${name} (another project's)` : name;
  };
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
    setProblems({});
    setDrafts(nics.map((nic) => toNicDraft(plan, m.vm.source_id, nic)));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = parseNicDrafts(drafts ?? []);
    setProblems(parsed.problems);
    const refused = (drafts ?? []).findIndex((d) => parsed.problems[d.mac]);
    if (refused >= 0) {
      setFocusField(refused);
      return;
    }
    save(parsed.entries, `${m.vm.name}'s network is saved.`);
  };
  const setDraft = (index: number, change: Partial<NicDraft>) => setDrafts((list) => (list ?? []).map((d, i) => (i === index ? { ...d, ...change } : d)));
  // an edited address field is no longer the one refused
  const setAddresses = (index: number, mac: string, addresses: string) => {
    setDraft(index, { addresses });
    setProblems(({ [mac]: _refused, ...others }) => others);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 text-sm text-muted-foreground">
          {vmware
            ? hasEntry
              ? "The migration kit keeps the guest's network settings: this VM's network overrides cannot be applied and block it."
              : "The migration kit keeps the guest's network settings: network overrides are for OpenStack sources."
            : hasEntry
              ? "This VM's overrides send some NICs elsewhere."
              : "Every NIC goes where the plan's mappings send its network, with its own addresses."}
        </p>
        {!vmware && (
          <Button ref={editRef} size="sm" icon={Pencil} disabledReason={edit.reason} onClick={open}>
            Edit network
          </Button>
        )}
        {vmware && hasEntry && (
          <Button size="sm" icon={Trash2} disabledReason={edit.reason} loading={patch.isPending} onClick={() => save({}, `${m.vm.name}'s network overrides are removed.`)}>
            Remove the overrides
          </Button>
        )}
      </div>
      {vmware && patch.error && <ErrorBanner error={patch.error} title="The overrides were not removed" />}
      {nics.length === 0 ? (
        <p className="text-sm text-muted-foreground">The VM has no NIC.</p>
      ) : (
        <ScrollRegion label={`NICs of ${m.vm.name}`} className="rounded-md border border-border">
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
                // a VMware NIC goes where the plan maps its port group, with the guest's addresses: the migration kit keeps
                // the guest's network settings, so no override moves it (SDD §7.2)
                const target = vmware
                  ? { network: plan.mappings.networks[nic.network] ?? nic.network, addresses: nic.fixed_ips, overridden: false }
                  : nicTarget(plan, m.vm.source_id, nic);
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
                        {target.network}
                        {target.overridden && <span className="ml-2 rounded-sm bg-status-info/15 px-1.5 py-0.5 text-xs font-medium text-status-info">Override</span>}
                      </span>
                      <span className="block font-mono text-xs text-muted-foreground">{target.addresses.join(', ') || 'no fixed IP'}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollRegion>
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
            {(drafts ?? []).map((draft, i) => {
              const nic = nics[i]!;
              const mapped = plan.mappings.networks[nic.network] ?? nic.network;
              const refused = problems[draft.mac] ?? [];
              const hint = `${titleId}-ips-${i}-hint`;
              const error = `${titleId}-ips-${i}-error`;
              // an entry's network RHOSO no longer lists stays selected, marked so (SDD §16)
              const unlisted = Boolean(draft.network) && !networks.includes(draft.network);
              const note = `${titleId}-net-${i}-note`;
              // said under the field too: a narrow select cuts its option's text
              const chosen = draft.network && details.length ? resolveNetwork(details, draft.network) : null;
              const notAllowed = chosen?.network ? portRefusal(chosen.network, roles, profile) : null;
              const said =
                unlisted && listed
                  ? { warn: true, text: `RHOSO does not list ${draft.network}: validation refuses it.` }
                  : chosen?.why
                    ? { warn: true, text: `Several RHOSO networks are named ${draft.network}: validation refuses it.` }
                    : chosen?.network && notAllowed
                      ? {
                          warn: true,
                          text: `The RHOSO credential may not put a port on ${draft.network} (project ${chosen.network.project_id}): ${notAllowed}. Validation refuses it.`,
                        }
                      : chosen?.network && !chosen.network.own
                        ? { warn: false, text: `${draft.network} is another project's network: the cutover finds it by its name.` }
                        : null;
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
                        aria-describedby={said ? note : undefined}
                        className="input"
                        value={draft.network}
                        onChange={(e) => setDraft(i, { network: e.target.value })}
                      >
                        <option value="">As the plan maps it ({mapped})</option>
                        {unlisted && <option value={draft.network}>{listed ? `${draft.network} (not in RHOSO)` : draft.network}</option>}
                        {networks.map((name) => (
                          <option key={name} value={name}>
                            {networkLabel(name)}
                          </option>
                        ))}
                      </select>
                      {said && (
                        <p id={note} className="mt-1 flex items-start gap-1 text-xs text-muted-foreground">
                          {said.warn ? (
                            <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-status-warning" />
                          ) : (
                            <Info aria-hidden className="mt-0.5 size-3.5 shrink-0 text-status-info" />
                          )}
                          {said.text}
                        </p>
                      )}
                    </div>
                    <div>
                      <label htmlFor={`${titleId}-ips-${i}`} className="field-label">
                        Addresses <span className="sr-only">of {draft.mac}</span>
                      </label>
                      <input
                        id={`${titleId}-ips-${i}`}
                        ref={(el) => {
                          addressRefs.current[i] = el;
                        }}
                        aria-invalid={refused.length ? true : undefined}
                        aria-describedby={refused.length ? `${error} ${hint}` : hint}
                        className="input font-mono"
                        value={draft.addresses}
                        onChange={(e) => setAddresses(i, draft.mac, e.target.value)}
                        autoComplete="off"
                        spellCheck={false}
                      />
                      {refused.length > 0 && (
                        <p id={error} className="field-error mt-1">
                          {refused.join('; ')}
                        </p>
                      )}
                      <p id={hint} className="mt-1 text-xs text-muted-foreground">
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
