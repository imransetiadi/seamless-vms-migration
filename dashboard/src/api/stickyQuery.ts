import { useQuery, type DefaultError, type QueryKey, type UseQueryOptions, type UseQueryResult } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

/**
 * `useQuery` that keeps a failed load said while it is fetched again (SDD §16 Failed loads). TanStack Query 5 puts a
 * query without data back to `pending` with no error on every refetch — Retry, a live event, the window regaining
 * focus — so the page's message and its Retry button turned into a loading placeholder under the operator's focus.
 * While such a refetch runs, the last error of the same query key is reported instead, until an answer replaces it.
 */
export function useApiQuery<TQueryFnData = unknown, TError = DefaultError, TData = TQueryFnData, TQueryKey extends QueryKey = QueryKey>(
  options: UseQueryOptions<TQueryFnData, TError, TData, TQueryKey>,
): UseQueryResult<TData, TError> {
  const result = useQuery(options);
  const key = JSON.stringify(options.queryKey);
  const [last, setLast] = useState<{ key: string; error: TError } | null>(null);
  const { error, isSuccess } = result;
  useEffect(() => {
    if (error) setLast({ key, error });
    else if (isSuccess) setLast(null);
  }, [key, error, isSuccess]);
  if (result.isPending && result.isFetching && last && last.key === key) {
    return {
      ...result,
      error: last.error,
      isError: true,
      isPending: false,
      isLoading: false,
      isLoadingError: true,
      status: 'error',
    } as unknown as UseQueryResult<TData, TError>;
  }
  return result;
}
