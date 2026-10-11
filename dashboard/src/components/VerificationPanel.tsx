import { Activity, CircleCheck, CircleMinus, CircleX, Pencil } from 'lucide-react';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useMigrations, usePatchPlan } from '../api/hooks';
import type { Migration, Plan, Role, VerificationCheck } from '../api/types';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative } from '../lib/format';
import { planActions, type Availability } from '../lib/planActions';
import { describeProbes, parseDrafts, PROBE_RESULT_META, probesFor, toDraft, withVmProbes, type ProbeDraft } from '../lib/probes';
import { TONE_CLASSES } from '../lib/status';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { ErrorBanner } from './ErrorBanner';
import { Modal } from './Modal';
import { ProbeListEditor } from './ProbeListEditor';
import { StatusBadge } from './StatusBadge';

/** The VM's application probes, the plan's or its own, with Edit (an operator; a plan edit, SDD §12). */
function VmProbes({ m, plan, edit, onSaved }: { m: Migration; plan: Plan; edit: Availability; onSaved: (text: string) => void }) {
  const patch = usePatchPlan(plan.id);
  const titleId = useId();
  const [drafts, setDrafts] = useState<ProbeDraft[] | null>(null);
  const [tried, setTried] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const editRef = useRef<HTMLButtonElement>(null);
  // read by the suggestion's dialog as it closes: empty on Cancel, Edit probes once the suggestion was used and went
  // with its button (SDD §16)
  const afterSuggestionRef = useRef<HTMLElement | null>(null);
  const { probes, source } = probesFor(m.vm.source_id, plan);
  const parsed = drafts ? parseDrafts(drafts) : null;
  const save = (next: Parameters<typeof withVmProbes>[2], said: string, after?: () => void) =>
    patch.mutate(
      { probe_overrides: withVmProbes(plan, m.vm.source_id, next) },
      {
        onSuccess: () => {
          after?.();
          setDrafts(null);
          setSuggesting(false);
          onSaved(`${said} The plan is a draft again: validate it before starting.`);
        },
      },
    );
  const open = () => {
    patch.reset();
    setTried(false);
    setDrafts(probes.map(toDraft));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!parsed || parsed.problems.length) return;
    save(parsed.probes, `${m.vm.name}'s own probes are saved.`);
  };
  const label = source === 'override' ? "This VM's own probes" : "The plan's probes";
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="min-w-0 text-sm text-foreground">
          {probes.length ? `${label}: ${describeProbes(probes)}` : source === 'override' ? "No application probe: this VM's own list is empty." : 'No application probe: verification checks the boot only.'}
        </p>
        <Button ref={editRef} size="sm" icon={Pencil} disabledReason={edit.reason} onClick={open}>
          Edit probes
        </Button>
      </div>
      {probes.length === 0 && m.suggested_probes.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-dashed border-border px-3 py-2 text-sm">
          <span className="text-muted-foreground">Suggested: {describeProbes(m.suggested_probes)}</span>
          <Button
            size="sm"
            icon={Activity}
            disabledReason={edit.reason}
            onClick={() => {
              patch.reset();
              afterSuggestionRef.current = null;
              setSuggesting(true);
            }}
          >
            Use suggested probe
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={suggesting}
        title={`Probe ${m.vm.name} on ${describeProbes(m.suggested_probes)}?`}
        description="Saves it as this VM's own probe. The plan returns to draft: validate it again before it starts."
        confirmLabel="Use suggested probe"
        pending={patch.isPending}
        error={suggesting ? patch.error : null}
        returnFocusRef={afterSuggestionRef}
        onConfirm={() =>
          save(m.suggested_probes, `${m.vm.name} is probed on ${describeProbes(m.suggested_probes)}.`, () => {
            afterSuggestionRef.current = editRef.current;
          })
        }
        onCancel={() => setSuggesting(false)}
      />
      <Modal open={drafts !== null} onClose={() => setDrafts(null)} labelledBy={titleId} dismissible={!patch.isPending}>
        <form onSubmit={submit} noValidate className="flex flex-col">
          <div className="border-b border-border px-5 py-4">
            <h2 id={titleId} className="text-lg font-semibold text-foreground">
              Application probes of {m.vm.name}
            </h2>
            <p className="text-sm text-muted-foreground">Saving edits the plan: it returns to draft and is validated again.</p>
          </div>
          <div className="flex flex-col gap-4 px-5 py-4">
            {patch.error && <ErrorBanner error={patch.error} title="The probes were not saved" />}
            <ProbeListEditor
              legend={`Probes of ${m.vm.name}`}
              drafts={drafts ?? []}
              onChange={setDrafts}
              hint="They replace the plan's probes for this VM; an empty list probes nothing."
              error={tried ? (parsed?.problems[0] ?? null) : null}
              liveError
              emptyText="No application probe for this VM: verification checks the boot only."
            />
          </div>
          <div className="flex flex-col-reverse gap-2 border-t border-border px-5 py-3 sm:flex-row sm:justify-end">
            <Button type="button" size="lg" onClick={() => setDrafts(null)}>
              Cancel
            </Button>
            {source === 'override' && (
              <Button type="button" size="lg" loading={patch.isPending} onClick={() => save(null, `${m.vm.name} runs the plan's probes again.`)}>
                Use the plan&apos;s probes
              </Button>
            )}
            <Button type="submit" variant="primary" size="lg" loading={patch.isPending}>
              Save probes
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}

const CHECK_ICON = { ok: CircleCheck, failed: CircleX, skipped: CircleMinus };

function checkState(c: VerificationCheck): 'ok' | 'failed' | 'skipped' {
  return c.skipped ? 'skipped' : c.ok ? 'ok' : 'failed';
}

/**
 * Verification (SDD §7.5, §16, ADV-18): the VM's application probes and the last verification — each
 * probe's target, result, latency and time, and the checks.
 */
export function VerificationPanel({ migration: m, plan, role, onSaved }: { migration: Migration; plan: Plan | null | undefined; role: Role | undefined; onSaved: (text: string) => void }) {
  // the plan's edit rules (SDD §12): a migration in flight keeps the plan as it is
  const siblings = useMigrations({ plan_id: m.plan_id });
  const edit = plan ? planActions(plan, role, siblings.data ?? [m]).edit : { enabled: false, reason: 'The plan is loading.' };
  const record = m.verification;
  const caption = useId();
  const checksId = useId();
  return (
    <div className="flex flex-col gap-4">
      {plan && <VmProbes m={m} plan={plan} edit={edit} onSaved={onSaved} />}
      {record === null ? (
        <p className="text-sm text-muted-foreground">Not verified yet: verification runs after the cutover.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge tone={record.passed ? 'success' : 'danger'} icon={record.passed ? CircleCheck : CircleX} label={record.passed ? 'Passed' : 'Failed'} />
            <time dateTime={record.at} title={formatDateTime(record.at)} className="text-muted-foreground">
              Last verified {formatRelative(record.at)}
            </time>
          </p>
          {record.probes.length > 0 && (
            <div className="table-wrap rounded-md border border-border">
              <table className="data-table" aria-labelledby={caption}>
                <caption id={caption} className="sr-only">
                  Application probes of the last verification
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Probe</th>
                    <th scope="col">Result</th>
                    <th scope="col" className="text-right">
                      Latency
                    </th>
                    <th scope="col">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {record.probes.map((p, i) => {
                    const meta = PROBE_RESULT_META[p.result];
                    return (
                      <tr key={`${p.target}-${i}`}>
                        <th scope="row" className="min-w-0 font-mono text-xs" title={p.detail}>
                          <span className="wrap-anywhere">{p.target}</span>
                        </th>
                        <td>
                          <span className={cn('inline-flex items-center gap-1', TONE_CLASSES[meta.tone].text)} title={p.detail}>
                            <meta.icon aria-hidden className="size-3.5 shrink-0" />
                            {meta.label}
                          </span>
                        </td>
                        <td className="num text-right">{p.latency_ms === null ? '—' : `${p.latency_ms} ms`}</td>
                        <td>
                          <time dateTime={p.at} title={formatDateTime(p.at)}>
                            {formatRelative(p.at)}
                          </time>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div>
            <h3 id={checksId} className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Checks
            </h3>
            <ul aria-label="Checks of the last verification" className="flex flex-col gap-1 text-sm">
              {record.checks.map((c) => {
                const state = checkState(c);
                const Icon = CHECK_ICON[state];
                return (
                  <li key={c.name} className="flex items-start gap-2">
                    <Icon
                      aria-hidden
                      className={cn('mt-0.5 size-3.5 shrink-0', state === 'ok' ? TONE_CLASSES.success.text : state === 'failed' ? TONE_CLASSES.danger.text : 'text-muted-foreground')}
                    />
                    <span className="min-w-0">
                      <code className="text-xs">{c.name}</code>
                      <span className="sr-only">: {state === 'ok' ? 'passed' : state}</span>
                      <span className="block wrap-anywhere text-xs text-muted-foreground">{c.detail}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
