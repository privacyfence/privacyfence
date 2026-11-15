import { cleanText } from './result';
import { parseSse } from './sse';

export interface HttpRequest {
	method: 'POST' | 'DELETE';
	url: string;
	headers: Record<string, string>;
	body?: string;
	timeoutMs: number;
}
export interface HttpResponse {
	status: number;
	headers: Record<string, string>;
	body: string;
}
export type HttpSend = (request: HttpRequest) => Promise<HttpResponse>;

export class McpTransportError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'McpTransportError';
	}
}

export interface McpTool {
	name: string;
	description: string;
	inputSchema: unknown;
	annotations?: unknown;
}
export interface CallToolResult {
	content: Array<{ type: string; text?: string }>;
	isError?: boolean;
	structuredContent?: unknown;
	_meta?: Record<string, unknown>;
}
export interface McpSession {
	listTools(): Promise<McpTool[]>;
	callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult>;
	close(): Promise<void>;
}
export interface OpenSessionOptions {
	url: string;
	token: string;
	timeoutMs: number;
	clientVersion: string;
}

export const REQUESTED_VERSION = '2025-11-25';
export const SUPPORTED_VERSIONS: readonly string[] = ['2025-11-25', '2025-06-18', '2025-03-26'];
export const MAX_BODY_CHARS = 10_000_000;
export const MAX_PAGES = 10;
const MAX_MCP_MESSAGE = 300;

function hostOf(url: string): string {
	try {
		return new URL(url).host || 'the configured address';
	} catch {
		return 'the configured address';
	}
}

function texts(host: string) {
	return {
		unreachable: `PrivacyFence at ${host} could not be reached.`,
		status: (status: number) => `PrivacyFence at ${host} answered HTTP ${status}.`,
		refused: (status: number) =>
			`PrivacyFence at ${host} refused the token (HTTP ${status}); it may be revoked or expired.`,
		redirect: `PrivacyFence at ${host} redirected the request; check the PrivacyFence MCP address in the credential.`,
		notMcp: `PrivacyFence at ${host} did not speak MCP as expected.`,
		mcpError: (message: string) => `PrivacyFence at ${host} answered an MCP error: ${message}`,
	};
}

/** The only place the token is put into a header. */
function authorizationHeaders(token: string): Record<string, string> {
	return {
		Authorization: `Bearer ${token}`,
		'Content-Type': 'application/json',
		Accept: 'application/json, text/event-stream',
	};
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export async function openSession(send: HttpSend, options: OpenSessionOptions): Promise<McpSession> {
	const { url, timeoutMs } = options;
	const t = texts(hostOf(url));
	const baseHeaders = authorizationHeaders(options.token);
	let sessionHeaders: Record<string, string> = {};
	let nextId = 1;
	let closed = false;

	const transmit = async (
		method: 'POST' | 'DELETE',
		body: unknown,
	): Promise<HttpResponse> => {
		let response: HttpResponse | undefined;
		let failed = false;
		try {
			response = await send({
				method,
				url,
				headers: { ...baseHeaders, ...sessionHeaders },
				body: body === undefined ? undefined : JSON.stringify(body),
				timeoutMs,
			});
		} catch {
			failed = true;
		}
		if (failed || !response || typeof response.status !== 'number') {
			throw new McpTransportError(t.unreachable);
		}
		const { status } = response;
		if (status >= 300 && status < 400) throw new McpTransportError(t.redirect);
		if (status === 401 || status === 403) throw new McpTransportError(t.refused(status));
		if (status < 200 || status >= 300) throw new McpTransportError(t.status(status));
		if (typeof response.body !== 'string' || response.body.length > MAX_BODY_CHARS) {
			throw new McpTransportError(t.notMcp);
		}
		return response;
	};

	const rpc = async (
		method: string,
		params: Record<string, unknown>,
	): Promise<{ result: unknown; response: HttpResponse }> => {
		const id = nextId++;
		const response = await transmit('POST', { jsonrpc: '2.0', id, method, params });
		const contentType = (response.headers['content-type'] ?? '').toLowerCase();
		let messages: unknown[];
		if (contentType.includes('text/event-stream')) {
			messages = parseSse(response.body);
		} else if (contentType.includes('application/json')) {
			let parsed: unknown;
			let ok = true;
			try {
				parsed = JSON.parse(response.body);
			} catch {
				ok = false;
			}
			if (!ok) throw new McpTransportError(t.notMcp);
			messages = Array.isArray(parsed) ? parsed : [parsed];
		} else {
			throw new McpTransportError(t.notMcp);
		}
		const answer = messages.find((m) => isRecord(m) && m.id === id);
		if (!isRecord(answer)) throw new McpTransportError(t.notMcp);
		if (isRecord(answer.error)) {
			const message = typeof answer.error.message === 'string' ? answer.error.message : '';
			throw new McpTransportError(t.mcpError(cleanText(message, MAX_MCP_MESSAGE)));
		}
		if (!('result' in answer)) throw new McpTransportError(t.notMcp);
		return { result: answer.result, response };
	};

	const close = async (): Promise<void> => {
		if (closed) return;
		closed = true;
		try {
			await send({
				method: 'DELETE',
				url,
				headers: { ...baseHeaders, ...sessionHeaders },
				timeoutMs,
			});
		} catch {
			// Closing never fails.
		}
	};

	// Handshake: initialize, then notifications/initialized.
	const init = await rpc('initialize', {
		protocolVersion: REQUESTED_VERSION,
		capabilities: {},
		clientInfo: { name: 'n8n-nodes-privacyfence', version: options.clientVersion },
	});
	const sessionId = init.response.headers['mcp-session-id'];
	if (!sessionId) throw new McpTransportError(t.notMcp);
	const answered = isRecord(init.result) ? init.result.protocolVersion : undefined;
	sessionHeaders = { 'Mcp-Session-Id': sessionId };
	if (typeof answered !== 'string' || !SUPPORTED_VERSIONS.includes(answered)) {
		await close();
		throw new McpTransportError(t.notMcp);
	}
	sessionHeaders = { 'Mcp-Session-Id': sessionId, 'MCP-Protocol-Version': answered };

	let notifyError: McpTransportError | undefined;
	try {
		await transmit('POST', { jsonrpc: '2.0', method: 'notifications/initialized' });
	} catch (error) {
		notifyError = error instanceof McpTransportError ? error : new McpTransportError(t.notMcp);
	}
	if (notifyError) {
		await close();
		throw notifyError;
	}

	return {
		async listTools(): Promise<McpTool[]> {
			const tools: McpTool[] = [];
			let cursor: string | undefined;
			for (let page = 0; page < MAX_PAGES; page++) {
				const { result } = await rpc('tools/list', cursor === undefined ? {} : { cursor });
				if (!isRecord(result) || !Array.isArray(result.tools)) {
					throw new McpTransportError(t.notMcp);
				}
				for (const raw of result.tools) {
					if (!isRecord(raw) || typeof raw.name !== 'string') continue;
					tools.push({
						name: raw.name,
						description: typeof raw.description === 'string' ? raw.description : '',
						inputSchema: raw.inputSchema,
						annotations: raw.annotations,
					});
				}
				const next = result.nextCursor;
				if (typeof next !== 'string' || next === '') break;
				cursor = next;
			}
			return tools;
		},
		async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult> {
			const { result } = await rpc('tools/call', { name, arguments: args });
			if (!isRecord(result)) throw new McpTransportError(t.notMcp);
			const content = Array.isArray(result.content) ? result.content : [];
			return { ...result, content } as CallToolResult;
		},
		close,
	};
}
