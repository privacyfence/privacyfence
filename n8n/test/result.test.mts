import assert from 'node:assert/strict';
import { test } from 'node:test';

import { cleanText, toItem } from '../nodes/PrivacyFence/transport/result.ts';

const text = (t: string) => ({ type: 'text', text: t });

test('rule 1: the file bridge key is an error, before anything else', () => {
	const r = toItem({
		content: [text('{"a":1}')],
		isError: true,
		structuredContent: { a: 1 },
		_meta: { 'privacyfence.eu/file-bridge': { id: 1 } },
	});
	assert.deepEqual(r, {
		error: "This tool needs a file from the person's computer, which n8n cannot provide.",
	});
});

test('rule 2: isError joins text blocks with a newline', () => {
	assert.deepEqual(
		toItem({ content: [text('Request denied'), { type: 'image' }, text('by rule')], isError: true }),
		{ error: 'Request denied\nby rule' },
	);
});

test('rule 2: control characters are removed and the text is cut to 1000', () => {
	const r = toItem({ content: [text('a\u0000b\u001bc\u0085d' + 'x'.repeat(2000))], isError: true });
	assert.ok('error' in r);
	assert.equal(r.error.length, 1000);
	assert.ok(r.error.startsWith('abcdxxx'));
});

test('rule 2: an error without text still has a message', () => {
	assert.deepEqual(toItem({ content: [], isError: true }), {
		error: 'PrivacyFence returned an error without a message.',
	});
});

test('rule 3: structuredContent object is the item', () => {
	assert.deepEqual(toItem({ content: [text('ignored')], structuredContent: { a: 1 } }), {
		json: { a: 1 },
	});
});

test('rule 3: a structuredContent that is not an object falls through', () => {
	assert.deepEqual(toItem({ content: [text('{"b":2}')], structuredContent: [1] }), {
		json: { b: 2 },
	});
	assert.deepEqual(toItem({ content: [text('{"b":2}')], structuredContent: null }), {
		json: { b: 2 },
	});
});

test('rule 4: a JSON object text is the item', () => {
	assert.deepEqual(toItem({ content: [text('{"a":1}')] }), { json: { a: 1 } });
});

test('rule 4: an array or scalar becomes result', () => {
	assert.deepEqual(toItem({ content: [text('[1,2]')] }), { json: { result: [1, 2] } });
	assert.deepEqual(toItem({ content: [text('5')] }), { json: { result: 5 } });
	assert.deepEqual(toItem({ content: [text('null')] }), { json: { result: null } });
});

test('rule 4: text that is not JSON becomes text', () => {
	assert.deepEqual(toItem({ content: [text('hello')] }), { json: { text: 'hello' } });
	assert.deepEqual(toItem({ content: [text('')] }), { json: { text: '' } });
});

test('rule 4: only the first text block is read', () => {
	assert.deepEqual(toItem({ content: [text('{"a":1}'), text('{"b":2}')] }), { json: { a: 1 } });
});

test('rule 4: non-text blocks are counted', () => {
	assert.deepEqual(toItem({ content: [{ type: 'image' }, text('{"a":1}'), { type: 'audio' }] }), {
		json: { a: 1, nonTextBlocks: 2 },
	});
	assert.deepEqual(toItem({ content: [{ type: 'image' }] }), { json: { nonTextBlocks: 1 } });
	assert.deepEqual(toItem({ content: [] }), { json: {} });
});

test('missing content is tolerated', () => {
	assert.deepEqual(toItem({} as never), { json: {} });
});

test('cleanText keeps newlines only on request', () => {
	assert.equal(cleanText('a\nb\tc', 10), 'abc');
	assert.equal(cleanText('a\nb\tc', 10, true), 'a\nbc');
	assert.equal(cleanText('abcdef', 3), 'abc');
});
