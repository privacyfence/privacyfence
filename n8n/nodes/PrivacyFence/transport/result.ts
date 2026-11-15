import type { CallToolResult } from './mcpClient';

export const FILE_BRIDGE_META = 'privacyfence.eu/file-bridge';

export const FILE_BRIDGE_ERROR =
	"This tool needs a file from the person's computer, which n8n cannot provide.";
export const EMPTY_ERROR = 'PrivacyFence returned an error without a message.';

const MAX_ERROR_LENGTH = 1000;

/** Remove control characters (optionally keeping newlines) and cut to `max` characters. */
export function cleanText(text: string, max: number, keepNewlines = false): string {
	let out = '';
	for (const char of text) {
		const code = char.codePointAt(0) ?? 0;
		const isControl = code < 0x20 || (code >= 0x7f && code <= 0x9f);
		if (isControl && !(keepNewlines && code === 0x0a)) continue;
		out += char;
	}
	return out.length > max ? out.slice(0, max) : out;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function toItem(result: CallToolResult): { json: Record<string, unknown> } | { error: string } {
	const meta = result._meta;
	if (isPlainObject(meta) && FILE_BRIDGE_META in meta) {
		return { error: FILE_BRIDGE_ERROR };
	}

	const blocks = Array.isArray(result.content) ? result.content : [];
	const textBlocks = blocks.filter((b) => b.type === 'text' && typeof b.text === 'string');

	if (result.isError === true) {
		const joined = textBlocks.map((b) => b.text as string).join('\n');
		const message = cleanText(joined, MAX_ERROR_LENGTH, true);
		return { error: message === '' ? EMPTY_ERROR : message };
	}

	if (isPlainObject(result.structuredContent)) {
		return { json: result.structuredContent };
	}

	const json: Record<string, unknown> = {};
	if (textBlocks.length > 0) {
		const text = textBlocks[0].text as string;
		let parsed: unknown;
		let isJson = true;
		try {
			parsed = JSON.parse(text);
		} catch {
			isJson = false;
		}
		if (!isJson) json.text = text;
		else if (isPlainObject(parsed)) Object.assign(json, parsed);
		else json.result = parsed;
	}
	const nonText = blocks.length - textBlocks.length;
	if (nonText > 0) json.nonTextBlocks = nonText;
	return { json };
}
