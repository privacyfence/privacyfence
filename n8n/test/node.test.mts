import assert from 'node:assert/strict';
import { test } from 'node:test';

import { PrivacyFence } from '../nodes/PrivacyFence/PrivacyFence.node.ts';

const TOKEN = 'pf_secret_token_value_123';
const URL_ = 'https://pf.example.org/private/path/mcp';

type Json = Record<string, unknown>;
interface Call {
	method: string;
	rpc?: string;
}

interface ServerOptions {
	tools?: Array<Record<string, unknown>>;
	callResult?: Json;
	failAt?: string;
	statusAt?: { rpc: string; status: number };
}

function fakeServer(options: ServerOptions = {}) {
	const calls: Call[] = [];
	const sent: Json[] = [];
	const tools = options.tools ?? [
		{ name: 'privacyfence_check_policy', description: 'hidden' },
		{ name: 'privacyfence_status', description: 'S'.repeat(500) },
		{ name: 'gmail_search', description: 'Search', annotations: { readOnlyHint: true } },
		{ name: 'drive_write', description: 'Write' },
	];
	const httpRequest = async (opts: Json): Promise<Json> => {
		const method = opts.method as string;
		if (method === 'DELETE') {
			calls.push({ method });
			return { statusCode: 200, headers: {}, body: '' };
		}
		const body = JSON.parse(opts.body as string) as Json;
		const rpc = body.method as string;
		calls.push({ method, rpc });
		sent.push(body);
		if (options.failAt === rpc) throw new Error(`boom ${TOKEN}`);
		if (options.statusAt?.rpc === rpc) {
			return { statusCode: options.statusAt.status, headers: {}, body: `body ${TOKEN}` };
		}
		const json = { 'content-type': 'application/json' };
		if (rpc === 'initialize') {
			return {
				statusCode: 200,
				headers: { ...json, 'Mcp-Session-Id': 'sess' },
				body: JSON.stringify({
					jsonrpc: '2.0',
					id: body.id,
					result: { protocolVersion: '2025-11-25', capabilities: {} },
				}),
			};
		}
		if (rpc === 'notifications/initialized') return { statusCode: 202, headers: {}, body: '' };
		const result =
			rpc === 'tools/list'
				? { tools }
				: (options.callResult ?? { content: [], structuredContent: { ok: 1 } });
		return {
			statusCode: 200,
			headers: json,
			body: JSON.stringify({ jsonrpc: '2.0', id: body.id, result }),
		};
	};
	return { calls, sent, httpRequest };
}

function context(
	server: ReturnType<typeof fakeServer>,
	params: Json,
	opts: { continueOnFail?: boolean; items?: number; credential?: Json } = {},
) {
	return {
		getInputData: () => Array.from({ length: opts.items ?? 1 }, () => ({ json: {} })),
		getNode: () => ({ name: 'PrivacyFence', type: 'privacyFence', typeVersion: 1 }),
		getCredentials: async () => opts.credential ?? { mcpUrl: URL_, token: TOKEN },
		getNodeParameter: (name: string, _index: number, fallback?: unknown) =>
			name in params ? params[name] : fallback,
		continueOnFail: () => opts.continueOnFail === true,
		helpers: { httpRequest: server.httpRequest },
	};
}

const node = new PrivacyFence();
const run = (ctx: unknown) =>
	(
		node.execute as (this: unknown) => Promise<Array<Array<{ json: Json; pairedItem?: unknown }>>>
	).call(ctx);
const callParams = {
	operation: 'callTool',
	tool: 'gmail_search',
	reason: 'why',
	arguments: '{"q":"x"}',
};

function assertNoToken(text: string) {
	assert.ok(!text.includes(TOKEN), `token leaked: ${text}`);
	assert.ok(!text.includes('/private/path'), `path leaked: ${text}`);
}

test('a call returns the structured item and sends the reason', async () => {
	const server = fakeServer();
	const [items] = await run(context(server, callParams));
	assert.deepEqual(items, [{ json: { ok: 1 }, pairedItem: { item: 0 } }]);
	const call = server.sent.find((m) => m.method === 'tools/call') as Json;
	assert.deepEqual(call.params, { name: 'gmail_search', arguments: { q: 'x', reason: 'why' } });
	assert.deepEqual(server.calls.at(-1), { method: 'DELETE' });
});

test('the reason field wins over a reason key in the arguments', async () => {
	const server = fakeServer();
	await run(context(server, { ...callParams, arguments: { reason: 'inner' } }));
	const call = server.sent.find((m) => m.method === 'tools/call') as { params: Json };
	assert.deepEqual(call.params.arguments, { reason: 'why' });
});

test('an isError result throws with the daemon text and closes the session', async () => {
	const server = fakeServer({
		callResult: { isError: true, content: [{ type: 'text', text: 'Request denied: nope' }] },
	});
	await assert.rejects(run(context(server, callParams)), (error: Error) => {
		assert.equal(error.message, 'Request denied: nope');
		return true;
	});
	assert.deepEqual(server.calls.at(-1), { method: 'DELETE' });
});

test('with continueOnFail an error becomes an error item', async () => {
	const server = fakeServer({
		callResult: { isError: true, content: [{ type: 'text', text: 'Request denied: nope' }] },
	});
	const [items] = await run(context(server, callParams, { continueOnFail: true, items: 2 }));
	assert.deepEqual(items, [
		{ json: { error: 'Request denied: nope' }, pairedItem: { item: 0 } },
		{ json: { error: 'Request denied: nope' }, pairedItem: { item: 1 } },
	]);
	assert.equal(server.calls.filter((c) => c.rpc === 'initialize').length, 1);
});

test('invalid arguments are refused before any call', async () => {
	for (const bad of ['not json', '[1]', '"s"', '3']) {
		const server = fakeServer();
		await assert.rejects(
			run(context(server, { ...callParams, arguments: bad })),
			/The arguments must be a JSON object\./,
		);
		assert.ok(!server.calls.some((c) => c.rpc === 'tools/call'));
	}
});

test('listTools hides privacy tools except status and pairs with item 0', async () => {
	const server = fakeServer();
	const [items] = await run(context(server, { operation: 'listTools' }, { items: 2 }));
	assert.deepEqual(
		items.map((i) => i.json.name),
		['drive_write', 'gmail_search', 'privacyfence_status'],
	);
	assert.equal(items[1].json.readOnly, true);
	assert.equal(items[0].json.readOnly, false);
	assert.ok(items.every((i) => (i.pairedItem as { item: number }).item === 0));
});

test('the picker hides privacyfence_check_policy, keeps privacyfence_status, cuts descriptions', async () => {
	const server = fakeServer();
	const options = await node.methods.loadOptions.getTools.call(context(server, {}) as never);
	assert.deepEqual(
		options.map((o) => o.value),
		['drive_write', 'gmail_search', 'privacyfence_status'],
	);
	assert.equal(options[2].description?.length, 200);
	assert.deepEqual(server.calls.at(-1), { method: 'DELETE' });
});

test('a missing credential value is refused', async () => {
	const server = fakeServer();
	await assert.rejects(
		run(context(server, callParams, { credential: { mcpUrl: '', token: TOKEN } })),
		/The PrivacyFence credential needs an address and a token\./,
	);
});

test('the credential test returns the OK and Error shapes', async () => {
	const requests: Json[] = [];
	const make = (server: ReturnType<typeof fakeServer>) => ({
		helpers: {
			request: async (opts: Json) => {
				requests.push(opts);
				return server.httpRequest({ ...opts, body: opts.body });
			},
		},
	});
	const test_ = node.methods.credentialTest.privacyFenceApiTest;
	const ok = await test_.call(
		make(fakeServer()) as never,
		{
			data: { mcpUrl: URL_, token: TOKEN },
		} as never,
	);
	assert.deepEqual(ok, {
		status: 'OK',
		message: 'Connected to PrivacyFence. 4 tools are available to this token.',
	});
	assert.ok(requests.every((r) => r.followRedirect === false && r.simple === false));
	const refused = await test_.call(
		make(fakeServer({ statusAt: { rpc: 'initialize', status: 401 } })) as never,
		{ data: { mcpUrl: URL_, token: TOKEN } } as never,
	);
	assert.equal(refused.status, 'Error');
	assert.match(refused.message, /refused the token \(HTTP 401\)/);
	const missing = await test_.call(make(fakeServer()) as never, { data: {} } as never);
	assert.equal(missing.status, 'Error');
});

test('the token is in no message, error item or picker failure', async () => {
	const failures: ServerOptions[] = [
		{ failAt: 'initialize' },
		{ failAt: 'tools/call' },
		{ statusAt: { rpc: 'initialize', status: 401 } },
		{ statusAt: { rpc: 'initialize', status: 302 } },
		{ statusAt: { rpc: 'tools/call', status: 500 } },
		{ callResult: { isError: true, content: [{ type: 'text', text: 'Request denied' }] } },
		{ callResult: { _meta: { 'privacyfence.eu/file-bridge': {} }, content: [] } },
	];
	for (const failure of failures) {
		await assert.rejects(run(context(fakeServer(failure), callParams)), (error: Error) => {
			assertNoToken(error.message);
			assertNoToken(JSON.stringify(error, Object.getOwnPropertyNames(error)));
			return true;
		});
		const [items] = await run(context(fakeServer(failure), callParams, { continueOnFail: true }));
		assertNoToken(JSON.stringify(items));
		if (failure.failAt === 'initialize' || failure.statusAt?.rpc === 'initialize') {
			await assert.rejects(
				node.methods.loadOptions.getTools.call(context(fakeServer(failure), {}) as never),
				(error: Error) => {
					assertNoToken(error.message);
					return true;
				},
			);
		}
	}
});

test('an unexpected failure gets the fixed text and no cause', async () => {
	const server = fakeServer();
	const ctx = context(server, callParams);
	ctx.getNodeParameter = () => {
		throw new Error(`leak ${TOKEN}`);
	};
	await assert.rejects(run(ctx), (error: Error) => {
		assert.equal(error.message, 'The PrivacyFence node failed unexpectedly.');
		assertNoToken(JSON.stringify(error, Object.getOwnPropertyNames(error)));
		return true;
	});
});
