import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from 'react';
import { BrowserRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ApiProvider } from './api/ApiProvider';
import type { ApiClient } from './api/client';
import { Layout } from './components/Layout';
import { RequireAuth } from './components/RequireAuth';
import { LoadingBlock } from './components/Skeleton';
import { Button } from './components/Button';
import { ThemeProvider } from './theme/ThemeProvider';

// Route-level code splitting: charts (Recharts) only load with the pages that use them.
const Overview = lazy(() => import('./pages/Overview'));
const Plans = lazy(() => import('./pages/Plans'));
const PlanDetail = lazy(() => import('./pages/PlanDetail'));
const MigrationDetail = lazy(() => import('./pages/MigrationDetail'));
const Providers = lazy(() => import('./pages/Providers'));
const Users = lazy(() => import('./pages/Users'));
const Inventory = lazy(() => import('./pages/Inventory'));
const Events = lazy(() => import('./pages/Events'));
const Advisor = lazy(() => import('./pages/Advisor'));
const AdvisorAudit = lazy(() => import('./pages/AdvisorAudit'));
const AdvisorPolicies = lazy(() => import('./pages/AdvisorPolicies'));
const RecommendationDetail = lazy(() => import('./pages/RecommendationDetail'));
const Login = lazy(() => import('./pages/Login'));
const NotFound = lazy(() => import('./pages/NotFound'));

export class PageErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // the panel below is the recovery; this line is the trace support has: the error and where it happened
    console.error(error, info.componentStack);
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <section role="alert" className="card mx-auto mt-8 max-w-xl p-6">
        <h1 className="text-xl font-semibold text-foreground">This page could not be loaded</h1>
        <p className="mt-2 text-sm text-muted-foreground">The page could not start. Check the connection, then reload. If it still fails, contact your administrator.</p>
        <Button size="lg" variant="primary" className="mt-4" onClick={() => window.location.reload()}>
          Reload page
        </Button>
      </section>
    );
  }
}

function Page({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  return (
    <PageErrorBoundary key={pathname}>
      <Suspense fallback={<LoadingBlock label="Loading page…" rows={4} />}>{children}</Suspense>
    </PageErrorBoundary>
  );
}

const ROUTES: Array<{ path?: string; index?: true; element: ReactNode }> = [
  { index: true, element: <Overview /> },
  { path: 'plans', element: <Plans /> },
  { path: 'plans/:planId', element: <PlanDetail /> },
  { path: 'migrations/:migrationId', element: <MigrationDetail /> },
  { path: 'providers', element: <Providers /> },
  { path: 'users', element: <Users /> },
  { path: 'inventory', element: <Inventory /> },
  { path: 'inventory/:providerId', element: <Inventory /> },
  { path: 'events', element: <Events /> },
  { path: 'advisor', element: <Advisor /> },
  { path: 'advisor/audit', element: <AdvisorAudit /> },
  { path: 'advisor/policies', element: <AdvisorPolicies /> },
  { path: 'advisor/recommendations/:recId', element: <RecommendationDetail /> },
  { path: '*', element: <NotFound /> },
];

export function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <Page>
            <Login />
          </Page>
        }
      />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        {ROUTES.map((route) =>
          route.index ? (
            <Route key="index" index element={<Page>{route.element}</Page>} />
          ) : (
            <Route key={route.path} path={route.path} element={<Page>{route.element}</Page>} />
          ),
        )}
      </Route>
    </Routes>
  );
}

export interface AppProps {
  client: ApiClient;
  queryClient: QueryClient;
}

export default function App({ client, queryClient }: AppProps) {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ApiProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
