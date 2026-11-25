import { useScrollOffset } from '../lib/useScrollOffset';

export function SectionNav({ label, sections }: { label: string; sections: ReadonlyArray<readonly [string, string]> }) {
  const navRef = useScrollOffset<HTMLElement>('--section-nav-height');
  return (
    <nav ref={navRef} aria-label={label} className="section-nav z-10 mb-4 overflow-x-auto scroll-px-2 rounded-md border border-border bg-card px-1 py-1 shadow-sm">
      <ul className="flex min-w-max gap-1">
        {sections.map(([id, title]) => (
          <li key={id}>
            <a href={`#${id}`} className="inline-flex min-h-11 items-center rounded-md px-3 text-sm text-foreground transition-colors duration-200 hover:bg-muted">
              {title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
