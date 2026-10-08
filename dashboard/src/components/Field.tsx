import { CircleAlert } from 'lucide-react';
import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react';
import { cn } from '../lib/cn';

interface FieldChrome {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  className?: string;
}

function Chrome({
  id,
  label,
  hint,
  error,
  required,
  className,
  children,
}: FieldChrome & { id: string; children: ReactNode }) {
  return (
    <div className={cn('min-w-0', className)}>
      <label htmlFor={id} className="field-label">
        {label}
        {required && (
          <span aria-hidden className="ml-0.5 text-status-danger">
            *
          </span>
        )}
      </label>
      {children}
      {hint && !error && (
        <p id={`${id}-hint`} className="field-hint">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="field-error">
          <CircleAlert aria-hidden className="size-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}

function describedBy(id: string, hint: unknown, error: unknown): string | undefined {
  if (error) return `${id}-error`;
  if (hint) return `${id}-hint`;
  return undefined;
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export function SelectField({
  label,
  hint,
  error,
  required,
  className,
  options,
  id: idProp,
  ...rest
}: FieldChrome & Omit<SelectHTMLAttributes<HTMLSelectElement>, 'className'> & { options: SelectOption[] }) {
  const generated = useId();
  const id = idProp ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required} className={className}>
      <select
        id={id}
        className="input cursor-pointer pr-8"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        aria-required={required || undefined}
        {...rest}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
    </Chrome>
  );
}

type TextFieldProps = FieldChrome & Omit<InputHTMLAttributes<HTMLInputElement>, 'className'> & { inputClassName?: string };

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { label, hint, error, required, className, inputClassName, id: idProp, ...rest },
  ref,
) {
  const generated = useId();
  const id = idProp ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required} className={className}>
      <input
        ref={ref}
        id={id}
        className={cn('input', inputClassName)}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        aria-required={required || undefined}
        {...rest}
      />
    </Chrome>
  );
});

export function TextAreaField({
  label,
  hint,
  error,
  required,
  className,
  id: idProp,
  ...rest
}: FieldChrome & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'className'>) {
  const generated = useId();
  const id = idProp ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required} className={className}>
      <textarea
        id={id}
        className="input min-h-20"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        aria-required={required || undefined}
        {...rest}
      />
    </Chrome>
  );
}
