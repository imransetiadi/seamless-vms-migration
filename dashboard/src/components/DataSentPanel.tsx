import { CircleSlash, Send } from 'lucide-react';
import { useDataSent } from '../api/hooks';
import { DECISION_TYPE_LABELS } from '../lib/advisor';
import { ErrorBanner } from './ErrorBanner';
import { Panel } from './Panel';
import { LoadingBlock } from './Skeleton';
import { StatusBadge } from './StatusBadge';

/** What each decision type sends to Jev, as the serializers declare it (GET /advisor/data-sent, SDD §13.4). */
export function DataSentPanel() {
  const sent = useDataSent();
  return (
    <Panel
      title="Data sent to advisor"
      description="What each decision type sends to Jev, generated from the serializers. Every field passes the redaction layer first: passwords, tokens, keys and clouds.yaml credentials never leave."
    >
      {sent.isPending ? (
        <LoadingBlock rows={3} />
      ) : sent.error ? (
        <ErrorBanner error={sent.error} title="The data sent to the advisor could not be loaded" onRetry={() => void sent.refetch()} />
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {sent.data.items.map((item) => {
            const label = DECISION_TYPE_LABELS[item.decision_type] ?? item.decision_type;
            return (
              <li key={item.decision_type} aria-label={label} className="flex min-w-0 flex-col gap-2 rounded-md border border-border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-semibold text-foreground">{label}</p>
                  {item.sent ? (
                    <StatusBadge tone="info" icon={Send} label="Sent" />
                  ) : (
                    <StatusBadge tone="neutral" icon={CircleSlash} label="Not sent" />
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  Tool <code>{item.tool}</code>
                </p>
                <ul className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-foreground">
                  {item.fields.map((field) => (
                    <li key={field.key} className="wrap-break-word">
                      {field.label}
                    </li>
                  ))}
                </ul>
                {/* a setting name is one long word: it wraps anywhere on a narrow screen (NFR-08) */}
                {item.note && <p className="wrap-anywhere text-xs text-status-warning">{item.note}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
