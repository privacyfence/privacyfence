import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	type HttpRequest,
	type HttpResponse,
	type HttpSend,
	McpTransportError,
	openSession,
} from '../nodes/PrivacyFence/transport/mcpClient.ts';

const TOKEN = 'pf_secret_TOKEN_value_123';
const URL_ = 'https://pf.example.org:8443/private/path/mcp';
const HOST = 'pf.example.org:8443';
const opts = { url: URL_, token: TOKEN, timeoutMs: 5000, clientVersion: '1.2.3' };

const JSON_H = { 'content-type': 'application/json' };
const json = (body: unknown, headers: Record<string, string> = {}): HttpResponse => ({
	status: 200,
	headers: { ...JSON_H, ...headers },
	body: JSON.stringify(body),
});
const rpcResult = (id: number, result: unknown) => ({ jsonrpc: '2.0', id, result });

interface Rig {
	send: HttpSend;
	calls: HttpRequest[];
}

/** Fake server: `routes` maps a JSON-RPC method to a response builder. */
function rig(
	overrides: Partial<Record<string, (id: number, req: HttpRequest) => HttpResponse>> = {},
): Rig {
	const calls: HttpRequest[] = [];
	const send: HttpSend = async (req) => {
		calls.push(req);
		if (req.method === 'DELETE') return { status: 204, headers: {}, body: '' };
		const msg = JSON.parse(req.body ?? '{}') as { id?: number; method: string };
		const handler = overrides[msg.method];
		if (handler) return handler(msg.id as number, req);
		switch (msg.method) {
			case 'initialize':
				return json(rpcResult(msg.id as number, { protocolVersion: '2025-11-25' }), {
					'mcp-session-id': 'sess-1',
				});
			case 'notifications/initialized':
				return { status: 202, headers: {}, body: '' };
			case 'tools/list':
				return json(rpcResult(msg.id as number, { tools: [{ name: 't1', description: 'd' }] }));
			case 'tools/call':
				return json(rpcResult(msg.id as number, { content: [{ type: 'text', text: '{"ok":1}' }] }));
			default:
				return { status: 500, headers: {}, body: '' };
		}
	};
	return { send, calls };
}

async function failure(fn: () => Promise<unknown>): Promise<McpTransportError> {
	try {
		await fn();
	} catch (error) {
		assert.ok(error instanceof McpTransportError, 'a McpTransportError is thrown');
		assert.ok(!error.message.includes(TOKEN), 'message has no token');
		assert.ok(!error.message.includes('/private/path'), 'message has no URL path');
		assert.ok(!JSON.stringify(error).includes(TOKEN));
		assert.equal((error as { cause?: unknown }).cause, undefined);
		return error;
	}
	assert.fail('expected a failure');
}

test('full handshake order and headers', async () => {
	const { send, calls } = rig();
	const session = await openSession(send, opts);
	const tools = await session.listTools();
	assert.deepEqual(tools.map((t) => t.name), ['t1']);
	const result = await session.callTool('t1', { a: 1, reason: 'why' });
	assert.deepEqual(result.content, [{ type: 'text', text: '{"ok":1}' }]);
	await session.close();

	const bodies = calls.map((c) => (c.body ? (JSON.parse(c.body) as Record<string, unknown>) : null));
	assert.deepEqual(
		calls.map((c, i) => `${c.method} ${bodies[i]?.method ?? ''}`.trim()),
		[
			'POST initialize',
			'POST notifications/initialized',
			'POST tools/list',
			'POST tools/call',
			'DELETE',
		],
	);
	assert.deepEqual(bodies[0]?.params, {
		protocolVersion: '2025-11-25',
		capabilities: {},
		clientInfo: { name: 'n8n-nodes-privacyfence', version: '1.2.3' },
	});
	assert.deepEqual(
		[bodies[0]?.id, bodies[2]?.id, bodies[3]?.id],
		[1, 2, 3],
	);
	assert.equal(bodies[1]?.id, undefined);
	assert.deepEqual(bodies[3]?.params, { name: 't1', arguments: { a: 1, reason: 'why' } });

	for (const [i, c] of calls.entries()) {
		assert.equal(c.url, URL_);
		assert.equal(c.timeoutMs, 5000);
		assert.equal(c.headers.Authorization, `Bearer ${TOKEN}`);
		assert.equal(c.headers.Accept, 'application/json, text/event-stream');
		assert.equal(c.headers['Content-Type'], 'application/json');
		if (i === 0) {
			assert.equal(c.headers['Mcp-Session-Id'], undefined);
		} else {
			assert.equal(c.headers['Mcp-Session-Id'], 'sess-1');
			assert.equal(c.headers['MCP-Protocol-Version'], '2025-11-25');
		}
	}
});

test('an answered older supported version is used on later requests', async () => {
	const { send, calls } = rig({
		initialize: (id) =>
			json(rpcResult(id, { protocolVersion: '2025-03-26' }), { 'mcp-session-id': 's' }),
	});
	const session = await openSession(send, opts);
	await session.listTools();
	assert.equal(calls[2]?.headers['MCP-Protocol-Version'], '2025-03-26');
});

test('close is idempotent and swallows every error', async () => {
	const base = rig();
	let deletes = 0;
	const send: HttpSend = async (req) => {
		if (req.method === 'DELETE') {
			deletes++;
			throw new Error(`boom ${TOKEN}`);
		}
		return base.send(req);
	};
	const session = await openSession(send, opts);
	await session.close();
	await session.close();
	assert.equal(deletes, 1);
});

test('an SSE answer with a notification and a foreign id before the answer', async () => {
	const { send } = rig({
		'tools/call': (id) => ({
			status: 200,
			headers: { 'content-type': 'text/event-stream; charset=utf-8' },
			body:
				': keepalive\r\n\r\n' +
				'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\r\n\r\n' +
				'data: {"jsonrpc":"2.0","id":999,"result":{"content":[]}}\r\n\r\n' +
				`data: ${JSON.stringify(rpcResult(id, { content: [{ type: 'text', text: 'hi' }] }))}\r\n\r\n`,
		}),
	});
	const session = await openSession(send, opts);
	const result = await session.callTool('x', {});
	assert.deepEqual(result.content, [{ type: 'text', text: 'hi' }]);
});

test('an SSE stream without the answer is not MCP', async () => {
	const { send } = rig({
		'tools/call': () => ({
			status: 200,
			headers: { 'content-type': 'text/event-stream' },
			body: 'data: {"jsonrpc":"2.0","method":"ping"}\n\n',
		}),
	});
	const session = await openSession(send, opts);
	const error = await failure(() => session.callTool('x', {}));
	assert.equal(error.message, `PrivacyFence at ${HOST} did not speak MCP as expected.`);
});

test('an unsupported version errors and the session is closed', async () => {
	const { send, calls } = rig({
		initialize: (id) =>
			json(rpcResult(id, { protocolVersion: '2026-07-28' }), { 'mcp-session-id': 's' }),
	});
	const error = await failure(() => openSession(send, opts));
	assert.equal(error.message, `PrivacyFence at ${HOST} did not speak MCP as expected.`);
	assert.equal(calls.at(-1)?.method, 'DELETE');
});

test('a missing protocolVersion errors', async () => {
	const { send } = rig({
		initialize: (id) => json(rpcResult(id, {}), { 'mcp-session-id': 's' }),
	});
	await failure(() => openSession(send, opts));
});

test('a missing session id errors', async () => {
	const { send } = rig({ initialize: (id) => json(rpcResult(id, { protocolVersion: '2025-11-25' })) });
	const error = await failure(() => openSession(send, opts));
	assert.equal(error.message, `PrivacyFence at ${HOST} did not speak MCP as expected.`);
});

test('401 and 403 are the token text', async () => {
	for (const status of [401, 403]) {
		const send: HttpSend = async () => ({ status, headers: {}, body: `denied ${TOKEN}` });
		const error = await failure(() => openSession(send, opts));
		assert.equal(
			error.message,
			`PrivacyFence at ${HOST} refused the token (HTTP ${status}); it may be revoked or expired.`,
		);
	}
});

test('3xx is the redirect text', async () => {
	for (const status of [301, 302, 307, 308]) {
		const send: HttpSend = async () => ({
			status,
			headers: { location: 'https://evil.example/' },
			body: '',
		});
		const error = await failure(() => openSession(send, opts));
		assert.equal(
			error.message,
			`PrivacyFence at ${HOST} redirected the request; check the PrivacyFence MCP address in the credential.`,
		);
	}
});

test('any other status is the HTTP text', async () => {
	for (const status of [400, 404, 500, 503, 100]) {
		const send: HttpSend = async () => ({ status, headers: {}, body: '' });
		const error = await failure(() => openSession(send, opts));
		assert.equal(error.message, `PrivacyFence at ${HOST} answered HTTP ${status}.`);
	}
});

test('a sender that throws is unreachable, without the cause', async () => {
	const send: HttpSend = async (req) => {
		throw new Error(`connect failed ${req.url} ${req.headers.Authorization}`);
	};
	const error = await failure(() => openSession(send, opts));
	assert.equal(error.message, `PrivacyFence at ${HOST} could not be reached.`);
});

test('a sender that returns nothing usable is unreachable', async () => {
	const send = (async () => undefined) as unknown as HttpSend;
	const error = await failure(() => openSession(send, opts));
	assert.equal(error.message, `PrivacyFence at ${HOST} could not be reached.`);
});

test('an unparseable URL still gives a fixed text', async () => {
	const send: HttpSend = async () => {
		throw new Error('x');
	};
	const error = await failure(() => openSession(send, { ...opts, url: 'not a url' }));
	assert.equal(error.message, 'PrivacyFence at the configured address could not be reached.');
});

test('a JSON-RPC error carries a cleaned, cut message', async () => {
	const { send } = rig({
		'tools/call': (id) =>
			json({ jsonrpc: '2.0', id, error: { code: -32602, message: 'bad\u0000 args\n' + 'y'.repeat(500) } }),
	});
	const session = await openSession(send, opts);
	const error = await failure(() => session.callTool('x', {}));
	assert.equal(error.message, `PrivacyFence at ${HOST} answered an MCP error: bad args${'y'.repeat(292)}`);
});

test('a JSON-RPC error without a message', async () => {
	const { send } = rig({
		'tools/call': (id) => json({ jsonrpc: '2.0', id, error: { code: 1 } }),
	});
	const session = await openSession(send, opts);
	const error = await failure(() => session.callTool('x', {}));
	assert.equal(error.message, `PrivacyFence at ${HOST} answered an MCP error: `);
});

test('bad bodies are not MCP', async () => {
	const cases: HttpResponse[] = [
		{ status: 200, headers: JSON_H, body: 'not json' },
		{ status: 200, headers: { 'content-type': 'text/html' }, body: '<html>' },
		{ status: 200, headers: {}, body: '{}' },
		json({ jsonrpc: '2.0', id: 77, result: {} }),
		json({ jsonrpc: '2.0', id: 2 }),
	];
	for (const response of cases) {
		const { send } = rig({ 'tools/call': () => response });
		const session = await openSession(send, opts);
		const error = await failure(() => session.callTool('x', {}));
		assert.equal(error.message, `PrivacyFence at ${HOST} did not speak MCP as expected.`);
	}
});

test('a tools/call result that is not an object, and one without content', async () => {
	let r: unknown = 5;
	const { send } = rig({ 'tools/call': (id) => json(rpcResult(id, r)) });
	const session = await openSession(send, opts);
	await failure(() => session.callTool('x', {}));
	r = { structuredContent: { a: 1 } };
	const result = await session.callTool('x', {});
	assert.deepEqual(result, { structuredContent: { a: 1 }, content: [] });
});

test('a JSON-RPC batch answer is searched for the id', async () => {
	const { send } = rig({
		'tools/call': (id) =>
			json([{ jsonrpc: '2.0', method: 'x' }, rpcResult(id, { content: [] })]),
	});
	const session = await openSession(send, opts);
	assert.deepEqual((await session.callTool('x', {})).content, []);
});

test('a body over the cap is refused', async () => {
	const { send } = rig({
		'tools/list': () => ({ status: 200, headers: JSON_H, body: ' '.repeat(10_000_001) }),
	});
	const session = await openSession(send, opts);
	const error = await failure(() => session.listTools());
	assert.equal(error.message, `PrivacyFence at ${HOST} did not speak MCP as expected.`);
});

test('a non-string body is refused', async () => {
	const { send } = rig({
		'tools/list': () => ({ status: 200, headers: JSON_H, body: undefined as unknown as string }),
	});
	const session = await openSession(send, opts);
	await failure(() => session.listTools());
});

test('tools/list follows nextCursor and stops at 10 pages', async () => {
	const cursors: unknown[] = [];
	const { send } = rig({
		'tools/list': (id, req) => {
			const params = (JSON.parse(req.body ?? '{}') as { params: { cursor?: string } }).params;
			cursors.push(params.cursor);
			return json(
				rpcResult(id, { tools: [{ name: `t${cursors.length}` }, { nope: 1 }, 'junk'], nextCursor: `c${cursors.length}` }),
			);
		},
	});
	const session = await openSession(send, opts);
	const tools = await session.listTools();
	assert.equal(tools.length, 10);
	assert.equal(cursors.length, 10);
	assert.equal(cursors[0], undefined);
	assert.equal(cursors[1], 'c1');
	assert.equal(tools[0]?.description, '');
});

test('tools/list stops when nextCursor is absent or empty', async () => {
	let n = 0;
	const { send } = rig({
		'tools/list': (id) => {
			n++;
			return json(rpcResult(id, { tools: [{ name: 'a', description: 'D', inputSchema: {}, annotations: { readOnlyHint: true } }], nextCursor: n < 2 ? 'x' : '' }));
		},
	});
	const session = await openSession(send, opts);
	const tools = await session.listTools();
	assert.equal(tools.length, 2);
	assert.deepEqual(tools[0]?.annotations, { readOnlyHint: true });
});

test('tools/list without a tools array is not MCP', async () => {
	const { send } = rig({ 'tools/list': (id) => json(rpcResult(id, { tools: 'x' })) });
	const session = await openSession(send, opts);
	await failure(() => session.listTools());
});

test('a failing initialized notification closes the session', async () => {
	const { send, calls } = rig({
		'notifications/initialized': () => ({ status: 500, headers: {}, body: '' }),
	});
	const error = await failure(() => openSession(send, opts));
	assert.equal(error.message, `PrivacyFence at ${HOST} answered HTTP 500.`);
	assert.equal(calls.at(-1)?.method, 'DELETE');
});

test('the token and the URL path are in no error, in every failure case', async () => {
	const hostile = `x ${TOKEN} ${URL_}`;
	const senders: HttpSend[] = [
		async () => {
			throw new Error(hostile);
		},
		async () => ({ status: 401, headers: {}, body: hostile }),
		async () => ({ status: 302, headers: { location: URL_ }, body: hostile }),
		async () => ({ status: 500, headers: {}, body: hostile }),
		async () => ({ status: 200, headers: JSON_H, body: hostile }),
		async () => json({ jsonrpc: '2.0', id: 1, error: { message: 'plain' } }),
		async () => json(rpcResult(1, { protocolVersion: 'nope' }), { 'mcp-session-id': 's' }),
	];
	for (const send of senders) {
		await failure(() => openSession(send, opts));
	}
});
