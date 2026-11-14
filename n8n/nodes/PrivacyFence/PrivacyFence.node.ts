import type {
	ICredentialTestFunctions,
	ICredentialsDecrypted,
	IExecuteFunctions,
	INodeCredentialTestResult,
	INodeExecutionData,
	INodePropertyOptions,
	INodeType,
	INodeTypeDescription,
	ILoadOptionsFunctions,
} from 'n8n-workflow';
import { NodeConnectionTypes } from 'n8n-workflow';

async function getTools(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
	throw new Error('not implemented');
}

async function privacyFenceApiTest(
	this: ICredentialTestFunctions,
	credential: ICredentialsDecrypted,
): Promise<INodeCredentialTestResult> {
	throw new Error('not implemented');
}

export class PrivacyFence implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'PrivacyFence',
		name: 'privacyFence',
		icon: { light: 'file:privacyfence.svg', dark: 'file:privacyfence.dark.svg' },
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
				displayName: 'Tool',
				name: 'tool',
				type: 'options',
				typeOptions: { loadOptionsMethod: 'getTools' },
				required: true,
				default: '',
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
		throw new Error('not implemented');
	}
}
