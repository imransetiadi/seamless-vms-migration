import { CircleAlert, CircleCheck, CircleX, Eye, EyeOff, LogIn } from 'lucide-react';
import { useId, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ApiError, errorMessage } from '../api/client';
import { useHealth } from '../api/hooks';
import { useSignIn } from '../api/session';
import { Button } from '../components/Button';
import { ThemeToggle } from '../components/ThemeToggle';
import { usePageTitle } from '../lib/usePageTitle';

/**
 * Token sign-in (SDD §13.1): the bearer token is verified with GET /me and kept in sessionStorage,
 * so it disappears with the tab. Paste and password managers work (WCAG 2.2 accessible authentication).
 */
export default function Login() {
  usePageTitle('Sign in');
  const tokenId = useId();
  const signIn = useSignIn();
  const health = useHealth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? '/';
  const [token, setToken] = useState('');
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const mock = import.meta.env.VITE_SEAMLESS_MOCK === '1';

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token.trim()) {
      setError('Paste your API token to sign in.');
      inputRef.current?.focus();
      return;
    }
    setPending(true);
    setError(null);
    try {
      await signIn(token);
      navigate(from, { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'The token was rejected. Check that it is complete and has not been revoked.'
          : err instanceof ApiError && err.status === 429
            ? 'Too many failed sign-in attempts from this address. Wait a minute, then try again.'
            : errorMessage(err),
      );
      setPending(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <div className="flex justify-end p-3">
        <ThemeToggle />
      </div>
      <main id="main" className="flex flex-1 items-start justify-center px-4 pb-16 pt-6 sm:items-center">
        <div className="card w-full max-w-md p-6">
          <div className="mb-5 flex items-center gap-3">
            <svg viewBox="0 0 32 32" aria-hidden className="size-9 shrink-0">
              <rect width="32" height="32" rx="6" className="fill-primary" />
              <path d="M7 11h12l-3.5-3.5M25 21H13l3.5 3.5" fill="none" className="stroke-accent" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <div>
              <p className="font-mono text-sm font-semibold text-foreground">Seamless Migrate</p>
              <p className="text-xs text-muted-foreground">VM migrations to RHOSO 18.0</p>
            </div>
          </div>
          <h1 className="text-xl font-semibold text-foreground">Sign in</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Paste the API token issued with <code>seamless token create</code>. It is kept in this browser tab only.
          </p>

          <form onSubmit={submit} noValidate className="mt-5 flex flex-col gap-4">
            {/* Lets password managers associate the saved token with this service. */}
            <input type="text" name="username" autoComplete="username" value="seamless" readOnly hidden />
            <div>
              <label htmlFor={tokenId} className="field-label">
                API token
              </label>
              <div className="flex gap-2">
                <input
                  ref={inputRef}
                  id={tokenId}
                  name="token"
                  type={show ? 'text' : 'password'}
                  autoComplete="current-password"
                  spellCheck={false}
                  autoCapitalize="off"
                  className="input font-mono"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${tokenId}-error` : `${tokenId}-hint`}
                />
                <button
                  type="button"
                  aria-pressed={show}
                  aria-controls={tokenId}
                  onClick={() => setShow((v) => !v)}
                  className="inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center rounded-md border border-input text-foreground transition-colors duration-200 hover:bg-muted"
                >
                  {show ? <EyeOff aria-hidden className="size-4" /> : <Eye aria-hidden className="size-4" />}
                  <span className="sr-only">Show token</span>
                </button>
              </div>
              {error ? (
                <p id={`${tokenId}-error`} role="alert" className="field-error">
                  <CircleAlert aria-hidden className="size-3.5 shrink-0" />
                  {error}
                </p>
              ) : (
                <p id={`${tokenId}-hint`} className="field-hint">
                  Tokens start with <code>smg_</code>. Your role (viewer, operator, approver, admin) comes with the token.
                </p>
              )}
            </div>
            <Button type="submit" variant="primary" size="lg" icon={LogIn} loading={pending} className="w-full">
              Sign in
            </Button>
          </form>

          <div className="mt-5 flex flex-col gap-2 border-t border-border pt-4 text-xs text-muted-foreground">
            {health.data ? (
              <p className="flex items-center gap-1.5">
                <CircleCheck aria-hidden className="size-3.5 text-status-success" />
                Control plane {health.data.version} reachable{health.data.demo ? ' · demo mode' : ''}
              </p>
            ) : health.error ? (
              <p className="flex items-center gap-1.5 text-status-danger">
                <CircleX aria-hidden className="size-3.5" />
                The control plane is not reachable right now.
              </p>
            ) : null}
            {health.data?.demo && (
              <p>
                Demo mode on loopback usually runs without authentication:{' '}
                <Link to={from} className="text-foreground underline underline-offset-4">
                  continue without a token
                </Link>
                .
              </p>
            )}
            {mock && (
              <p>
                Mock API: use the token <code>viewer</code>, <code>operator</code>, <code>approver</code> or <code>admin</code>.
              </p>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
