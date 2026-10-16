import { QueryClient, type DefaultOptions } from '@tanstack/react-query';
import { shareStructure } from './structuralSharing';

/** The dashboard's query client, for the app and its tests alike: its cache keeps the API's data as sent, a tenant's
 * "__proto__" key included (review RV24-02); `defaults` are the caller's own options. */
export function createQueryClient(defaults: DefaultOptions = {}): QueryClient {
  return new QueryClient({ defaultOptions: { ...defaults, queries: { ...defaults.queries, structuralSharing: shareStructure } } });
}
