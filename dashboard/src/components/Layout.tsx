import {
  Activity,
  Bot,
  CircleX,
  ClipboardList,
  HardDrive,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  Server,
  SlidersHorizontal,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useHealth, useMe, useStats } from '../api/hooks';
import { useLiveSnapshot, type LiveStatus } from '../api/live';
import { useSignOut } from '../api/session';
import { cn } from '../lib/cn';
import { keepsFocus } from '../lib/navigation';
import { ROLE_LABELS } from '../lib/roles';
import { BrandMark } from './BrandMark';
import { LiveIndicator } from './LiveIndicator';
import { Modal } from './Modal';
import { ThemeToggle } from './ThemeToggle';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  match: (pathname: string) => boolean;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

/** The navigation's groups and items, in this order (SDD §16). */
const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Migrations',
    items: [
      { to: '/', label: 'Overview', icon: LayoutDashboard, match: (p) => p === '/' },
      { to: '/plans', label: 'Plans', icon: ClipboardList, match: (p) => p.startsWith('/plans') || p.startsWith('/migrations') },
    ],
  },
  {
    label: 'Clouds',
    items: [
      { to: '/providers', label: 'Providers', icon: Server, match: (p) => p.startsWith('/providers') },
      { to: '/inventory', label: 'Inventory', icon: HardDrive, match: (p) => p.startsWith('/inventory') },
    ],
  },
  {
    label: 'Activity',
    items: [{ to: '/events', label: 'Events', icon: Activity, match: (p) => p.startsWith('/events') }],
  },
  {
    label: 'Advisor',
    items: [
      { to: '/advisor', label: 'Recommendations', icon: Bot, match: (p) => p === '/advisor' || p.startsWith('/advisor/recommendations') },
      { to: '/advisor/policies', label: 'Policies', icon: SlidersHorizontal, match: (p) => p.startsWith('/advisor/policies') },
      { to: '/advisor/audit', label: 'Audit trail', icon: ScrollText, match: (p) => p.startsWith('/advisor/audit') },
    ],
  },
  {
    label: 'Administration',
    items: [{ to: '/users', label: 'Users', icon: Users, match: (p) => p === '/users' }],
  },
];

type NavMode = 'full' | 'rail';
const NAV_STORAGE_KEY = 'seamless.nav';
const WIDE_QUERY = '(min-width: 1280px)';

function readStoredMode(): NavMode | null {
  try {
    const value = window.localStorage.getItem(NAV_STORAGE_KEY);
    return value === 'full' || value === 'rail' ? value : null;
  } catch {
    return null; // storage refused (private window, blocked site data): the width decides
  }
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);
  return matches;
}

/**
 * The rail (labelled icons) or the full sidebar: the viewer's stored choice, else the width — the full sidebar from
 * 1280 px (SDD §16). The choice is remembered in this browser; a storage that refuses it only forgets it.
 */
function useNavMode(): [NavMode, () => void] {
  const wide = useMediaQuery(WIDE_QUERY);
  const [stored, setStored] = useState<NavMode | null>(readStoredMode);
  const mode: NavMode = stored ?? (wide ? 'full' : 'rail');
  const toggle = () => {
    const next: NavMode = mode === 'full' ? 'rail' : 'full';
    setStored(next);
    try {
      window.localStorage.setItem(NAV_STORAGE_KEY, next);
    } catch {
      // kept for this page only
    }
  };
  return [mode, toggle];
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <Link to="/" className={cn('flex min-h-11 items-center gap-2 rounded-md', compact && 'justify-center')}>
      <BrandMark className="size-7 shrink-0" />
      <span className={cn('flex flex-col leading-tight', compact && 'sr-only')}>
        <span className="font-mono text-sm font-semibold text-foreground">Seamless Migrate</span>
        <span className="text-xs text-muted-foreground">to RHOSO 18.0</span>
      </span>
    </Link>
  );
}

/**
 * The grouped items (SDD §16): in the full sidebar each label beside its icon under the group's heading, in the rail
 * each label under its icon and the groups apart by a rule. Plans is described by the failed count from /stats.
 */
function NavGroups({ pathname, mode, onNavigate }: { pathname: string; mode: NavMode; onNavigate?: (to: string) => void }) {
  const prefix = useId();
  const me = useMe();
  const stats = useStats();
  const failed = stats.data?.failed ?? 0;
  const rail = mode === 'rail';
  return (
    <div className={cn('flex flex-col', rail ? 'gap-2' : 'gap-4')}>
      {NAV_GROUPS.map((group, index) => {
        if (group.label === 'Administration' && me.data?.role !== 'admin') return null;
        const headingId = `${prefix}-group-${index}`;
        return (
          <div key={group.label} role="group" aria-labelledby={headingId} className={cn(rail && index > 0 && 'border-t border-border pt-2')}>
            <p id={headingId} className={rail ? 'sr-only' : 'px-3 pb-1 text-xs font-medium text-muted-foreground'}>
              {group.label}
            </p>
            <ul className="flex flex-col gap-0.5">
              {group.items.map((item) => {
                const active = item.match(pathname);
                const pillId = `${prefix}-failed`;
                const pill = item.to === '/plans' && failed > 0;
                return (
                  <li key={item.to} className="relative">
                    <Link
                      to={item.to}
                      onClick={onNavigate ? () => onNavigate(item.to) : undefined}
                      aria-current={active ? 'page' : undefined}
                      aria-describedby={pill ? pillId : undefined}
                      className={cn(
                        'flex min-h-11 rounded-md transition-colors duration-200',
                        rail
                          ? 'flex-col items-center justify-center gap-1 border-l-2 px-1 py-1.5 text-center text-[11px] leading-tight'
                          : 'items-center gap-3 border-l-2 px-3 text-sm',
                        // room for the failed count under the label
                        rail && pill && 'pb-6',
                        active
                          ? 'border-accent bg-primary font-semibold text-primary-foreground'
                          : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
                      )}
                    >
                      <item.icon aria-hidden className="size-4 shrink-0" />
                      {/* in the rail a long label wraps, hyphenated, never cut off (SDD §16) */}
                      <span className={rail ? 'max-w-full hyphens-auto [overflow-wrap:anywhere]' : undefined}>{item.label}</span>
                    </Link>
                    {pill && (
                      // the link's description, so its name stays "Plans" (SDD §16); shown over the link, which keeps
                      // the clicks
                      <span
                        id={pillId}
                        aria-hidden
                        className={cn(
                          'pointer-events-none absolute inline-flex items-center gap-1 rounded-sm border border-status-danger/50 bg-card font-medium text-status-danger',
                          // in the rail under the label, inside the item; in the sidebar at the item's right end
                          rail
                            ? 'bottom-1.5 left-1/2 -translate-x-1/2 whitespace-nowrap px-1 text-[10px]'
                            : 'right-2 top-1/2 -translate-y-1/2 px-1.5 py-0.5 text-xs',
                        )}
                      >
                        <CircleX aria-hidden className="size-3 shrink-0" />
                        {failed} failed
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Shown above every page while GET /health is degraded or unreachable (SDD §12). The live
 * region is always mounted so screen readers announce the text when it appears.
 */
function HealthNotice() {
  const health = useHealth();
  let tone: 'danger' | 'warning' | null = null;
  let text = '';
  if (health.isError) {
    tone = 'warning';
    text = 'The control plane is not answering health checks; the data below may be stale.';
  } else if (health.data && health.data.status === 'degraded') {
    const h = health.data;
    const reasons: string[] = [];
    if (h.db !== 'ok') reasons.push('the database is unreachable');
    if (h.orchestrator && !h.orchestrator.healthy) {
      const age = h.orchestrator.last_tick_age_s;
      reasons.push(
        !h.orchestrator.running
          ? 'the orchestrator loop is not running'
          : age === null
            ? 'the orchestrator loop is unhealthy before its first tick'
            : `the orchestrator loop is unhealthy (last tick ${Math.round(age)} s ago)`,
      );
    }
    tone = 'danger';
    text = `Control plane degraded: ${reasons.length ? reasons.join('; ') : 'see the service logs'}. Migrations do not advance until this is resolved.`;
  }
  return (
    <p
      role="status"
      aria-live="polite"
      data-testid="health-notice"
      className={cn(
        tone === null && 'sr-only',
        tone === 'danger' && 'mb-4 rounded-md border border-status-danger/50 bg-status-danger/10 px-3 py-2 text-sm text-foreground',
        tone === 'warning' && 'mb-4 rounded-md border border-status-warning/50 bg-status-warning/10 px-3 py-2 text-sm text-foreground',
      )}
    >
      {text}
    </p>
  );
}

/**
 * The live event stream's changes once it has been live — lost, back, stopped, signed out — said once for the
 * page from a region already on it; the Live indicators only show them, and the first connection is not news
 * (SDD §16).
 */
function StreamNotice() {
  const { status } = useLiveSnapshot();
  const [text, setText] = useState('');
  const wasOpen = useRef(false);
  const last = useRef<LiveStatus>(status);
  useEffect(() => {
    const previous = last.current;
    last.current = status;
    if (status === previous) return;
    if (status === 'open') {
      if (wasOpen.current) setText('Live updates back.');
      wasOpen.current = true;
      return;
    }
    if (!wasOpen.current) return;
    if (status === 'reconnecting') setText('Live updates lost: reconnecting.');
    else if (status === 'unauthorized') setText('Signed out: live updates stopped.');
    else if (status === 'closed' || status === 'idle') setText('Live updates stopped: the page shows what it last received.');
  }, [status]);
  return (
    <p role="status" className="sr-only">
      {text}
    </p>
  );
}

/** The rail's foot: the live indicator, the theme and sign out as labelled icons (SDD §16). */
function RailSession() {
  const me = useMe();
  const signOut = useSignOut();
  const anonymous = me.data?.name === 'anonymous';
  const railButton =
    'flex min-h-11 w-full cursor-pointer flex-col items-center justify-center gap-1 rounded-md px-1 py-1.5 text-center text-[11px] leading-tight text-foreground transition-colors duration-200 hover:bg-muted';
  return (
    <div className="flex flex-col items-center gap-1 border-t border-border pt-2">
      <LiveIndicator stacked className="py-1" />
      <ThemeToggle stacked className="w-full" />
      {anonymous ? (
        <Link to="/login" className={railButton}>
          <KeyRound aria-hidden className="size-4" />
          Use a token
        </Link>
      ) : (
        <button type="button" onClick={signOut} className={railButton}>
          <LogOut aria-hidden className="size-4" />
          Sign out
        </button>
      )}
    </div>
  );
}

function SessionFooter() {
  const me = useMe();
  const health = useHealth();
  const signOut = useSignOut();
  const anonymous = me.data?.name === 'anonymous';
  return (
    <div className="flex flex-col gap-2 border-t border-border pt-3">
      <div className="flex items-center justify-between gap-2 px-1">
        <LiveIndicator />
        {health.data?.demo && (
          <span className="rounded-sm border border-status-info/40 px-1.5 py-0.5 text-xs text-status-info">Demo</span>
        )}
      </div>
      {health.data?.orchestrator && (
        <p className="px-1 text-xs text-muted-foreground" data-testid="orchestrator-status">
          Orchestrator{' '}
          {health.data.orchestrator.healthy
            ? health.data.orchestrator.last_tick_age_s === null
              ? 'starting'
              : `running · last tick ${Math.round(health.data.orchestrator.last_tick_age_s)} s ago`
            : health.data.orchestrator.running
              ? 'stalled'
              : 'stopped'}
        </p>
      )}
      {health.data?.version && (
        // the sign-in page names it too, but with auth disabled nobody sees that page
        <p className="px-1 text-xs text-muted-foreground">Control plane {health.data.version}</p>
      )}
      {me.data && (
        <p className="px-1 text-sm">
          <span className="block wrap-break-word font-medium text-foreground">{me.data.name}</span>
          <span className="text-xs text-muted-foreground">{ROLE_LABELS[me.data.role] ?? me.data.role}</span>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1">
        <ThemeToggle showLabel />
        {anonymous ? (
          <Link
            to="/login"
            className="inline-flex min-h-11 items-center gap-2 rounded-md px-2 text-sm text-foreground transition-colors duration-200 hover:bg-muted"
          >
            <KeyRound aria-hidden className="size-4" />
            Use a token
          </Link>
        ) : (
          <button
            type="button"
            onClick={signOut}
            className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 text-sm text-foreground transition-colors duration-200 hover:bg-muted"
          >
            <LogOut aria-hidden className="size-4" />
            Sign out
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * App shell (SDD §16): skip link, the navigation on the left — from 768 px a column, the rail or the full sidebar, from
 * below 768 px a drawer from the left behind the header's menu button — and the main region.
 */
export function Layout() {
  const { pathname, hash, state } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [mode, toggleMode] = useNavMode();
  const mainRef = useRef<HTMLElement>(null);
  // where the focus goes when the drawer closes: its button, or the page when the choice was the page already shown
  const drawerReturnRef = useRef<HTMLElement | null>(null);
  const drawerTitleId = useId();
  const columnNavId = useId();
  const lastPath = useRef(pathname);
  const closeMenu = () => {
    drawerReturnRef.current = null;
    setMenuOpen(false);
  };
  // the drawer closes on a choice; the link goes with it, so the focus goes where a navigation puts it — the page —
  // even when the choice is the page already shown, which moves nowhere (SDD §16)
  const navigateFromMenu = (to: string) => {
    drawerReturnRef.current = to === pathname ? mainRef.current : null;
    setMenuOpen(false);
  };

  // Move focus to the main region after client-side navigation (screen readers announce the page).
  // Comparing paths (not a "first render" flag) keeps the initial load untouched — so the first Tab
  // reaches the skip link — even when StrictMode runs effects twice.
  // A page that lands on the part a link names (`/advisor/policies#policy-…`) keeps the focus there: its effect
  // runs first in the same commit. While it still loads that part, main takes focus and the page moves it
  // there once it shows (SDD §16).
  useEffect(() => {
    setMenuOpen(false);
    if (lastPath.current === pathname) return;
    lastPath.current = pathname;
    // a control that switches the view in place (the inventory's Provider select) keeps the focus: moving it on
    // each choice would be a change of context on input (WCAG 3.2.2, SDD §16)
    if (keepsFocus(state)) return;
    const target = hash.length > 1 ? document.getElementById(decodeURIComponent(hash.slice(1))) : null;
    if (target && document.activeElement === target) return;
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [pathname, hash, state]);

  const full = mode === 'full';
  return (
    <div className="min-h-dvh md:grid md:grid-cols-[auto_minmax(0,1fr)]">
      <a href="#main" className="skip-link">
        Skip to main content
      </a>

      <aside
        data-testid="app-navigation"
        data-nav={mode}
        className={cn(
          'sticky top-0 hidden h-dvh flex-col gap-3 border-r border-border bg-card py-4 md:flex',
          full ? 'w-60 px-3' : 'w-20 px-1.5',
        )}
      >
        <Brand compact={!full} />
        <nav id={columnNavId} aria-label="Primary" className="flex-1 overflow-y-auto">
          <NavGroups pathname={pathname} mode={mode} />
        </nav>
        {full ? <SessionFooter /> : <RailSession />}
        <button
          type="button"
          onClick={toggleMode}
          aria-expanded={full}
          aria-controls={columnNavId}
          aria-label={full ? 'Collapse navigation' : 'Expand navigation'}
          className={cn(
            'flex min-h-11 cursor-pointer items-center rounded-md text-muted-foreground transition-colors duration-200 hover:bg-muted hover:text-foreground',
            full ? 'gap-2 px-2 text-sm' : 'flex-col justify-center gap-1 px-1 py-1.5 text-[11px] leading-tight',
          )}
        >
          {full ? <PanelLeftClose aria-hidden className="size-4 shrink-0" /> : <PanelLeftOpen aria-hidden className="size-4 shrink-0" />}
          {full ? 'Collapse navigation' : 'Expand'}
        </button>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-nav flex items-center gap-2 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur-sm md:hidden">
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            aria-haspopup="dialog"
            aria-expanded={menuOpen}
            aria-label="Open navigation"
            className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-foreground transition-colors duration-200 hover:bg-muted"
          >
            <Menu aria-hidden className="size-5" />
          </button>
          <Brand />
          <div className="ml-auto flex items-center gap-1">
            <LiveIndicator compact />
            <ThemeToggle />
          </div>
        </header>
        <Modal
          open={menuOpen}
          onClose={closeMenu}
          labelledBy={drawerTitleId}
          placement="left"
          closeOnBackdrop
          returnFocusRef={drawerReturnRef}
        >
          <div className="flex min-h-full flex-col gap-4 px-3 py-4">
            <div className="flex items-center justify-between gap-2">
              <h2 id={drawerTitleId} className="sr-only">
                Navigation
              </h2>
              <Brand />
              <button
                type="button"
                onClick={closeMenu}
                aria-label="Close navigation"
                className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-foreground transition-colors duration-200 hover:bg-muted"
              >
                <X aria-hidden className="size-5" />
              </button>
            </div>
            <nav aria-label="Primary" className="flex-1">
              <NavGroups pathname={pathname} mode="full" onNavigate={navigateFromMenu} />
            </nav>
            <SessionFooter />
          </div>
        </Modal>

        <main
          id="main"
          ref={mainRef}
          tabIndex={-1}
          className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-4 outline-hidden md:px-6 md:py-6"
        >
          <HealthNotice />
          <StreamNotice />
          <Outlet />
        </main>
      </div>
    </div>
  );
}
