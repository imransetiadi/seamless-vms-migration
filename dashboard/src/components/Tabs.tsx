import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from '../lib/cn';

export interface TabItem {
  id: string;
  label: string;
  content: ReactNode;
}

/** Tabs with the WAI-ARIA keyboard model: arrows move and select, Home and End jump. */
export function Tabs({ tabs, label, className }: { tabs: TabItem[]; label: string; className?: string }) {
  const baseId = useId();
  const [active, setActive] = useState(tabs[0]?.id);
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = tabs.length - 1;
    const next =
      event.key === 'ArrowRight' ? (index === last ? 0 : index + 1) : event.key === 'ArrowLeft' ? (index === 0 ? last : index - 1) : event.key === 'Home' ? 0 : event.key === 'End' ? last : null;
    if (next === null) return;
    event.preventDefault();
    setActive(tabs[next]?.id);
    refs.current[next]?.focus();
  };

  return (
    <div className={className}>
      <div role="tablist" aria-label={label} className="flex gap-1 border-b border-border">
        {tabs.map((tab, index) => {
          const selected = tab.id === active;
          return (
            <button
              key={tab.id}
              ref={(el) => {
                refs.current[index] = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`${baseId}-panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(tab.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
              className={cn(
                '-mb-px min-h-10 cursor-pointer border-b-2 px-3 text-sm font-medium transition-colors duration-200',
                selected ? 'border-accent text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div key={tab.id} role="tabpanel" id={`${baseId}-panel-${tab.id}`} aria-labelledby={`${baseId}-tab-${tab.id}`} hidden={tab.id !== active} tabIndex={0} className="pt-4">
          {tab.id === active && tab.content}
        </div>
      ))}
    </div>
  );
}
