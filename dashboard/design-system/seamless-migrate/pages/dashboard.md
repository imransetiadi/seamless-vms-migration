# Dashboard Page Overrides

SDD §16 wins. Shared tokens and typography follow `../MASTER.md`.

- Max content width 1400px; page gutters 16px mobile and 24px desktop.
- Overview starts with a compact scope header and eight key figures. All figures and hints remain visible.
- Downtime now is the main operational card. Clocks, SLO, source state and warning labels remain readable.
- From 1024px, clocks/history and human action queues use a 3:2 split. Below it, read clocks, approvals,
  attention, then history. First clock and SLO remain inside the first 844px at 390px width.
- Cards use 12px radius and quiet shadows; headers have a restrained tint. Static cards do not pretend to be links.
- Use existing task CTAs and native links. The Plans summary offers View all plans; primary creation actions
  stay on their owning page with the existing role rules. Buttons/segments use at least 44px on small screens.
- Provider source/destination columns keep equal widths, 16px card separation and a separate action footer.
- Plans places status below its name before 1024px; desktop keeps a separate status column with at least 8rem.
- Soft light surfaces and current Fira typography stay intact. Test both themes, root text at 200%, text-spacing
  overrides, keyboard access, long content and 320px reflow after spacing changes.
- No marketing hero, trial CTA, invented status, external font or new motion dependency.
