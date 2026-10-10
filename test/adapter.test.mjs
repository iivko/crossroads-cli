import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { MarketplaceClient } from '../dist/client.js';
import { loadConfig, validateConfig } from '../dist/config.js';

async function backend(t, handler) {
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const request = { method: req.method, url: req.url, headers: req.headers, body: raw ? JSON.parse(raw) : undefined };
    requests.push(request);
    if (handler) handler(request, res);
    else { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ id: 'result' })); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const apiUrl = `http://127.0.0.1:${server.address().port}`;
  return { apiUrl, requests, client: new MarketplaceClient({ apiUrl, apiToken: 'test-token' }) };
}
const cases = [
  ['search_listings', { q: 'a & b' }, 'GET', '/v1/listings?q=a%20%26%20b'],
  ['get_listing', { listingId: 'a/b?#' }, 'GET', '/v1/listings/a%2Fb%3F%23'],
  ['get_seller_verification', { sellerId: 's1' }, 'GET', '/v1/sellers/s1/verification'],
  ['create_rfq', { listingId: 'l1', title: 'Cleanup', description: 'Normalize', budgetCents: 50000, deadline: '2026-12-01T00:00:00Z', acceptanceCriteria: 'Valid CSV' }, 'POST', '/v1/rfqs'],
  ['list_quotes', { rfqId: 'r & 1' }, 'GET', '/v1/quotes?rfqId=r%20%26%201'],
  ['submit_quote', { rfqId: 'r1', scope: 'Normalize', totalCents: 40000, deliveryDays: 2, expiresAt: '2026-11-01T00:00:00Z' }, 'POST', '/v1/quotes'],
  ['preview_order', { quoteId: 'q1' }, 'POST', '/v1/order-previews'],
  ['request_approval', { previewId: 'p1' }, 'POST', '/v1/approvals'],
  ['commit_order', { previewId: 'p1', approvalId: 'a1', idempotencyKey: 'stable-key' }, 'POST', '/v1/orders'],
  ['get_order', { orderId: 'o1' }, 'GET', '/v1/orders/o1'],
  ['submit_delivery', { orderId: 'o1', url: 'https://files.example/result.csv', notes: 'Ready' }, 'POST', '/v1/orders/o1/delivery'],
  ['open_dispute', { orderId: 'o1', reason: 'Missing rows' }, 'POST', '/v1/orders/o1/disputes']
];
test('all operations map to REST, preserve JSON and set bearer headers', async t => {
  const { client, requests } = await backend(t);
  for (const [name, args, method, path] of cases) {
    assert.deepEqual(await client.call(name, args), { id: 'result' });
    const request = requests.at(-1);
    assert.equal(request.method, method); assert.equal(request.url, path);
    assert.equal(request.headers.authorization, 'Bearer test-token');
    assert.equal(request.headers.accept, 'application/json');
    if (method === 'POST') {
      const { orderId, idempotencyKey, ...body } = args;
      assert.deepEqual(request.body, body);
      assert.equal(request.headers['content-type'], 'application/json');
    } else assert.equal(request.body, undefined);
  }
  await client.health(); assert.equal(requests.at(-1).url, '/health');
});
test('order retries preserve idempotency and never auto retry', async t => {
  const { client, requests } = await backend(t, (_, res) => { res.statusCode = 503; res.end(JSON.stringify({ error: { code: 'UNAVAILABLE', message: 'Try later' } })); });
  const args = cases[8][1];
  for (let i = 0; i < 2; i++) await assert.rejects(client.call('commit_order', args), { code: 'UNAVAILABLE', status: 503 });
  assert.equal(requests.length, 2);
  for (const req of requests) {
    assert.equal(req.headers['idempotency-key'], 'stable-key');
    assert.deepEqual(req.body, { previewId: 'p1', approvalId: 'a1' });
  }
});
test('validation rejects extra authority fields, missing keys, fractional cents and dot IDs before HTTP', async t => {
  const { client, requests } = await backend(t);
  for (const [name, input] of [
    ['request_approval', { previewId: 'p1', approved: true }],
    ['commit_order', { previewId: 'p1', approvalId: 'a1' }],
    ['commit_order', { ...cases[8][1], idempotencyKey: 'bad\r\nheader' }],
    ['submit_quote', { ...cases[5][1], totalCents: 1.5 }],
    ['get_listing', { listingId: '..' }], ['approve', {}], ['accept_order', {}]
  ]) await assert.rejects(client.call(name, input));
  assert.equal(requests.length, 0);
});
test('config fails closed and accepts only HTTPS or loopback HTTP origins', () => {
  assert.throws(() => loadConfig({}));
  assert.throws(() => loadConfig({ MARKETPLACE_API_TOKEN: ' ' }));
  assert.equal(loadConfig({ MARKETPLACE_API_TOKEN: 'demo-agent-token' }).apiUrl, 'http://localhost:4000');
  for (const apiUrl of ['http://example.com', 'http://localhost.evil', 'https://a.example/path', 'https://u:p@a.example', 'https://a.example/?q=1', 'https://a.example/#x', 'file:///tmp/a', 'invalid'])
    assert.throws(() => validateConfig({ apiUrl, apiToken: 'secret' }));
  for (const apiUrl of ['http://localhost:4000', 'http://127.0.0.1:4000', 'http://[::1]:4000', 'https://api.example'])
    assert.equal(validateConfig({ apiUrl, apiToken: 'secret' }).apiUrl, apiUrl);
});
test('redirects never forward token, including same-origin redirects', async t => {
  const target = await backend(t);
  let destination = target.apiUrl + '/stolen';
  const source = await backend(t, (_, res) => { res.writeHead(307, { Location: destination }); res.end(); });
  await assert.rejects(source.client.call('search_listings', {}), { code: 'REDIRECT_BLOCKED' });
  destination = source.apiUrl + '/redirected';
  await assert.rejects(source.client.call('search_listings', {}), { code: 'REDIRECT_BLOCKED' });
  assert.equal(target.requests.length, 0); assert.equal(source.requests.length, 2);
});
test('backend errors preserve code/status, redact tokens, malformed JSON is safe', async t => {
  let response = JSON.stringify({ error: { code: 'FORBIDDEN', message: 'Denied test-token' } });
  const { client } = await backend(t, (_, res) => { res.statusCode = 403; res.end(response); });
  await assert.rejects(client.call('get_order', { orderId: 'o1' }), { code: 'FORBIDDEN', status: 403, message: 'Denied [REDACTED]' });
  response = '<html>test-token</html>';
  await assert.rejects(client.health(), { code: 'INVALID_RESPONSE', status: 403 });
  response = 'null';
  await assert.rejects(client.health(), { code: 'HTTP_ERROR' });
});
test('unwrapped arrays and transport failures', async t => {
  const { client } = await backend(t, (_, res) => res.end('[{"id":"l1"}]'));
  assert.deepEqual(await client.call('search_listings', {}), [{ id: 'l1' }]);
  const broken = await backend(t, (_, res) => res.destroy());
  await assert.rejects(broken.client.health(), { code: 'TRANSPORT_ERROR' });
});
test('real SDK stdio discovery, calls, seller identity and policy errors', { timeout: 15000 }, async t => {
  const { apiUrl, requests } = await backend(t, (req, res) => {
    if (req.url === '/v1/orders' || req.url === '/v1/approvals') {
      res.statusCode = 403; res.end(JSON.stringify({ error: { code: 'APPROVAL_REQUIRED', message: 'Human approval required' } }));
    } else res.end(JSON.stringify(req.method === 'GET' ? [{ id: 'l1' }] : { id: 'q1' }));
  });
  const transport = new StdioClientTransport({ command: process.execPath, args: ['dist/stdio.js'],
    env: { ...process.env, MARKETPLACE_API_URL: apiUrl, MARKETPLACE_API_TOKEN: 'demo-seller-agent-token' }, stderr: 'pipe' });
  const client = new Client({ name: 'smoke-test', version: '1.0.0' });
  t.after(() => client.close());
  await client.connect(transport);
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map(tool => tool.name).sort(), cases.map(c => c[0]).sort());
  const result = await client.callTool({ name: 'search_listings', arguments: {} });
  assert.deepEqual(JSON.parse(result.content[0].text), [{ id: 'l1' }]);
  await client.callTool({ name: 'submit_quote', arguments: cases[5][1] });
  assert.equal(requests.at(-1).headers.authorization, 'Bearer demo-seller-agent-token');
  const denied = await client.callTool({ name: 'commit_order', arguments: cases[8][1] });
  assert.equal(denied.isError, true);
  assert.equal(JSON.parse(denied.content[0].text).error.code, 'APPROVAL_REQUIRED');
  const count = requests.length;
  const invalid = await client.callTool({ name: 'request_approval', arguments: { previewId: 'p1', approved: true } });
  assert.equal(invalid.isError, true); assert.equal(requests.length, count);
});
function cli(args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { env: { ...process.env, ...env } });
    let stdout = '', stderr = '';
    child.stdout.on('data', data => stdout += data); child.stderr.on('data', data => stderr += data);
    child.on('error', reject); child.on('close', code => resolve({ code, stdout, stderr }));
  });
}
test('CLI uses same HTTP contract and fails missing credentials; stdio stdout stays clean', async t => {
  const { apiUrl, requests } = await backend(t);
  const env = { MARKETPLACE_API_URL: apiUrl, MARKETPLACE_API_TOKEN: 'demo-agent-token' };
  const result = await cli(['dist/cli.js', 'commit_order', JSON.stringify(cases[8][1])], env);
  assert.equal(result.code, 0); assert.deepEqual(JSON.parse(result.stdout), { id: 'result' });
  assert.equal(requests[0].headers['idempotency-key'], 'stable-key');
  for (const entry of ['dist/cli.js', 'dist/stdio.js']) {
    const failed = await cli([entry, ...(entry.includes('cli') ? ['health'] : [])], { ...env, MARKETPLACE_API_TOKEN: '' });
    assert.equal(failed.code, 1); assert.equal(failed.stdout, ''); assert.ok(failed.stderr);
  }
  const schema = await cli(['dist/cli.js', 'schema', 'commit_order'], env);
  assert.equal(schema.code, 0); assert.ok(JSON.parse(schema.stdout).required.includes('idempotencyKey'));
});

test('backend contract rejects invalid bounds and types before sending HTTP', async t => {
  const { client, requests } = await backend(t);
  const rfq = cases[3][1], quote = cases[5][1], delivery = cases[10][1];
  const invalid = [];
  const add = (name, base, field, values) => {
    for (const value of values) invalid.push([name, { ...base, [field]: value }]);
  };
  add('create_rfq', rfq, 'acceptanceCriteria', [['Valid CSV'], '', '   ', 'x'.repeat(8001)]);
  add('create_rfq', rfq, 'title', ['', ' \n ', 'x'.repeat(201)]);
  add('create_rfq', rfq, 'description', ['', '   ', 'x'.repeat(8001)]);
  add('submit_quote', quote, 'scope', ['', '   ', 'x'.repeat(8001)]);
  for (const [name, base, field] of [['create_rfq', rfq, 'budgetCents'], ['submit_quote', quote, 'totalCents']])
    add(name, base, field, [0, -1, 0.5, 100_000_001]);
  for (const [name, base, field] of [['create_rfq', rfq, 'deadline'], ['submit_quote', quote, 'expiresAt']])
    add(name, base, field, ['2026-12-01', '2026-12-01T00:00:00', 'not-a-date', '2026-02-30T00:00:00Z']);
  add('submit_quote', quote, 'deliveryDays', [0, -1, 1.5, 91]);
  for (const [name, base] of cases) {
    for (const field of Object.keys(base).filter(key => key.endsWith('Id')))
      add(name, base, field, ['', 'x'.repeat(151)]);
  }
  add('commit_order', cases[8][1], 'idempotencyKey', ['x'.repeat(7), 'x'.repeat(151)]);
  add('submit_delivery', delivery, 'url', ['http://files.example/a', 'ftp://files.example/a', 'https://files.example/' + 'x'.repeat(2048)]);
  add('submit_delivery', delivery, 'notes', ['', '   ', 'x'.repeat(8001)]);
  add('open_dispute', cases[11][1], 'reason', ['', '   ', 'x'.repeat(8001)]);
  for (const [name, input] of invalid) await assert.rejects(client.call(name, input), undefined, `${name} must reject invalid input`);
  assert.equal(requests.length, 0);
});

test('backend contract accepts inclusive limits, offset dates and trims outgoing text', async t => {
  const { client, requests } = await backend(t);
  for (const [money, days, length] of [[1, 1, 8], [100_000_000, 90, 150]]) {
    await client.call('create_rfq', { ...cases[3][1], listingId: 'i'.repeat(150), title: ' ' + 't'.repeat(200) + ' ',
      description: ' ' + 'd'.repeat(8000) + ' ', budgetCents: money, deadline: '2026-12-01T12:00:00+02:00', acceptanceCriteria: ' Valid CSV ' });
    assert.equal(requests.at(-1).body.title.length, 200);
    assert.equal(requests.at(-1).body.description.length, 8000);
    assert.equal(requests.at(-1).body.acceptanceCriteria, 'Valid CSV');
    await client.call('submit_quote', { ...cases[5][1], scope: ' Normalize ', totalCents: money, deliveryDays: days, expiresAt: '2026-11-01T12:00:00-05:00' });
    assert.equal(requests.at(-1).body.scope, 'Normalize');
    await client.call('commit_order', { ...cases[8][1], idempotencyKey: 'k'.repeat(length) });
    assert.equal(requests.at(-1).headers['idempotency-key'].length, length);
  }
  const url = 'https://files.example/' + 'x'.repeat(2048 - 'https://files.example/'.length);
  await client.call('submit_delivery', { ...cases[10][1], url, notes: ' Ready ' });
  assert.deepEqual(requests.at(-1).body, { url, notes: 'Ready' });
  await client.call('open_dispute', { ...cases[11][1], reason: ' Missing rows ' });
  assert.equal(requests.at(-1).body.reason, 'Missing rows');
});
