import { useQuery, type DefaultError, type QueryKey, type UseQueryOptions, type UseQueryResult } from '@tanstack/react-query';
import { useState } from 'react';

/**
 * `useQuery` that keeps a failed load said while it is fetched again (SDD §16 Failed loads). TanStack Query 5 puts a
 * query without data back to `pending` with no error on every refetch — Retry, a live event, the window regaining
 * focus — so the page's message and its Retry button turned into a loading placeholder under the operator's focus.
 * While such a refetch runs — or waits offline — the last error of the same query key is reported instead, until an
 * answer replaces it; a query with `keepPreviousData` never shows another key's figures for the key that failed.
 */
export function useApiQuery<TQueryFnData = unknown, TError = DefaultError, TData = TQueryFnData, TQueryKey extends QueryKey = QueryKey>(
  options: UseQueryOptions<TQueryFnData, TError, TData, TQueryKey>,
): UseQueryResult<TData, TError> {
  const result = useQuery(options);
  const key = JSON.stringify(options.queryKey);
  const [last, setLast] = useState<{ key: string; error: TError } | null>(null);
  const { error, isSuccess, isPlaceholderData } = result;
  // only a real answer clears it: a placeholder (another key's data) counts as 'success' too
  const answered = isSuccess && !isPlaceholderData;
  // followed as the query renders, not by an effect a render later (react.dev, "Adjusting some state when a prop changes")
  if (error && (last === null || last.key !== key || last.error !== error)) setLast({ key, error });
  else if (!error && answered && last !== null) setLast(null);
  // fetched again (or waiting offline to be) with no answer yet: a pending query, or one showing another key's
  // figures as placeholder (keepPreviousData), reports the failure instead - never those figures for this key
  const refetching = result.isFetching || result.isPaused;
  if (refetching && result.error === null && (result.isPending || result.isPlaceholderData) && last && last.key === key) {
    return {
      ...result,
      data: undefined,
      error: last.error,
      isError: true,
      isPending: false,
      isLoading: false,
      isLoadingError: true,
      isPlaceholderData: false,
      isSuccess: false,
      status: 'error',
    } as unknown as UseQueryResult<TData, TError>;
  }
  return result;
}
