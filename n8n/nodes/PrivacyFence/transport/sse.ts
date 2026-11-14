/** Parse a `text/event-stream` body into the JSON values of its events. */
export function parseSse(body: string): unknown[] {
	const messages: unknown[] = [];
	const lines = body.split(/\r\n|\n|\r/);
	let data: string[] = [];

	const flush = (): void => {
		if (data.length === 0) return;
		const joined = data.join('\n');
		data = [];
		try {
			messages.push(JSON.parse(joined));
		} catch {
			// An event that is not JSON is skipped.
		}
	};

	for (const line of lines) {
		if (line === '') {
			flush();
		} else if (line.startsWith(':')) {
			// Comment line.
		} else if (line === 'data' || line.startsWith('data:')) {
			let value = line.slice(4);
			if (value.startsWith(':')) value = value.slice(1);
			if (value.startsWith(' ')) value = value.slice(1);
			data.push(value);
		}
	}
	flush();
	return messages;
}
