import type { ReactNode } from 'react';
import type { ApiClient } from './client';
import { ApiContext } from './hooks';

export function ApiProvider({ client, children }: { client: ApiClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}
