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
`src/main.tsx` imports `@fontsource/fira-code` (Latin 400/500/600) and `@fontsource/fira-sans` (Latin 400/500/600);
the CSP allows fonts from the control plane only.
Typical body text is `text-sm`; input text is 16px on mobile to avoid automatic input zoom.
Use `num` for tabular numbers, with units and an explicit unknown state.

Typography scale (root-relative; browser default root is 16 px):

| Role | Size / weight | Line height |
| --- | --- | --- |
| Body and controls | 1rem inputs below 640px; 0.875rem inputs from 640px and dense copy, 400/500 | 1.5 |
| Hints, dates, chart labels and rail navigation | At least 0.75rem, 400/500 | 1.5; compact navigation labels may use 1.25 |
| Page heading | 1.25rem mobile / 1.5rem desktop, Fira Code 600 | Default heading rhythm |
| Panel heading | 0.875rem, Fira Code 600 | 1.5 |
| Metrics and timers | 1.25–2.25rem, Fira Code 600 with tabular numerals | 1–1.25 for numeric display |

Use `rem` instead of fixed-pixel font sizes. Preserve text at 200% root size and with user text-spacing overrides;
labels and values wrap when space runs out. Charts retain a readable minimum width in a named keyboard-scrollable
region, with the existing data-table alternative. Font faces use `font-display: swap` and system fallbacks.
No 700-weight copy is used, so its unused font import is omitted. These sizes are a dashboard convention, not a
claim that WCAG prescribes a minimum font size.

## Layout and controls

Use existing Tailwind spacing: `gap-2` for controls, `gap-3` or `gap-4` for related sections,
`p-4` for cards, and responsive grids with `minmax(0, 1fr)`. Flex/grid children containing tenant
names need `min-w-0` and `wrap-anywhere`. `card` provides intrinsic shrinkage and wrapping;
`PageHeader` also wraps long titles and breadcrumbs. Two-dimensional tables may scroll within
`table-wrap`; the document must not scroll sideways. Do not hide identity text just to fit a card.

- `Button` and `buttonClassName` define variants and sizes. Use `lg` (44px) for primary actions.
  Pending actions retain focus and prevent duplicate submission. Unavailable actions expose a reason.
  Use `Button` or `buttonClassName` for links with button styling. Primary actions use the accent token;
  secondary actions use the card surface and border. Danger actions keep their own tone and confirmation.
  Targets are at least 44px below 640px; compact desktop buttons use 32/36px, primary actions remain 44px.
  Labels wrap, focus remains visible, and hover/pressed states never move the layout.
- `TextField`, `SelectField`, and `TextAreaField` associate labels, hints, errors and required state.
  Use native constraints or the existing `noValidate` form error summary, with focus on invalid input.
  Inputs retain their semantic boundary/focus colours, visible labels and mobile 44px height.
- `Modal` owns focus containment, Escape, background inertness and trigger focus restoration.
  Keep form headers/actions separate from a shrinking scrollable body. Dirty forms require discard
  confirmation; pending requests must not silently discard entered data. Use the existing `Modal` and
  `ConfirmDialog` components: dialog actions stay below the scrolling body; surfaces use theme tokens,
  including the soft light palette. Do not replace validation, focus trapping or error recovery with
  visual-only alternatives.
- `ScrollRegion` labels tables without focusable content and permits keyboard scrolling.
- `ErrorBanner` supports retry for transient failure. A missing entity needs navigation recovery.
- `EmptyState` distinguishes no data from filters with no matches. List views preserve filters and
  sort when returning from details. Dialog-only selectors keep their state local.

## Cards and page headers

Use `.card`: 12px radius, semantic card/border/text tokens and a quiet shadow. Static cards do not lift on
hover or show a pointer cursor. `Panel` uses a subtle tinted header and 16px padding. `PageHeader` groups
title, description, context and actions in one card, with 12px mobile / 20px wider padding. Long descriptions
use a readable measure. Metric strips keep shared rules rather than eight separate floating cards.

## Brand

Keep the SDD §16 continuous S and fixed brand colours in every asset. The shell mark is 32px with 12px
clear space before its wordmark; sign-in uses a 48px mark with 16px clear space. Preserve SVG proportions,
decorative semantics and unique gradient IDs. Sign-in branding wraps rather than squeezing the wordmark
when text is enlarged. Refine presentation without introducing another logo.

## Page pattern: operations landing

- **Primary task:** Monitor VM migration state and reach the next human action. Keep loading, failed and stale data explicit.
- **CTA Placement:** Page task in the header; object actions beside their context. Overview links to Plans after its plan summary.
- **Section Order:** Scope and context > Key metrics > Downtime now > Approvals and attention > History/charts > Plan progress.
- **Columns:** Overview uses an operational 3:2 split from 1024px, one column below; DOM order keeps clocks before action queues.
- **Hero card:** Downtime now has a neutral information accent, without implying healthy status. VM status always keeps its own icon and label.
- **First screen:** Preserve the first clock and SLO at 390×844. Shorten repeated explanatory copy before shrinking font or hiding metrics.

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
