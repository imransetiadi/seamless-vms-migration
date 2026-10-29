import { Moon, Sun } from 'lucide-react';
import { cn } from '../lib/cn';
import { useTheme } from '../theme/context';

/** Switches between the dark (default) and light themes; the choice is remembered per browser. */
export function ThemeToggle({
  showLabel = false,
  stacked = false,
  className,
}: {
  showLabel?: boolean;
  /** The navigation rail's form: the icon above its visible label (SDD §16). */
  stacked?: boolean;
  className?: string;
}) {
  const labelled = showLabel || stacked;
  const { theme, toggleTheme } = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  const Icon = theme === 'dark' ? Sun : Moon;
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={labelled ? undefined : `Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      className={cn(
        'inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-foreground transition-colors duration-200 hover:bg-muted',
        stacked ? 'flex-col gap-1 px-1 py-1.5 text-center text-xs leading-tight' : 'gap-2 px-2 text-sm',
        className,
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      {labelled && <span>{next === 'light' ? 'Light theme' : 'Dark theme'}</span>}
    </button>
  );
}
