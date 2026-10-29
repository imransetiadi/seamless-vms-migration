import { ChevronRight } from 'lucide-react';
import type { ReactNode, Ref } from 'react';
import { Link } from 'react-router-dom';

export interface Crumb {
  label: string;
  to?: string;
}

export interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: Crumb[];
  meta?: ReactNode;
  /** Makes the title a focus target, for an action that takes the focused control with it (SDD §16). */
  titleRef?: Ref<HTMLHeadingElement>;
}

export function PageHeader({ title, description, actions, breadcrumbs, meta, titleRef }: PageHeaderProps) {
  return (
    <header className="mb-4 flex min-w-0 flex-col gap-3 wrap-anywhere md:mb-6">
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav aria-label="Breadcrumb">
          <ol className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
            {breadcrumbs.map((crumb, index) => (
              <li key={`${crumb.label}-${index}`} className="flex min-w-0 items-center gap-1">
                {index > 0 && <ChevronRight aria-hidden className="size-3.5 shrink-0" />}
                {crumb.to ? (
                  <Link to={crumb.to} className="min-w-0 wrap-anywhere rounded-sm underline-offset-4 hover:text-foreground hover:underline">
                    {crumb.label}
                  </Link>
                ) : (
                  <span aria-current="page" className="min-w-0 wrap-anywhere text-foreground">
                    {crumb.label}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h1
            ref={titleRef}
            tabIndex={titleRef ? -1 : undefined}
            className="wrap-anywhere text-xl font-semibold text-foreground outline-hidden md:text-2xl"
          >
            {title}
          </h1>
          {description && <div className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</div>}
          {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}
