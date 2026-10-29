import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { Component, lazy, Suspense, useState, type ErrorInfo, type ReactNode } from 'react';
import { createBrowserRouter, RouterProvider, Route, Routes, useLocation } from 'react-router-dom';
import { ApiProvider } from './api/ApiProvider';
import type { ApiClient } from './api/client';
import { Layout } from './components/Layout';
import { RequireAuth } from './components/RequireAuth';
import { LoadingBlock } from './components/Skeleton';
import { Button } from './components/Button';
import { routeChunk, reloadPage } from './lib/routeChunk';
import { ThemeProvider } from './theme/ThemeProvider';

// Route-level code splitting: charts (Recharts) only load with the pages that use them.
const Overview = lazy(() => routeChunk('Overview', () => import('./pages/Overview')));
const Plans = lazy(() => routeChunk('Plans', () => import('./pages/Plans')));
const PlanDetail = lazy(() => routeChunk('PlanDetail', () => import('./pages/PlanDetail')));
const MigrationDetail = lazy(() => routeChunk('MigrationDetail', () => import('./pages/MigrationDetail')));
const Providers = lazy(() => routeChunk('Providers', () => import('./pages/Providers')));
const Users = lazy(() => routeChunk('Users', () => import('./pages/Users')));
const Hooks = lazy(() => routeChunk('Hooks', () => import('./pages/Hooks')));
const Inventory = lazy(() => routeChunk('Inventory', () => import('./pages/Inventory')));
const Events = lazy(() => routeChunk('Events', () => import('./pages/Events')));
const Notifications = lazy(() => routeChunk('Notifications', () => import('./pages/Notifications')));
const Advisor = lazy(() => routeChunk('Advisor', () => import('./pages/Advisor')));
const AdvisorAudit = lazy(() => routeChunk('AdvisorAudit', () => import('./pages/AdvisorAudit')));
const AdvisorPolicies = lazy(() => routeChunk('AdvisorPolicies', () => import('./pages/AdvisorPolicies')));
const RecommendationDetail = lazy(() => routeChunk('RecommendationDetail', () => import('./pages/RecommendationDetail')));
const Login = lazy(() => routeChunk('Login', () => import('./pages/Login')));
const NotFound = lazy(() => routeChunk('NotFound', () => import('./pages/NotFound')));

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
        <Button size="lg" variant="primary" className="mt-4" onClick={reloadPage}>
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
  { path: 'hooks', element: <Hooks /> },
  { path: 'migrations/:migrationId', element: <MigrationDetail /> },
  { path: 'providers', element: <Providers /> },
  { path: 'users', element: <Users /> },
  { path: 'inventory', element: <Inventory /> },
  { path: 'inventory/:providerId', element: <Inventory /> },
  { path: 'events', element: <Events /> },
  { path: 'notifications', element: <Notifications /> },
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
  const [router] = useState(() => createBrowserRouter([{ path: '*', element: <AppRoutes /> }]));
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <RouterProvider router={router} />
        </ApiProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
