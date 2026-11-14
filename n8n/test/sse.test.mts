import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseSse } from '../nodes/PrivacyFence/transport/sse.ts';

test('parses one event', () => {
	assert.deepEqual(parseSse('data: {"a":1}\n\n'), [{ a: 1 }]);
});

test('parses several events, with and without a trailing blank line', () => {
	assert.deepEqual(parseSse('data: 1\n\ndata: 2'), [1, 2]);
});

test('joins multiple data lines with a newline', () => {
	assert.deepEqual(parseSse('data: {"a":\ndata: 1}\n\n'), [{ a: 1 }]);
});

test('tolerates CRLF and bare CR', () => {
	assert.deepEqual(parseSse('event: message\r\ndata: {"a":1}\r\n\r\ndata: 2\r\r'), [{ a: 1 }, 2]);
});

test('skips comment lines and events without data', () => {
	assert.deepEqual(parseSse(': ping\n\nevent: x\nid: 3\n\n: c\ndata: 5\n\n'), [5]);
});

test('skips events whose data is not JSON', () => {
	assert.deepEqual(parseSse('data: nope\n\ndata: {"ok":true}\n\n'), [{ ok: true }]);
});

test('accepts data without a space and a bare data field', () => {
	assert.deepEqual(parseSse('data:7\n\ndata\n\n'), [7]);
});

test('an empty body has no messages', () => {
	assert.deepEqual(parseSse(''), []);
});
