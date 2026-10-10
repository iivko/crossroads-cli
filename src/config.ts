export interface Config { apiUrl: string; apiToken: string }

export function validateConfig(config: Config): Config {
  let url: URL;
  try { url = new URL(config.apiUrl); }
  catch { throw new Error('MARKETPLACE_API_URL must be a valid backend origin'); }
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (!(url.protocol === 'https:' || (url.protocol === 'http:' && local)) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('MARKETPLACE_API_URL must be an HTTPS origin or localhost HTTP origin, without credentials, path, query, or fragment');
  }
  if (!config.apiToken || !/^[A-Za-z0-9\-._~+/]+=*$/.test(config.apiToken)) {
    throw new Error('MARKETPLACE_API_TOKEN is required and must be a valid bearer token');
  }
  return { apiUrl: url.origin, apiToken: config.apiToken };
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return validateConfig({ apiUrl: env.MARKETPLACE_API_URL ?? 'http://localhost:4000', apiToken: env.MARKETPLACE_API_TOKEN ?? '' });
}
