#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { MarketplaceClient } from './client.js';
import { loadConfig } from './config.js';
import { createServer } from './server.js';

try {
  const server = createServer(new MarketplaceClient(loadConfig()));
  await server.connect(new StdioServerTransport());
} catch {
  console.error('Marketplace MCP failed to start. Check MARKETPLACE_API_URL and MARKETPLACE_API_TOKEN.');
  process.exitCode = 1;
}
