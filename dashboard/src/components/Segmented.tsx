import { useId } from 'react';
import { cn } from '../lib/cn';

/** One choice of a few, as radio buttons drawn as a segmented control (keyboard: arrows within, Tab out). */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex flex-wrap gap-1 rounded-md border border-border p-1">
      {options.map((option) => (
        <label
          key={option.value}
          className={cn(
            'inline-flex min-h-9 cursor-pointer items-center rounded px-3 text-sm transition-colors duration-150',
            option.value === value ? 'bg-accent text-accent-foreground' : 'text-foreground hover:bg-muted',
            'has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-ring',
          )}
        >
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === value}
            onChange={() => onChange(option.value)}
            className="sr-only"
          />
          {option.label}
        </label>
      ))}
    </div>
  );
}
