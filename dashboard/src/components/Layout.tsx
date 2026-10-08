import {
  Activity,
  Bot,
  ClipboardList,
  HardDrive,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Server,
  X,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useHealth, useMe } from '../api/hooks';
import { useSignOut } from '../api/session';
import { cn } from '../lib/cn';
import { ROLE_LABELS } from '../lib/roles';
import { LiveIndicator } from './LiveIndicator';
import { ThemeToggle } from './ThemeToggle';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  match: (pathname: string) => boolean;
}

const NAV: NavItem[] = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, match: (p) => p === '/' },
  { to: '/plans', label: 'Plans', icon: ClipboardList, match: (p) => p.startsWith('/plans') || p.startsWith('/migrations') },
  { to: '/providers', label: 'Providers', icon: Server, match: (p) => p.startsWith('/providers') },
  { to: '/inventory', label: 'Inventory', icon: HardDrive, match: (p) => p.startsWith('/inventory') },
  { to: '/events', label: 'Events', icon: Activity, match: (p) => p.startsWith('/events') },
  { to: '/advisor', label: 'Advisor', icon: Bot, match: (p) => p.startsWith('/advisor') },
];

function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden className="size-7 shrink-0">
      <rect width="32" height="32" rx="6" className="fill-primary" />
      <path
        d="M7 11h12l-3.5-3.5M25 21H13l3.5 3.5"
        fill="none"
        className="stroke-accent"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Brand() {
  return (
    <Link to="/" className="flex min-h-11 items-center gap-2 rounded-md">
      <BrandMark />
      <span className="flex flex-col leading-tight">
        <span className="font-mono text-sm font-semibold text-foreground">Seamless Migrate</span>
        <span className="text-xs text-muted-foreground">to RHOSO 18.0</span>
      </span>
    </Link>
  );
}

function NavLinks({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <ul className="flex flex-col gap-0.5">
      {NAV.map((item) => {
        const active = item.match(pathname);
        return (
          <li key={item.to}>
            <Link
              to={item.to}
              onClick={onNavigate}
              aria-current={active ? 'page' : undefined}
              className={cn(
                'flex min-h-11 items-center gap-3 rounded-md border-l-2 px-3 text-sm transition-colors duration-200',
                active
                  ? 'border-accent bg-primary font-semibold text-primary-foreground'
                  : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
            >
              <item.icon aria-hidden className="size-4 shrink-0" />
              {item.label}
            </Link>
          </li>
        );
      })}
    </ul>
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
      {me.data && (
        <p className="px-1 text-sm">
          <span className="block truncate font-medium text-foreground">{me.data.name}</span>
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

/** App shell: skip link, sidebar navigation (≥1024 px) or a disclosure menu, and the main region. */
export function Layout() {
  const { pathname } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const lastPath = useRef(pathname);

  // Move focus to the main region after client-side navigation (screen readers announce the page).
  // Comparing paths (not a "first render" flag) keeps the initial load untouched — so the first Tab
  // reaches the skip link — even when StrictMode runs effects twice.
  useEffect(() => {
    setMenuOpen(false);
    if (lastPath.current === pathname) return;
    lastPath.current = pathname;
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [pathname]);

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <a href="#main" className="skip-link">
        Skip to main content
      </a>

      <aside className="sticky top-0 hidden h-dvh flex-col gap-4 border-r border-border bg-card px-3 py-4 lg:flex">
        <Brand />
        <nav aria-label="Primary" className="flex-1 overflow-y-auto">
          <NavLinks pathname={pathname} />
        </nav>
        <SessionFooter />
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-nav flex items-center gap-2 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur-sm lg:hidden">
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="mobile-navigation"
            aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
            className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-foreground transition-colors duration-200 hover:bg-muted"
          >
            {menuOpen ? <X aria-hidden className="size-5" /> : <Menu aria-hidden className="size-5" />}
          </button>
          <Brand />
          <div className="ml-auto flex items-center gap-1">
            <LiveIndicator compact />
            <ThemeToggle />
          </div>
        </header>
        {menuOpen && (
          <div id="mobile-navigation" className="border-b border-border bg-card px-3 py-3 lg:hidden">
            <nav aria-label="Primary">
              <NavLinks pathname={pathname} onNavigate={() => setMenuOpen(false)} />
            </nav>
            <div className="mt-3">
              <SessionFooter />
            </div>
          </div>
        )}

        <main
          id="main"
          ref={mainRef}
          tabIndex={-1}
          className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-4 outline-hidden md:px-6 md:py-6"
        >
          <Outlet />
        </main>
      </div>
    </div>
  );
}
