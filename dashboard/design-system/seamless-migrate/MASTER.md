# Seamless Migrate Dashboard Design System

SDD section 16 is binding. This guide describes the operator dashboard, not a marketing site.
Use existing components and `src/index.css`; do not copy isolated CSS examples over their behavior.
Page-specific guidance must remain consistent with the SDD and these shared components.

## Purpose and hierarchy

Operators monitor migrations, investigate failures, manage providers, and approve consequential actions.
Keep VM identity, migration phase, downtime, next action, and evidence easy to find. Use `PageHeader`
for titles, breadcrumbs and primary actions; `Panel` for related information; `SectionNav` for long
pages. Keep the current task visible on mobile. Do not add sales CTAs, unsupported trust claims,
false urgency, or decorative dashboards.

## Color and typography

Use semantic Tailwind tokens from `src/index.css` for both themes. Dark is the initial theme;
`ThemeProvider` remembers an explicit choice. Status always has a label or icon as well as color.

| Token | Dark | Light |
| --- | --- | --- |
| Background | `#0F172A` | `#E8EDF3` |
| Foreground | `#F8FAFC` | `#1E293B` |
| Card | `#1B2336` | `#F1F4F8` |
| Muted | `#272F42` | `#DCE3ED` |
| Muted foreground | `#94A3B8` | `#475569` |
| Border | `#475569` | `#C5CFDC` |
| Input boundary | `#748499` | `#64748B` |
| Accent | `#22C55E` | `#EA580C` |
| Accent foreground | `#0F172A` | `#0F172A` |
| Focus ring | `#FFFFFF` | `#2563EB` |

Status, chart and destructive tokens live beside these definitions. `src/theme/contrast.test.ts`
checks text and control contrast; avoid literal colors in components. Use the softer light surfaces
for cards, forms and navigation, including modals.

Fira Sans is the body font; Fira Code is for headings, identifiers and tabular numbers. Fonts are
bundled locally through `@fontsource` imports in `src/main.tsx`; no external font requests.
Typical body text is `text-sm`; input text is 16px on mobile to avoid automatic input zoom.
Use `num` for tabular numbers, with units and an explicit unknown state.

## Layout and controls

Use existing Tailwind spacing: `gap-2` for controls, `gap-3` or `gap-4` for related sections,
`p-4` for cards, and responsive grids with `minmax(0, 1fr)`. Flex/grid children containing tenant
names need `min-w-0` and `wrap-anywhere`. `card` provides intrinsic shrinkage and wrapping;
`PageHeader` also wraps long titles and breadcrumbs. Two-dimensional tables may scroll within
`table-wrap`; the document must not scroll sideways. Do not hide identity text just to fit a card.

- `Button` and `buttonClassName` define variants and sizes. Use `lg` (44px) for primary actions.
  Pending actions retain focus and prevent duplicate submission. Unavailable actions expose a reason.
- `TextField`, `SelectField`, and `TextAreaField` associate labels, hints, errors and required state.
  Use native constraints or the existing `noValidate` form error summary, with focus on invalid input.
- `Modal` owns focus containment, Escape, background inertness and trigger focus restoration.
  Keep form headers/actions separate from a shrinking scrollable body. Dirty forms require discard
  confirmation; pending requests must not silently discard entered data.
- `ScrollRegion` labels tables without focusable content and permits keyboard scrolling.
- `ErrorBanner` supports retry for transient failure. A missing entity needs navigation recovery.
- `EmptyState` distinguishes no data from filters with no matches. List views preserve filters and
  sort when returning from details. Dialog-only selectors keep their state local.

## Feedback and data safety

Label event delivery as Live only while the transport is connected. Preserve Pause independently
of filters. Identify bounded audit-history windows and offer older events before claiming no record.
Never store provider credentials, form drafts containing secrets, or one-time user tokens in browser
storage. Require token acknowledgement before dismissing the one-time display; navigation guards
warn before losing it. Authentication-token handling remains governed by SDD section 13 and 16.

## Motion and accessibility

Use the CSS transitions and keyframes already in `src/index.css`. No GSAP or new animation runtime.
Reduced motion removes nonessential movement; feedback must not depend on animation or hover.
Avoid moving controls during activation and avoid broad `transition: all` rules.

Use Lucide SVG icons, decorative icons marked `aria-hidden`, meaningful control names, semantic
headings and native form controls. Keep visible keyboard focus and ensure fixed navigation or
modal footers do not obscure it. Target at least 4.5:1 text contrast and 3:1 control/focus contrast.

## Verification

- Check both themes at 320/375/390/768/1024/1280/1440px, including unbroken long names.
- Exercise keyboard, pointer, mobile navigation, dialogs, errors, loading and empty states.
- Check Chromium and WebKit, text resizing, text spacing and reduced motion.
- Run component tests, typecheck, lint, production build/bundle gate and browser suites.
- Use axe and visual inspection together; automated checks do not certify complete WCAG compliance.
- Record fixture-based evidence separately from live provider or production validation.
