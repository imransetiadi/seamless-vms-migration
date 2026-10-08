import { Bot, BrainCircuit, Info, ListChecks, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { AdvisorNote, AdvisorNoteSource } from '../api/types';
import { formatDateTime, formatPct, formatRelative } from '../lib/format';
import { ADVISOR_KIND_LABELS, ADVISOR_SOURCE_LABELS, STRATEGY_LABELS } from '../lib/status';
import { EmptyState } from './EmptyState';

const SOURCE_ICON: Record<AdvisorNoteSource, LucideIcon> = { jev: Bot, rules: ListChecks, memory: BrainCircuit };

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

function strategyName(value: unknown): string {
  const key = text(value);
  return STRATEGY_LABELS[key as keyof typeof STRATEGY_LABELS] ?? key;
}

/** Kind-specific evidence. Everything here is untrusted data and is rendered as plain text only. */
function NoteDetails({ note }: { note: AdvisorNote }) {
  const data = asRecord(note.data);
  switch (note.kind) {
    case 'similar_incidents': {
      const hits = Array.isArray(data.hits) ? data.hits.map(asRecord) : [];
      if (!hits.length) return null;
      return (
        <ul className="mt-1.5 flex flex-col gap-1.5">
          {hits.map((hit, i) => (
            <li key={i} className="rounded-md border border-border px-2.5 py-1.5 text-xs">
              <p className="font-medium text-foreground">
                {text(hit.title)}
                {typeof hit.score === 'number' && <span className="num ml-2 font-normal text-muted-foreground">match {formatPct(hit.score * 100, 0)}</span>}
              </p>
              <p className="wrap-break-word text-muted-foreground">{text(hit.content)}</p>
            </li>
          ))}
        </ul>
      );
    }
    case 'strategy': {
      const probabilities = Object.entries(asRecord(data.probabilities)).filter(([, v]) => typeof v === 'number') as Array<[string, number]>;
      return (
        <p className="mt-1 text-xs text-muted-foreground">
          {data.selected !== undefined && <>Selected: {strategyName(data.selected)}. </>}
          {probabilities.length > 0 && <>Probabilities: {probabilities.map(([k, v]) => `${strategyName(k)} ${formatPct(v * 100, 0)}`).join(' · ')}</>}
        </p>
      );
    }
    case 'classification':
      return data.tier ? <p className="mt-1 text-xs text-muted-foreground">Tier: <code>{text(data.tier)}</code>{data.decision ? ` (${text(data.decision)})` : ''}</p> : null;
    case 'verification':
      return data.verdict ? <p className="mt-1 text-xs text-muted-foreground">Verdict: {text(data.verdict)}{data.action ? ` · ${text(data.action)}` : ''}</p> : null;
    case 'screen':
      return (
        <p className="mt-1 text-xs text-muted-foreground">
          {data.action ? `Action: ${text(data.action)}` : ''}
          {typeof data.injection === 'number' ? ` · injection probability ${formatPct(data.injection * 100, 0)}` : ''}
        </p>
      );
  }
}

export interface AdvisorNoteRow extends AdvisorNote {
  vmName?: string;
  migrationId?: string;
}

/** Jev / rules / agentmemory notes with source, confidence and evidence. Advisory only (SDD §14.2). */
export function AdvisorNotes({ notes, emptyText = 'No advisor notes.' }: { notes: AdvisorNoteRow[]; emptyText?: string }) {
  if (notes.length === 0) return <EmptyState icon={Bot} title={emptyText} />;
  const sorted = [...notes].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  return (
    <div className="flex flex-col gap-3">
      <ul className="-my-2 divide-y divide-border">
        {sorted.map((note, index) => {
          const SourceIcon = SOURCE_ICON[note.source] ?? Bot;
          return (
            <li key={`${note.created_at}-${index}`} className="py-2.5">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <span className="font-semibold text-foreground">{ADVISOR_KIND_LABELS[note.kind] ?? note.kind}</span>
                <span className="inline-flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-muted-foreground">
                  <SourceIcon aria-hidden className="size-3.5" />
                  {ADVISOR_SOURCE_LABELS[note.source] ?? note.source}
                </span>
                {note.confidence !== null && <span className="num text-muted-foreground">confidence {formatPct(note.confidence * 100, 0)}</span>}
                {note.vmName &&
                  (note.migrationId ? (
                    <Link to={`/migrations/${note.migrationId}`} className="font-mono underline underline-offset-4">
                      {note.vmName}
                    </Link>
                  ) : (
                    <span className="font-mono">{note.vmName}</span>
                  ))}
                <time dateTime={note.created_at} title={formatDateTime(note.created_at)} className="text-muted-foreground">
                  {formatRelative(note.created_at)}
                </time>
              </div>
              <p className="mt-1 wrap-break-word text-sm text-foreground">{note.summary}</p>
              <NoteDetails note={note} />
            </li>
          );
        })}
      </ul>
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
        <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" />
        Advisory only: the advisor never makes an ineligible strategy eligible, skips approval, or triggers rollback or finalize.
      </p>
    </div>
  );
}
