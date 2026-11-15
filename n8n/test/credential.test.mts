import assert from 'node:assert/strict';
import { test } from 'node:test';

import { PrivacyFenceApi } from '../credentials/PrivacyFenceApi.credentials.ts';

const credential = new PrivacyFenceApi();

test('has exactly the properties mcpUrl and token', () => {
	assert.deepEqual(credential.properties.map((p) => p.name).sort(), ['mcpUrl', 'token']);
});

test('token is a password field', () => {
	const token = credential.properties.find((p) => p.name === 'token');
	assert.equal(token?.typeOptions?.password, true);
});

test('has a light and a dark icon', () => {
	const icon = credential.icon as { light?: string; dark?: string };
	assert.ok(icon.light);
	assert.ok(icon.dark);
});

test('has no authenticate and no test key', () => {
	assert.equal('authenticate' in credential, false);
	assert.equal('test' in credential, false);
});
