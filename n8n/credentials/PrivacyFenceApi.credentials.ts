import type { ICredentialType, INodeProperties } from 'n8n-workflow';

export class PrivacyFenceApi implements ICredentialType {
	name = 'privacyFenceApi';

	displayName = 'PrivacyFence API';

	documentationUrl = 'https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md';

	icon = {
		light: 'file:../nodes/PrivacyFence/privacyfence.svg',
		dark: 'file:../nodes/PrivacyFence/privacyfence.dark.svg',
	} as const;

	properties: INodeProperties[] = [
		{
			displayName: 'PrivacyFence MCP Address',
			name: 'mcpUrl',
			type: 'string',
			required: true,
			default: '',
			placeholder: 'https://privacyfence.example.org/mcp',
		},
		{
			displayName: 'Token',
			name: 'token',
			type: 'string',
			typeOptions: { password: true },
			required: true,
			default: '',
		},
	];
}
