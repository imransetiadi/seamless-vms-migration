import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { lazy, Suspense, type ReactNode } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { ApiProvider } from './api/ApiProvider';
import type { ApiClient } from './api/client';
import { Layout } from './components/Layout';
import { RequireAuth } from './components/RequireAuth';
import { LoadingBlock } from './components/Skeleton';
import { ThemeProvider } from './theme/ThemeProvider';

// Route-level code splitting: charts (Recharts) only load with the pages that use them.
const Overview = lazy(() => import('./pages/Overview'));
const Plans = lazy(() => import('./pages/Plans'));
const PlanDetail = lazy(() => import('./pages/PlanDetail'));
const MigrationDetail = lazy(() => import('./pages/MigrationDetail'));
const Providers = lazy(() => import('./pages/Providers'));
const Inventory = lazy(() => import('./pages/Inventory'));
const Events = lazy(() => import('./pages/Events'));
const Advisor = lazy(() => import('./pages/Advisor'));
const Login = lazy(() => import('./pages/Login'));
const NotFound = lazy(() => import('./pages/NotFound'));

function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<LoadingBlock label="Loading page…" rows={4} />}>{children}</Suspense>;
}

const ROUTES: Array<{ path?: string; index?: true; element: ReactNode }> = [
  { index: true, element: <Overview /> },
  { path: 'plans', element: <Plans /> },
  { path: 'plans/:planId', element: <PlanDetail /> },
  { path: 'migrations/:migrationId', element: <MigrationDetail /> },
  { path: 'providers', element: <Providers /> },
  { path: 'inventory', element: <Inventory /> },
  { path: 'inventory/:providerId', element: <Inventory /> },
  { path: 'events', element: <Events /> },
  { path: 'advisor', element: <Advisor /> },
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
