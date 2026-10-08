import { useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { clearStoredToken, storeToken } from './auth';
import { queryKeys, useApi, useMe } from './hooks';
import type { Me, Role } from './types';

/** Stores the token, drops cached data from any previous identity and verifies it with GET /me. */
export function useSignIn() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useCallback(
    async (token: string): Promise<Me> => {
      storeToken(token.trim());
      queryClient.removeQueries();
      try {
        return await queryClient.fetchQuery({
          queryKey: queryKeys.me,
          queryFn: () => api.get<Me>('/me'),
          retry: false,
          staleTime: 0,
        });
      } catch (error) {
        clearStoredToken();
        queryClient.removeQueries({ queryKey: queryKeys.me });
        throw error;
      }
    },
    [api, queryClient],
  );
}

export function useSignOut() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useCallback(() => {
    clearStoredToken();
    queryClient.clear();
    navigate('/login', { replace: true });
  }, [navigate, queryClient]);
}

/** The signed-in principal's role (undefined while /me loads). */
export function useRole(): Role | undefined {
  return useMe().data?.role;
}
