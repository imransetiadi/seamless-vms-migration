import { CircleAlert, Plus, Trash2 } from 'lucide-react';
import { useId, useRef } from 'react';
import { MAX_PROBES_PER_VM, PROBE_KINDS, type ProbeKind } from '../api/types';
import { NEW_PROBE, type ProbeDraft } from '../lib/probes';
import { Button } from './Button';
import { SelectField, TextField } from './Field';

const KIND_LABELS: Record<ProbeKind, string> = { tcp: 'TCP', http: 'HTTP', https: 'HTTPS' };

export interface ProbeListEditorProps {
  /** The fieldset's id: an error summary moves focus here. */
  id?: string;
  legend: string;
  drafts: ProbeDraft[];
  onChange: (drafts: ProbeDraft[]) => void;
  hint?: string;
  /** What the API would refuse in the list, once the form was submitted. */
  error?: string | null;
  /**
   * For a list checked as it is edited (after a refused save): its error is announced when it appears, from a live
   * region already on the page (SDD §16). A form that focuses an error summary announces it there instead.
   */
  liveError?: boolean;
  emptyText?: string;
}

/**
 * Application probes (SDD §7.5, ADV-18): a TCP connect, or an HTTP(S) GET of a path with an optional
 * expected status. Each field names its probe ("Probe 2 port") for assistive technology.
 */
export function ProbeListEditor({
  id,
  legend,
  drafts,
  onChange,
  hint,
  error,
  liveError,
  emptyText = 'No application probe: verification checks the boot only.',
}: ProbeListEditorProps) {
  const hintId = useId();
  const errorId = useId();
  const legendRef = useRef<HTMLLegendElement>(null);
  const set = (index: number, patch: Partial<ProbeDraft>) => onChange(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  const full = drafts.length >= MAX_PROBES_PER_VM;
  const message = error && (
    <p id={errorId} className="field-error">
      <CircleAlert aria-hidden className="size-3.5 shrink-0" />
      {error}
    </p>
  );
  return (
    <fieldset id={id} tabIndex={id ? -1 : undefined} className="flex min-w-0 flex-col gap-2" aria-describedby={[hint ? hintId : '', error ? errorId : ''].filter(Boolean).join(' ') || undefined}>
      <legend ref={legendRef} tabIndex={-1} className="field-label outline-hidden">
        {legend}
      </legend>
      {hint && (
        <p id={hintId} className="-mt-1 text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {drafts.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground">{emptyText}</p>
      ) : (
        // two rows where the editor is narrow (a dialog), one where it is wide (the plan form)
        <ol className="@container flex flex-col gap-2">
          {drafts.map((draft, i) => {
            const n = i + 1;
            const http = draft.kind !== 'tcp';
            const prefix = <span className="sr-only">Probe {n} </span>;
            return (
              <li key={i} className="grid grid-cols-6 items-end gap-2 rounded-md border border-border p-2 @2xl:grid-cols-[7rem_6rem_minmax(0,1fr)_8rem_auto]">
                <SelectField
                  className="col-span-3 @2xl:col-span-1"
                  label={<>{prefix}Kind</>}
                  value={draft.kind}
                  onChange={(e) => set(i, { kind: e.target.value as ProbeKind })}
                  options={PROBE_KINDS.map((kind) => ({ value: kind, label: KIND_LABELS[kind] }))}
                />
                <TextField className="col-span-2 @2xl:col-span-1" label={<>{prefix}Port</>} type="number" inputMode="numeric" min={1} max={65535} value={draft.port} onChange={(e) => set(i, { port: e.target.value })} />
                {http ? (
                  <>
                    <TextField className="col-span-4 @2xl:col-span-1" label={<>{prefix}Path</>} value={draft.path} onChange={(e) => set(i, { path: e.target.value })} spellCheck={false} />
                    <TextField
                      className="col-span-2 @2xl:col-span-1"
                      label={<>{prefix}Expected status</>}
                      type="number"
                      inputMode="numeric"
                      min={100}
                      max={599}
                      placeholder="2xx or 3xx"
                      value={draft.expectStatus}
                      onChange={(e) => set(i, { expectStatus: e.target.value })}
                    />
                  </>
                ) : (
                  <p className="col-span-6 self-center text-xs text-muted-foreground @2xl:col-span-2">Passes when the port accepts a connection.</p>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label={`Remove probe ${n}`}
                  className="col-start-6 row-start-1 justify-self-end @2xl:col-start-auto @2xl:row-start-auto"
                  onClick={() => {
                    onChange(drafts.filter((_, j) => j !== i));
                    // the row and its Remove button go: focus moves to the list's legend, never to the page (SDD §16)
                    legendRef.current?.focus();
                  }}
                />
              </li>
            );
          })}
        </ol>
      )}
      {/* empty, it takes no room: -mt-2 cancels the fieldset's gap */}
      {liveError ? (
        <div role="status" className="empty:-mt-2">
          {message}
        </div>
      ) : (
        message
      )}
      <div>
        <Button
          type="button"
          size="sm"
          icon={Plus}
          disabledReason={full ? `At most ${MAX_PROBES_PER_VM} probes for one VM.` : null}
          onClick={() => onChange([...drafts, { ...NEW_PROBE }])}
        >
          Add probe
        </Button>
      </div>
    </fieldset>
  );
}
