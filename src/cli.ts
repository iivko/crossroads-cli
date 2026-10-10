#!/usr/bin/env node
import { zodToJsonSchema } from 'zod-to-json-schema';
import { MarketplaceClient, errorResult } from './client.js';
import { loadConfig } from './config.js';
import { isOperationName, operations } from './operations.js';

const [command, argument, ...extra] = process.argv.slice(2);
try {
  if (!command || command === '--help' || command === 'help') {
    console.log('Usage: marketplace <tool_name> \'<JSON arguments>\'\n       marketplace schema <tool_name>\n       marketplace health\n\nTools:\n' + Object.keys(operations).join('\n'));
  } else if (command === 'schema' && argument && isOperationName(argument) && !extra.length) {
    console.log(JSON.stringify(zodToJsonSchema(operations[argument].schema), null, 2));
  } else {
    if (extra.length || (command !== 'health' && !isOperationName(command)) || (command === 'health' && argument)) throw new Error('Invalid command');
    const client = new MarketplaceClient(loadConfig());
    const result = command === 'health' ? await client.health() : await client.call(command as keyof typeof operations, JSON.parse(argument ?? '{}'));
    console.log(JSON.stringify(result, null, 2));
  }
} catch (error) {
  console.error(JSON.stringify(errorResult(error)));
  process.exitCode = 1;
}
