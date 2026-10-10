import { cn } from '../lib/cn';

export interface CheckboxProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
  /** Id of the text that says why the box is disabled. */
  describedBy?: string;
}

/** A labelled checkbox with a 44 px target and an optional hint (SDD §16). */
export function Checkbox({ checked, onChange, label, hint, disabled = false, describedBy }: CheckboxProps) {
  return (
    <label className={cn('flex min-h-11 items-start gap-2.5 py-1', disabled ? 'cursor-not-allowed' : 'cursor-pointer')}>
      <input
        type="checkbox"
        className="mt-1 size-4 shrink-0 cursor-pointer accent-accent disabled:cursor-not-allowed disabled:opacity-50"
        checked={checked}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className={cn('text-sm text-foreground', disabled && 'opacity-70')}>
        {label}
        {hint && <span className="block text-xs text-muted-foreground">{hint}</span>}
      </span>
    </label>
  );
}
