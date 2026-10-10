import { Power, PowerOff } from 'lucide-react';
import { useState } from 'react';
import { useDemoPower, useHealth } from '../api/hooks';
import type { Migration, Role } from '../api/types';
import { hasRole } from '../lib/roles';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';

/**
 * Demo mode only (SDD §8.1, §16): powers a completed migration's fake source VM on or off, so the source
 * watch shows its alert on its next check. Mount it per migration (`key`): what the last press set wins
 * over `source_running_at`, which the watch updates only on its next check.
 */
export function DemoSourcePower({ migration: m, role }: { migration: Migration; role: Role | undefined }) {
  const health = useHealth();
  const power = useDemoPower();
  const [set, setSet] = useState<boolean | null>(null);
  const [announcement, setAnnouncement] = useState('');
  if (!health.data?.demo || m.phase !== 'completed') return null;
  const running = set ?? Boolean(m.source_running_at);
  const press = () =>
    power.mutate(
      { sourceId: m.vm.source_id, on: !running },
      {
        onSuccess: (vm) => {
          const on = vm.power_state === 'running';
          setSet(on);
          setAnnouncement(on ? 'The source VM is running: the source watch flags it on its next check.' : 'The source VM is stopped.');
        },
      },
    );
  return (
    <div className="mt-3 flex flex-col gap-2 border-t border-dashed border-border pt-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Button
          size="lg"
          variant="ghost"
          icon={running ? PowerOff : Power}
          loading={power.isPending}
          disabledReason={hasRole(role, 'operator') ? null : 'Requires the operator role.'}
          onClick={press}
        >
          {running ? 'Power off the source VM (demo)' : 'Power on the source VM (demo)'}
        </Button>
        {/* a basis, not flex-1 min-w-0: on a phone the note wraps under the button instead of shrinking to a column of words */}
        <p className="min-w-0 grow basis-60 text-xs text-muted-foreground">
          Demo mode: a source VM running after the cutover writes data RHOSO no longer receives; the source watch alerts on it.
        </p>
      </div>
      {power.error && <ErrorBanner error={power.error} title="The demo power change failed" />}
      <p role="status" className="sr-only">
        {announcement}
      </p>
    </div>
  );
}
