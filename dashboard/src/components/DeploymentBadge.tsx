import { Cloud, Server } from 'lucide-react';
import { deploymentLabel } from '../lib/advisor';

/** Where the Jev model runs: "On-prem", "Remote: {host}" or "Remote" (SDD §13.4, §16); nothing when unknown. */
export function DeploymentBadge({ deployment, host }: { deployment: 'on_prem' | 'remote' | 'unknown'; host: string | null }) {
  const label = deploymentLabel(deployment, host);
  if (!label) return null;
  const Icon = deployment === 'on_prem' ? Server : Cloud;
  return (
    <span
      title={deployment === 'on_prem' ? 'The model runs inside this installation' : 'The model runs outside this installation'}
      className="inline-flex max-w-full items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-xs text-muted-foreground"
    >
      <Icon aria-hidden className="size-3.5 shrink-0" />
      <span className="min-w-0 wrap-break-word">{label}</span>
    </span>
  );
}
