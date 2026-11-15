// Driven by tests/integration/test_n8n_node_contract.py, not by `npm test`:
//   node --import tsx test/contract-driver.mjs <mcp-url> <token> <tool> [<json arguments>]
// Runs the node's own transport client against a real PrivacyFence daemon and prints one JSON line.
import { McpTransportError, openSession } from '../nodes/PrivacyFence/transport/mcpClient.ts';
import { toItem } from '../nodes/PrivacyFence/transport/result.ts';

const [url, token, tool, rawArguments = '{}'] = process.argv.slice(2);
const TIMEOUT_MS = 20_000;

let sessionId = '';

/** An `HttpSend` over Node's global fetch, as the node builds it over n8n's helpers. */
async function send(request) {
	const response = await fetch(request.url, {
		method: request.method,
		headers: request.headers,
		body: request.body,
		redirect: 'manual',
		signal: AbortSignal.timeout(request.timeoutMs),
	});
	const headers = {};
	response.headers.forEach((value, name) => {
		headers[name.toLowerCase()] = value;
	});
	if (headers['mcp-session-id']) sessionId = headers['mcp-session-id'];
	return { status: response.status, headers, body: await response.text() };
}

function print(output) {
	process.stdout.write(`${JSON.stringify(output)}\n`);
}

try {
	const session = await openSession(send, { url, token, timeoutMs: TIMEOUT_MS, clientVersion: '0.0.0-contract' });
	const tools = (await session.listTools()).map((t) => t.name);
	const result = toItem(await session.callTool(tool, JSON.parse(rawArguments)));
	await session.close();
	// The closed session's id is no longer valid: a further request on it must be refused.
	const probe = await send({
		method: 'POST',
		url,
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json',
			Accept: 'application/json, text/event-stream',
			'Mcp-Session-Id': sessionId,
		},
		body: JSON.stringify({ jsonrpc: '2.0', id: 99, method: 'tools/list', params: {} }),
		timeoutMs: TIMEOUT_MS,
	});
	print({ tools, result, closedSessionRefused: probe.status < 200 || probe.status >= 300 });
} catch (error) {
	if (!(error instanceof McpTransportError)) throw error;
	print({ error: error.message });
}
