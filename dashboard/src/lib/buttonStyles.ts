import { cn } from './cn';

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';
export type ButtonSize = 'sm' | 'md' | 'lg';

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'border border-transparent bg-accent text-accent-foreground hover:bg-accent/90',
  secondary: 'border border-border bg-card text-foreground hover:bg-muted',
  danger: 'border border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/90',
  ghost: 'border border-transparent text-foreground hover:bg-muted',
};

const SIZES: Record<ButtonSize, string> = {
  sm: 'min-h-8 gap-1.5 px-2.5 text-sm',
  md: 'min-h-9 gap-2 px-3 text-sm',
  // 44×44 px minimum for primary actions (SDD §16).
  lg: 'min-h-11 min-w-11 gap-2 px-4 text-sm',
};

/** Shared button look for <button> and button-like links. */
export function buttonClassName(variant: ButtonVariant = 'secondary', size: ButtonSize = 'md', className?: string): string {
  return cn(
    'inline-flex shrink-0 cursor-pointer select-none items-center justify-center whitespace-nowrap rounded-md font-medium transition-colors duration-200',
    'disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50',
    VARIANTS[variant],
    SIZES[size],
    className,
  );
}
