import { Moon, Sun } from 'lucide-react';
import { cn } from '../lib/cn';
import { useTheme } from '../theme/context';

/** Switches between the dark (default) and light themes; the choice is remembered per browser. */
export function ThemeToggle({ showLabel = false, className }: { showLabel?: boolean; className?: string }) {
  const { theme, toggleTheme } = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  const Icon = theme === 'dark' ? Sun : Moon;
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={showLabel ? undefined : `Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      className={cn(
        'inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center gap-2 rounded-md px-2 text-sm text-foreground transition-colors duration-200 hover:bg-muted',
        className,
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      {showLabel && <span>{next === 'light' ? 'Light theme' : 'Dark theme'}</span>}
    </button>
  );
}
