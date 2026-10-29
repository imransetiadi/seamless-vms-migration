const RETRY = '__seamless_retry';

export function reloadPage(): void {
  const url = new URL(window.location.href);
  url.searchParams.set(RETRY, crypto.randomUUID());
  window.location.assign(url.href);
}

/** WebKit caches a failed module across reloads; only explicit recovery uses a fresh emitted asset URL. */
export async function routeChunk<T>(name: string, load: () => Promise<T>): Promise<T> {
  const retry = new URL(window.location.href).searchParams.get(RETRY);
  if (!retry || import.meta.env.DEV) return load();
  const response = await fetch(`${import.meta.env.BASE_URL}route-chunks.json`, { cache: 'no-store' });
  if (!response.ok) throw new Error('The page manifest could not be loaded.');
  const routes: unknown = await response.json();
  const asset = routes && typeof routes === 'object' ? (routes as Record<string, unknown>)[name] : null;
  if (typeof asset !== 'string' || !/^assets\/[\w-]+\.js$/.test(asset)) throw new Error('The page asset could not be found.');
  const url = new URL(`${import.meta.env.BASE_URL}${asset}`, window.location.origin);
  if (url.origin !== window.location.origin) throw new Error('The page asset origin is invalid.');
  url.searchParams.set(RETRY, retry);
  return import(/* @vite-ignore */ url.href) as Promise<T>;
}
