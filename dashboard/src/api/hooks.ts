/**
 * TanStack Query hooks over the SDD §12 routes. Components never call `fetch` directly.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from '@tanstack/react-query';
import { createContext, useContext } from 'react';
import { ApiClient, ApiError } from './client';
import type {
  AdvisorStatus,
  ApproveRequest,
  AutoWavesRequest,
  CancelRequest,
  CutoverRequest,
  Event,
  EventListQuery,
  FinalizeRequest,
  Health,
  Me,
  Migration,
  MigrationAction,
  MigrationListQuery,
  Plan,
  PlanCreate,
  PlanPatch,
  Provider,
  ProviderInventory,
  RollbackRequest,
  SimilarIncidentsRequest,
  SimilarIncidentsResponse,
  Stats,
  Strategy,
  ValidationReport,
} from './types';

export const ApiContext = createContext<ApiClient | null>(null);

export function useApi(): ApiClient {
  const client = useContext(ApiContext);
  if (!client) throw new Error('useApi must be used inside <ApiProvider>.');
  return client;
}

const enc = encodeURIComponent;

export const queryKeys = {
  me: ['me'] as const,
  health: ['health'] as const,
  providers: ['providers'] as const,
  provider: (id: string) => ['providers', id] as const,
  inventory: (id: string) => ['inventory', id] as const,
  plans: ['plans'] as const,
  plan: (id: string) => ['plans', id] as const,
  migrations: (query: MigrationListQuery = {}) => ['migrations', query] as const,
  migration: (id: string) => ['migration', id] as const,
  eventTail: (query: Omit<EventListQuery, 'since' | 'limit'> = {}) => ['events', 'tail', query] as const,
  stats: (planId?: string | null) => ['stats', planId ?? null] as const,
  advisorStatus: ['advisor', 'status'] as const,
};

function noRetryOnClientErrors(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

// ---------------------------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------------------------

export function useMe() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: ({ signal }) => api.get<Me>('/me', { signal }),
    retry: noRetryOnClientErrors,
    staleTime: 60_000,
  });
}

export function useHealth() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => api.get<Health>('/health', { signal }),
    refetchInterval: 60_000,
    retry: false,
  });
}

export function useProviders() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.providers,
    queryFn: ({ signal }) => api.get<Provider[]>('/providers', { signal }),
    retry: noRetryOnClientErrors,
  });
}

export function useInventory(providerId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.inventory(providerId ?? ''),
    queryFn: ({ signal }) => api.get<ProviderInventory>(`/providers/${enc(providerId ?? '')}/inventory`, { signal }),
    enabled: Boolean(providerId),
    retry: noRetryOnClientErrors,
    staleTime: 60_000,
  });
}

export function usePlans() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.plans,
    queryFn: ({ signal }) => api.get<Plan[]>('/plans', { signal }),
    retry: noRetryOnClientErrors,
  });
}

export function usePlan(planId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.plan(planId ?? ''),
    queryFn: ({ signal }) => api.get<Plan>(`/plans/${enc(planId ?? '')}`, { signal }),
    enabled: Boolean(planId),
    retry: noRetryOnClientErrors,
  });
}

export interface ListOptions {
  enabled?: boolean;
  /** Hold the previous result while a new filter loads (no skeleton flash on refilter). */
  keepPrevious?: boolean;
}

export function useMigrations(query: MigrationListQuery = {}, options: ListOptions = {}) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.migrations(query),
    queryFn: ({ signal }) =>
      api.get<Migration[]>('/migrations', {
        signal,
        query: {
          plan_id: query.plan_id,
          phase: query.phase,
          wave_id: query.wave_id,
          limit: query.limit,
          offset: query.offset,
        },
      }),
    enabled: options.enabled ?? true,
    placeholderData: options.keepPrevious ? keepPreviousData : undefined,
    retry: noRetryOnClientErrors,
  });
}

export function useMigration(migrationId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.migration(migrationId ?? ''),
    queryFn: ({ signal }) => api.get<Migration>(`/migrations/${enc(migrationId ?? '')}`, { signal }),
    enabled: Boolean(migrationId),
    retry: noRetryOnClientErrors,
  });
}

export function useStats(planId?: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.stats(planId),
    queryFn: ({ signal }) => api.get<Stats>('/stats', { signal, query: { plan_id: planId ?? undefined } }),
    refetchInterval: 15_000,
    placeholderData: keepPreviousData,
    retry: noRetryOnClientErrors,
  });
}

export function useAdvisorStatus() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.advisorStatus,
    queryFn: ({ signal }) => api.get<AdvisorStatus>('/advisor/status', { signal }),
    refetchInterval: 30_000,
    retry: noRetryOnClientErrors,
  });
}

const EVENT_PAGE = 1000;

/**
 * The newest `keep` persisted events. `GET /events` pages forward from `since` (SDD §12), so the
 * tail is reached by following the cursor until a short page arrives.
 */
export async function fetchEventTail(
  api: ApiClient,
  query: Omit<EventListQuery, 'since' | 'limit'> = {},
  keep = 500,
  signal?: AbortSignal,
): Promise<Event[]> {
  let since = 0;
  let tail: Event[] = [];
  for (let page = 0; page < 200; page += 1) {
    const batch = await api.get<Event[]>('/events', {
      signal,
      query: { since, limit: EVENT_PAGE, plan_id: query.plan_id, migration_id: query.migration_id },
    });
    tail = tail.concat(batch).slice(-keep);
    const last = batch.at(-1);
    if (batch.length < EVENT_PAGE || !last) break;
    since = last.seq;
  }
  return tail;
}

export function useEventTail(query: Omit<EventListQuery, 'since' | 'limit'> = {}, keep = 500) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.eventTail(query),
    queryFn: ({ signal }) => fetchEventTail(api, query, keep, signal),
    retry: noRetryOnClientErrors,
    staleTime: 30_000,
  });
}

// ---------------------------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------------------------

function invalidateAll(queryClient: QueryClient, keys: QueryKey[]) {
  for (const queryKey of keys) void queryClient.invalidateQueries({ queryKey });
}

export function useCheckProvider() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (providerId: string) => api.post<Provider>(`/providers/${enc(providerId)}/check`),
    onSuccess: (provider) => {
      queryClient.setQueryData<Provider[]>(queryKeys.providers, (old) =>
        old?.map((p) => (p.id === provider.id ? provider : p)),
      );
      invalidateAll(queryClient, [queryKeys.providers, queryKeys.inventory(provider.id)]);
    },
  });
}

export function useDeleteProvider() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (providerId: string) => api.delete(`/providers/${enc(providerId)}`),
    onSuccess: () => invalidateAll(queryClient, [queryKeys.providers]),
  });
}

export function useCreatePlan() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PlanCreate) => api.post<Plan>('/plans', body),
    onSuccess: () => invalidateAll(queryClient, [queryKeys.plans, ['migrations'], ['stats']]),
  });
}

export function usePatchPlan(planId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PlanPatch) => api.patch<Plan>(`/plans/${enc(planId)}`, body),
    onSuccess: (plan) => {
      queryClient.setQueryData(queryKeys.plan(plan.id), plan);
      invalidateAll(queryClient, [queryKeys.plans]);
    },
  });
}

export type PlanActionRequest =
  | { action: 'validate' }
  | { action: 'start' }
  | { action: 'pause' }
  | { action: 'waves'; body?: AutoWavesRequest };

export function usePlanAction(planId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: PlanActionRequest): Promise<Plan | ValidationReport> => {
      const base = `/plans/${enc(planId)}`;
      switch (request.action) {
        case 'validate':
          return api.post<ValidationReport>(`${base}/validate`);
        case 'start':
          return api.post<Plan>(`${base}/start`);
        case 'pause':
          return api.post<Plan>(`${base}/pause`);
        case 'waves':
          return api.post<Plan>(`${base}/waves/auto`, request.body ?? {});
      }
    },
    onSuccess: (result) => {
      if ('waves' in result) queryClient.setQueryData(queryKeys.plan(planId), result);
      invalidateAll(queryClient, [queryKeys.plans, ['migrations'], ['stats'], ['events']]);
    },
  });
}

export type MigrationActionRequest =
  | { action: 'approve'; body?: ApproveRequest }
  | { action: 'cutover'; body?: CutoverRequest }
  | { action: 'sync' }
  | { action: 'rollback'; body: RollbackRequest }
  | { action: 'retry' }
  | { action: 'cancel'; body?: CancelRequest }
  | { action: 'finalize'; body: FinalizeRequest };

export function useMigrationAction(migrationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: MigrationActionRequest) => {
      const action: MigrationAction = request.action;
      const body = 'body' in request ? (request.body ?? {}) : undefined;
      return api.post<Migration>(`/migrations/${enc(migrationId)}/${action}`, body);
    },
    onSuccess: (migration) => {
      queryClient.setQueryData(queryKeys.migration(migration.id), migration);
      invalidateAll(queryClient, [['migrations'], ['stats'], queryKeys.plan(migration.plan_id), ['events']]);
    },
  });
}

export function useSetStrategy(migrationId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (strategy: Strategy) => api.put<Migration>(`/migrations/${enc(migrationId)}/strategy`, { strategy }),
    onSuccess: (migration) => {
      queryClient.setQueryData(queryKeys.migration(migration.id), migration);
      invalidateAll(queryClient, [['migrations']]);
    },
  });
}

export function useSimilarIncidents() {
  const api = useApi();
  return useMutation({
    mutationFn: (body: SimilarIncidentsRequest) =>
      api.post<SimilarIncidentsResponse>('/advisor/similar-incidents', body),
  });
}
