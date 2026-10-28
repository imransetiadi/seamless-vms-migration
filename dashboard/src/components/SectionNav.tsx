export function SectionNav({ label, sections }: { label: string; sections: ReadonlyArray<readonly [string, string]> }) {
  return (
    <nav aria-label={label} className="section-nav sticky top-14 z-10 mb-4 overflow-x-auto rounded-md border border-border bg-card px-1 py-1 shadow-sm md:top-2">
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
