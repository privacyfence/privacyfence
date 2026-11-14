import type {
	ICredentialDataDecryptedObject,
	ICredentialTestFunctions,
	ICredentialsDecrypted,
	IDataObject,
	IExecuteFunctions,
	ILoadOptionsFunctions,
	INodeCredentialTestResult,
	INodeExecutionData,
	INodePropertyOptions,
	INodeType,
	INodeTypeDescription,
} from 'n8n-workflow';
import { NodeConnectionTypes, NodeOperationError, UnexpectedError } from 'n8n-workflow';

import type { HttpResponse, HttpSend, McpSession, McpTool } from './transport/mcpClient';
import { McpTransportError, openSession } from './transport/mcpClient';
import { toItem } from './transport/result';

const CREDENTIAL_NAME = 'privacyFenceApi';
const CLIENT_VERSION = '0.0.0';
const DEFAULT_TIMEOUT_MS = 60000;
const MAX_DESCRIPTION = 200;
const MISSING_CONNECTION = 'The PrivacyFence credential needs an address and a token.';
const UNEXPECTED = 'The PrivacyFence node failed unexpectedly.';
const BAD_ARGUMENTS = 'The arguments must be a JSON object.';

interface Connection {
	url: string;
	token: string;
}

type HeaderValue = string | string[] | undefined;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function connectionFrom(data: ICredentialDataDecryptedObject | undefined): Connection | undefined {
	const url = typeof data?.mcpUrl === 'string' ? data.mcpUrl.trim() : '';
	const token = typeof data?.token === 'string' ? data.token.trim() : '';
	return url === '' || token === '' ? undefined : { url, token };
}

/** Reads the credential. The only function that touches the credential. */
async function readConnection(
	context: IExecuteFunctions | ILoadOptionsFunctions,
): Promise<Connection> {
	const data = await context.getCredentials(CREDENTIAL_NAME);
	const connection = connectionFrom(data);
	if (!connection) throw new NodeOperationError(context.getNode(), MISSING_CONNECTION);
	return connection;
}

function lowerHeaders(headers: unknown): Record<string, string> {
	const out: Record<string, string> = {};
	if (!isRecord(headers)) return out;
	for (const [key, raw] of Object.entries(headers)) {
		const value = raw as HeaderValue;
		if (typeof value === 'string') out[key.toLowerCase()] = value;
		else if (Array.isArray(value)) out[key.toLowerCase()] = value.join(', ');
	}
	return out;
}

/** Builds the sender. The only function that sends a request; it never sees the credential. */
function makeSend(context: IExecuteFunctions | ILoadOptionsFunctions): HttpSend {
	return async (request) => {
		let response: unknown;
		let failed = false;
		try {
			response = await context.helpers.httpRequest({
				method: request.method,
				url: request.url,
				headers: request.headers,
				body: request.body,
				timeout: request.timeoutMs,
				returnFullResponse: true,
				ignoreHttpStatusErrors: true,
				disableFollowRedirect: true,
				encoding: 'text',
				json: false,
			});
		} catch {
			failed = true;
		}
		if (failed || !isRecord(response)) throw new UnexpectedError('unreachable');
		const mapped: HttpResponse = {
			status: typeof response.statusCode === 'number' ? response.statusCode : 0,
			headers: lowerHeaders(response.headers),
			body: typeof response.body === 'string' ? response.body : '',
		};
		return mapped;
	};
}

/** The sender for credential tests, which only have the legacy `request` helper. */
function makeTestSend(context: ICredentialTestFunctions): HttpSend {
	return async (request) => {
		let response: unknown;
		let failed = false;
		try {
			// eslint-disable-next-line @n8n/community-nodes/no-deprecated-workflow-functions -- ICredentialTestFunctions declares only `request`
			response = await context.helpers.request({
				method: request.method,
				uri: request.url,
				headers: request.headers,
				body: request.body,
				timeout: request.timeoutMs,
				resolveWithFullResponse: true,
				simple: false,
				followRedirect: false,
				followAllRedirects: false,
				json: false,
			});
		} catch {
			failed = true;
		}
		if (failed || !isRecord(response)) throw new UnexpectedError('unreachable');
		const mapped: HttpResponse = {
			status: typeof response.statusCode === 'number' ? response.statusCode : 0,
			headers: lowerHeaders(response.headers),
			body: typeof response.body === 'string' ? response.body : '',
		};
		return mapped;
	};
}

/** The tools a token may call: PrivacyFence's own meta tools are hidden, except its status tool. */
function visibleTools(tools: McpTool[]): McpTool[] {
	return tools
		.filter((tool) => !tool.name.startsWith('privacyfence_') || tool.name === 'privacyfence_status')
		.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

function isReadOnly(tool: McpTool): boolean {
	return isRecord(tool.annotations) && tool.annotations.readOnlyHint === true;
}

async function getTools(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
	const connection = await readConnection(this);
	const send = makeSend(this);
	let failure: string | undefined;
	let options: INodePropertyOptions[] = [];
	let session: McpSession | undefined;
	try {
		session = await openSession(send, {
			...connection,
			timeoutMs: DEFAULT_TIMEOUT_MS,
			clientVersion: CLIENT_VERSION,
		});
		const tools = visibleTools(await session.listTools());
		options = tools.map((tool) => ({
			name: tool.name,
			value: tool.name,
			description: tool.description.slice(0, MAX_DESCRIPTION),
		}));
	} catch (error) {
		failure = error instanceof McpTransportError ? error.message : UNEXPECTED;
	} finally {
		if (session) await session.close();
	}
	if (failure !== undefined) throw new NodeOperationError(this.getNode(), failure);
	return options;
}

async function privacyFenceApiTest(
	this: ICredentialTestFunctions,
	credential: ICredentialsDecrypted,
): Promise<INodeCredentialTestResult> {
	const connection = connectionFrom(credential.data);
	if (!connection) return { status: 'Error', message: MISSING_CONNECTION };
	const send = makeTestSend(this);
	let message: string | undefined;
	let count = 0;
	let session: McpSession | undefined;
	try {
		session = await openSession(send, {
			...connection,
			timeoutMs: DEFAULT_TIMEOUT_MS,
			clientVersion: CLIENT_VERSION,
		});
		count = (await session.listTools()).length;
	} catch (error) {
		message = error instanceof McpTransportError ? error.message : UNEXPECTED;
	} finally {
		if (session) await session.close();
	}
	if (message !== undefined) return { status: 'Error', message };
	return {
		status: 'OK',
		message: `Connected to PrivacyFence. ${count} tools are available to this token.`,
	};
}

export class PrivacyFence implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'PrivacyFence',
		name: 'privacyFence',
		icon: {
			light: 'file:privacyfence.svg',
			dark: 'file:privacyfence.dark.svg',
		},
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["operation"] === "listTools" ? "List tools" : $parameter["tool"]}}',
		description: 'Call a tool your PrivacyFence server lets this workflow use',
		defaults: {
			name: 'PrivacyFence',
		},
		usableAsTool: true,
		inputs: [NodeConnectionTypes.Main],
		outputs: [NodeConnectionTypes.Main],
		credentials: [
			{
				name: 'privacyFenceApi',
				required: true,
				testedBy: 'privacyFenceApiTest',
			},
		],
		properties: [
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Call a Tool', value: 'callTool' },
					{ name: 'List Tools', value: 'listTools' },
				],
				default: 'callTool',
			},
			{
				displayName: 'Tool Name or ID',
				name: 'tool',
				type: 'options',
				typeOptions: { loadOptionsMethod: 'getTools' },
				required: true,
				default: '',
				description:
					'Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>',
				displayOptions: { show: { operation: ['callTool'] } },
			},
			{
				displayName: 'Reason',
				name: 'reason',
				type: 'string',
				required: true,
				default: '={{ "n8n workflow: " + $workflow.name }}',
				description: 'Why this call is made. PrivacyFence records it in its audit log.',
				displayOptions: { show: { operation: ['callTool'] } },
			},
			{
				displayName: 'Arguments',
				name: 'arguments',
				type: 'json',
				default: '{}',
				description: "The tool's arguments as a JSON object, without reason",
				displayOptions: { show: { operation: ['callTool'] } },
			},
			{
				displayName: 'Options',
				name: 'options',
				type: 'collection',
				placeholder: 'Add Option',
				default: {},
				options: [
					{
						displayName: 'Timeout',
						name: 'timeout',
						type: 'number',
						typeOptions: { minValue: 1000 },
						default: 60000,
						description: 'Maximum time in milliseconds to wait for PrivacyFence',
					},
				],
			},
		],
	};

	methods = {
		loadOptions: { getTools },
		credentialTest: { privacyFenceApiTest },
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];
		const connection = await readConnection(this);
		const send = makeSend(this);
		let session: McpSession | undefined;

		try {
			for (let itemIndex = 0; itemIndex < items.length; itemIndex++) {
				const pairedItem = { item: itemIndex };
				let failure: string | undefined;
				try {
					const operation = this.getNodeParameter('operation', itemIndex, 'callTool') as string;
					const options = this.getNodeParameter('options', itemIndex, {}) as IDataObject;
					const timeoutMs =
						typeof options.timeout === 'number' && options.timeout >= 1000
							? options.timeout
							: DEFAULT_TIMEOUT_MS;
					if (operation === 'listTools') {
						if (!session) {
							session = await openSession(send, {
								...connection,
								timeoutMs,
								clientVersion: CLIENT_VERSION,
							});
						}
						const tools = visibleTools(await session.listTools());
						for (const tool of tools) {
							returnData.push({
								json: {
									name: tool.name,
									description: tool.description,
									readOnly: isReadOnly(tool),
								},
								pairedItem: { item: 0 },
							});
						}
						// listTools is answered once, whatever the number of input items.
						break;
					}
					const tool = this.getNodeParameter('tool', itemIndex, '') as string;
					const reason = this.getNodeParameter('reason', itemIndex, '') as string;
					const rawArguments = this.getNodeParameter('arguments', itemIndex, '{}') as unknown;
					let parsed: unknown = rawArguments;
					if (typeof rawArguments === 'string') {
						try {
							parsed = rawArguments.trim() === '' ? {} : JSON.parse(rawArguments);
						} catch {
							parsed = undefined;
						}
					}
					if (!isRecord(parsed)) {
						failure = BAD_ARGUMENTS;
					} else {
						if (!session) {
							session = await openSession(send, {
								...connection,
								timeoutMs,
								clientVersion: CLIENT_VERSION,
							});
						}
						const mapped = toItem(await session.callTool(tool, { ...parsed, reason }));
						if ('error' in mapped) failure = mapped.error;
						else returnData.push({ json: mapped.json as IDataObject, pairedItem });
					}
				} catch (error) {
					failure = error instanceof McpTransportError ? error.message : UNEXPECTED;
				}
				if (failure !== undefined) {
					if (this.continueOnFail()) {
						returnData.push({ json: { error: failure }, pairedItem });
					} else {
						throw new NodeOperationError(this.getNode(), failure, {
							itemIndex,
						});
					}
				}
			}
		} finally {
			if (session) await session.close();
		}
		return [returnData];
	}
}
