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
  AdvisorPolicy,
  AdvisorPolicySpec,
  AdvisorRecommendation,
  AdvisorSnapshot,
  AdvisorStatus,
  DataSentList,
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
  PolicyList,
  OverrideRequest,
  Provider,
  ProviderCreate,
  ProviderCredentials,
  ProviderPatch,
  ProviderInventory,
  RecommendationListQuery,
  RecommendationPage,
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
  eventTail: (query: Omit<EventListQuery, 'since' | 'limit' | 'tail'> = {}) => ['events', 'tail', query] as const,
  stats: (planId?: string | null) => ['stats', planId ?? null] as const,
  advisorStatus: ['advisor', 'status'] as const,
  dataSent: ['advisor', 'data-sent'] as const,
  recommendations: (query: RecommendationListQuery = {}) => ['advisor', 'recommendations', query] as const,
  /** Under `recommendations`, so a refresh of the list refreshes it too. */
  recommendation: (id: string) => ['advisor', 'recommendations', 'id', id] as const,
  snapshot: (sha256: string) => ['advisor', 'snapshot', sha256] as const,
  policies: ['advisor', 'policies'] as const,
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

/** `GET /advisor/data-sent`: what each decision type sends to Jev (SDD §13.4, ADV-06). */
export function useDataSent() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.dataSent,
    queryFn: ({ signal }) => api.get<DataSentList>('/advisor/data-sent', { signal }),
  });
}

/** `GET /advisor/recommendations`: stored and legacy recommendations, newest first (SDD §12, §14.4). */
export function useRecommendations(query: RecommendationListQuery = {}, options: ListOptions = {}) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.recommendations(query),
    queryFn: ({ signal }) =>
      api.get<RecommendationPage>('/advisor/recommendations', {
        signal,
        query: {
          migration_id: query.migration_id,
          plan_id: query.plan_id,
          decision_type: query.decision_type,
          limit: query.limit,
          offset: query.offset,
        },
      }),
    enabled: options.enabled ?? true,
    placeholderData: options.keepPrevious ? keepPreviousData : undefined,
    retry: noRetryOnClientErrors,
  });
}

/** `GET /advisor/recommendations/{id}`: one recommendation, a legacy one too (SDD §12). */
export function useRecommendation(recId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.recommendation(recId ?? ''),
    queryFn: ({ signal }) => api.get<AdvisorRecommendation>(`/advisor/recommendations/${enc(recId ?? '')}`, { signal }),
    enabled: Boolean(recId),
    retry: noRetryOnClientErrors,
  });
}

/** `GET /advisor/policies`: the stored policies and the built-in default (SDD §12, §14.6). */
export function usePolicies() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.policies,
    queryFn: ({ signal }) => api.get<PolicyList>('/advisor/policies', { signal }),
    retry: noRetryOnClientErrors,
  });
}

/** `GET /advisor/snapshots/{sha256}`: a redacted advisor input; it never changes (SDD §14.5). */
export function useSnapshot(sha256: string | null | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.snapshot(sha256 ?? ''),
    queryFn: ({ signal }) => api.get<AdvisorSnapshot>(`/advisor/snapshots/${enc(sha256 ?? '')}`, { signal }),
    enabled: Boolean(sha256),
    staleTime: Infinity,
    retry: noRetryOnClientErrors,
  });
}

/** `GET /events` returns at most 1000 events per request (SDD §12). */
const EVENT_LIMIT = 1000;

/**
 * The newest `keep` (≤ 1000) persisted events, ascending: one `GET /events?tail=true` request,
 * however many events are persisted (SDD §12).
 */
export async function fetchEventTail(
  api: ApiClient,
  query: Omit<EventListQuery, 'since' | 'limit' | 'tail'> = {},
  keep = 500,
  signal?: AbortSignal,
): Promise<Event[]> {
  return api.get<Event[]>('/events', {
    signal,
    query: { tail: true, limit: Math.min(keep, EVENT_LIMIT), plan_id: query.plan_id, migration_id: query.migration_id, audit: query.audit || undefined },
  });
}

export function useEventTail(query: Omit<EventListQuery, 'since' | 'limit' | 'tail'> = {}, keep = 500) {
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

function useProviderWrite<V>(request: (api: ReturnType<typeof useApi>, vars: V) => Promise<Provider>) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: V) => request(api, vars),
    onSuccess: (provider) => {
      queryClient.setQueryData<Provider[]>(queryKeys.providers, (old) =>
        old ? (old.some((p) => p.id === provider.id) ? old.map((p) => (p.id === provider.id ? provider : p)) : [...old, provider]) : old,
      );
      invalidateAll(queryClient, [queryKeys.providers, queryKeys.inventory(provider.id)]);
    },
  });
}

/** POST /providers (admin). */
export function useCreateProvider() {
  return useProviderWrite((api, body: ProviderCreate) => api.post<Provider>('/providers', body));
}

/** PATCH /providers/{id} (admin). */
export function useUpdateProvider() {
  return useProviderWrite((api, { id, patch }: { id: string; patch: ProviderPatch }) => api.patch<Provider>(`/providers/${enc(id)}`, patch));
}

/** PUT /providers/{id}/credentials (admin, write-only). */
export function useSetProviderCredentials() {
  return useProviderWrite((api, { id, credentials }: { id: string; credentials: ProviderCredentials }) =>
    api.put<Provider>(`/providers/${enc(id)}/credentials`, credentials),
  );
}

/** PUT /providers/{id}/conversion-key (admin, write-only). */
export function useSetConversionKey() {
  return useProviderWrite((api, { id, privateKey }: { id: string; privateKey: string }) =>
    api.put<Provider>(`/providers/${enc(id)}/conversion-key`, { private_key: privateKey }),
  );
}

/** The outcome of Check all: the providers checked, and every check request that failed. */
export interface CheckAllResult {
  checked: Provider[];
  /** Requests that failed (5xx, 401/403, 404, 429, network): the provider's own status is unchanged. */
  failed: { id: string; error: unknown }[];
}

/** POST /providers/{id}/check for every provider (operator), one after another. */
export function useCheckAllProviders() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: string[]): Promise<CheckAllResult> => {
      const result: CheckAllResult = { checked: [], failed: [] };
      for (const id of ids) {
        try {
          result.checked.push(await api.post<Provider>(`/providers/${enc(id)}/check`));
        } catch (error) {
          // one failed request must not stop the others, and is reported, never dropped (SDD §16)
          result.failed.push({ id, error });
        }
      }
      return result;
    },
    onSettled: () => invalidateAll(queryClient, [queryKeys.providers]),
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
    onSuccess: (result, request) => {
      if ('waves' in result) queryClient.setQueryData(queryKeys.plan(planId), result);
      invalidateAll(queryClient, [queryKeys.plans, ['migrations'], ['stats'], ['events']]);
      // a validation records a recommendation for every automatic choice it hands the advisor (SDD §14.4)
      if (request.action === 'validate') invalidateAll(queryClient, [['advisor', 'recommendations']]);
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

/** POST or PUT `/advisor/policies` (`advisor.policy.manage`, SDD §12, §14.6). */
export function useSavePolicy() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, spec }: { id?: string; spec: AdvisorPolicySpec }) =>
      id ? api.put<AdvisorPolicy>(`/advisor/policies/${enc(id)}`, spec) : api.post<AdvisorPolicy>('/advisor/policies', spec),
    onSuccess: () => invalidateAll(queryClient, [queryKeys.policies, ['events', 'tail']]),
  });
}

/** DELETE `/advisor/policies/{id}` (`advisor.policy.manage`). */
export function useDeletePolicy() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/advisor/policies/${enc(id)}`),
    onSuccess: () => invalidateAll(queryClient, [queryKeys.policies, ['events', 'tail']]),
  });
}

export type RecommendationDecisionRequest = { action: 'approve' } | { action: 'override'; body: OverrideRequest };

/** Approve or override an advisor decision (`advisor.approve`, SDD §14.6). */
export function useDecideRecommendation(recId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: RecommendationDecisionRequest) =>
      api.post<AdvisorRecommendation>(`/advisor/recommendations/${enc(recId)}/${request.action}`, request.action === 'override' ? request.body : {}),
    // the decision changes the migration's strategy and hold, an override the plan's overrides too
    onSuccess: () => invalidateAll(queryClient, [['advisor', 'recommendations'], ['migrations'], ['migration'], ['plans'], ['events', 'tail']]),
  });
}

export function useSimilarIncidents() {
  const api = useApi();
  return useMutation({
    mutationFn: (body: SimilarIncidentsRequest) =>
      api.post<SimilarIncidentsResponse>('/advisor/similar-incidents', body),
  });
}
