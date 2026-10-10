import { validateConfig, type Config } from './config.js';
import { isOperationName, operations, type OperationName, type RequestSpec } from './operations.js';

export class MarketplaceError extends Error {
  constructor(public readonly code: string, message: string, public readonly status?: number) {
    super(message); this.name = 'MarketplaceError';
  }
}
export function errorResult(error: unknown) {
  if (error instanceof MarketplaceError) return { error: { code: error.code, message: error.message, ...(error.status === undefined ? {} : { status: error.status }) } };
  return { error: { code: 'INVALID_INPUT', message: 'Invalid arguments or configuration; consult the command schema and environment settings.' } };
}
export class MarketplaceClient {
  readonly #config: Config;
  constructor(config: Config) { this.#config = validateConfig(config); }

  async call(name: OperationName, input: unknown): Promise<unknown> {
    if (!isOperationName(name)) throw new MarketplaceError('UNKNOWN_OPERATION', 'Unknown marketplace operation');
    return this.#request(operations[name].request(input));
  }
  async health(): Promise<unknown> { return this.#request({ method: 'GET', path: '/health' }); }

  async #request(spec: RequestSpec): Promise<unknown> {
    const url = new URL(spec.path, this.#config.apiUrl);
    if (url.origin !== this.#config.apiUrl) throw new MarketplaceError('INVALID_DESTINATION', 'Request destination must be the configured backend');
    const headers: Record<string, string> = { Authorization: `Bearer ${this.#config.apiToken}`, Accept: 'application/json' };
    if (spec.body !== undefined) headers['Content-Type'] = 'application/json';
    if (spec.idempotencyKey) headers['Idempotency-Key'] = spec.idempotencyKey;
    let response: Response;
    let raw: string;
    try {
      response = await fetch(url, {
        method: spec.method, headers, redirect: 'manual', signal: AbortSignal.timeout(30_000),
        ...(spec.body === undefined ? {} : { body: JSON.stringify(spec.body) })
      });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        throw new MarketplaceError('REDIRECT_BLOCKED', 'Backend redirects are not allowed', response.status);
      }
      raw = await response.text();
    } catch (error) {
      if (error instanceof MarketplaceError) throw error;
      throw new MarketplaceError('TRANSPORT_ERROR', 'Backend request failed or timed out. Outcome may be unknown; retry order commits only with the same idempotency key and payload.');
    }
    let data: unknown;
    try { data = JSON.parse(raw); }
    catch { throw new MarketplaceError('INVALID_RESPONSE', 'Backend returned invalid JSON', response.status); }
    if (!response.ok) {
      const envelope = data as { error?: { code?: unknown; message?: unknown } } | null;
      const redact = (value: string) => value.split(this.#config.apiToken).join('[REDACTED]');
      throw new MarketplaceError(
        typeof envelope?.error?.code === 'string' ? redact(envelope.error.code) : 'HTTP_ERROR',
        typeof envelope?.error?.message === 'string' ? redact(envelope.error.message) : 'Backend request rejected', response.status
      );
    }
    return data;
  }
}
