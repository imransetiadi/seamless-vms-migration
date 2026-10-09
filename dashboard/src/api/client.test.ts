import { describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiError } from './client';

type FetchMock = ReturnType<typeof vi.fn<typeof fetch>>;

function jsonResponse(status: number, body: unknown, statusText = ''): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeClient(fetchImpl: FetchMock, token: string | null = 'smg_secret', onUnauthorized = vi.fn()) {
  const client = new ApiClient({
    baseUrl: '/api/v1',
    getToken: () => token,
    onUnauthorized,
    fetchImpl,
  });
  return { client, onUnauthorized };
}

function lastRequest(fetchImpl: FetchMock): { url: string; init: RequestInit; headers: Headers } {
  const call = fetchImpl.mock.calls.at(-1);
  if (!call) throw new Error('fetch was not called');
  const [url, init = {}] = call;
  return { url: String(url), init, headers: new Headers(init.headers) };
}

describe('ApiClient', () => {
  it('sends the bearer token and parses JSON', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(200, { name: 'rina', role: 'operator' }));
    const { client } = makeClient(fetchImpl);

    await expect(client.get('/me')).resolves.toEqual({ name: 'rina', role: 'operator' });

    const { url, headers } = lastRequest(fetchImpl);
    expect(url).toBe('/api/v1/me');
    expect(headers.get('Authorization')).toBe('Bearer smg_secret');
    expect(headers.get('Accept')).toBe('application/json');
  });

  it('omits the Authorization header when there is no token', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(200, { status: 'ok' }));
    const { client } = makeClient(fetchImpl, null);

    await client.get('/health');

    expect(lastRequest(fetchImpl).headers.has('Authorization')).toBe(false);
  });

  it('serialises JSON bodies and drops empty query parameters', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(200, []));
    const { client } = makeClient(fetchImpl);

    await client.get('/migrations', { query: { plan_id: 'plan-0a1b2c3d', phase: undefined, wave_id: null } });
    expect(lastRequest(fetchImpl).url).toBe('/api/v1/migrations?plan_id=plan-0a1b2c3d');

    fetchImpl.mockResolvedValue(jsonResponse(200, { id: 'mig-0123456789' }));
    await client.post('/migrations/mig-0123456789/rollback', { reason: 'port 22 closed' });
    const { init, headers } = lastRequest(fetchImpl);
    expect(init.method).toBe('POST');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ reason: 'port 22 closed' }));
  });

  it('maps the SDD §12 error envelope to ApiError', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(409, { error: { code: 'conflict', message: 'cannot cutover from syncing' } }),
    );
    const { client, onUnauthorized } = makeClient(fetchImpl);

    const error = await client.post('/migrations/mig-0123456789/cutover', {}).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: 'conflict', message: 'cannot cutover from syncing' });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('calls onUnauthorized on 401 and still rejects', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(401, { error: { code: 'unauthorized', message: 'invalid token' } }),
    );
    const { client, onUnauthorized } = makeClient(fetchImpl);

    await expect(client.get('/plans')).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(onUnauthorized.mock.calls[0]?.[0]).toBeInstanceOf(ApiError);
  });

  it('does not treat 403 as a session problem', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(403, { error: { code: 'forbidden', message: 'approver role required' } }),
    );
    const { client, onUnauthorized } = makeClient(fetchImpl);

    await expect(client.post('/migrations/mig-0123456789/approve', {})).rejects.toMatchObject({
      status: 403,
      code: 'forbidden',
      message: 'approver role required',
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('falls back to an http_<status> code for non-envelope errors', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502, statusText: 'Bad Gateway' }));
    const { client } = makeClient(fetchImpl);

    await expect(client.get('/stats')).rejects.toMatchObject({ status: 502, code: 'http_502', message: 'Bad Gateway' });
  });

  it('accepts FastAPI-style {detail} bodies', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(422, { detail: 'reason is required' }));
    const { client } = makeClient(fetchImpl);

    await expect(client.post('/migrations/mig-0123456789/rollback', {})).rejects.toMatchObject({
      status: 422,
      message: 'reason is required',
    });
  });

  it('resolves 204 responses to undefined', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    const { client } = makeClient(fetchImpl);

    await expect(client.delete('/providers/rhosp17-dc1')).resolves.toBeUndefined();
    expect(lastRequest(fetchImpl).init.method).toBe('DELETE');
  });

  it('wraps network failures as ApiError status 0', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'));
    const { client } = makeClient(fetchImpl);

    await expect(client.get('/plans')).rejects.toMatchObject({ status: 0, code: 'network_error' });
  });
});
