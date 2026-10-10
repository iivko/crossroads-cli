import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { MarketplaceClient, errorResult } from './client.js';
import { operations, type OperationName } from './operations.js';

export function createServer(client: MarketplaceClient): McpServer {
  const server = new McpServer({ name: 'crossroads-marketplace', version: '0.1.0' });
  for (const name of Object.keys(operations) as OperationName[]) {
    const operation = operations[name];
    server.registerTool(name, {
      description: operation.description, inputSchema: operation.schema,
      annotations: { readOnlyHint: operation.readOnly, destructiveHint: !operation.readOnly,
        idempotentHint: operation.readOnly || name === 'commit_order', openWorldHint: true }
    }, async (input: unknown) => {
      try {
        const result = await client.call(name, input);
        return { content: [{ type: 'text' as const, text: JSON.stringify(result) }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text' as const, text: JSON.stringify(errorResult(error)) }] };
      }
    });
  }
  return server;
}
