// In-app modals in place of the browser's confirm() and prompt(): one at a time, the page
// dimmed behind. Shown by <Dialogs /> in the layout.

export type Ask = {
	message: string;
	ok: string;
	danger: boolean;
	input: 'password' | 'text' | null;
	resolve: (answer: string | null) => void;
};

export const dialogs = $state<{ queue: Ask[] }>({ queue: [] });

function open(a: Omit<Ask, 'resolve'>): Promise<string | null> {
	return new Promise((resolve) => dialogs.queue.push({ ...a, resolve }));
}

/** Yes or no. `ok` names the action ("Delete"); `danger` styles it as destructive. */
export async function ask(message: string, ok = 'OK', danger = false): Promise<boolean> {
	return (await open({ message, ok, danger, input: null })) !== null;
}

/** A line of text, or null when cancelled. */
export function askText(
	message: string,
	{ ok = 'OK', password = false }: { ok?: string; password?: boolean } = {}
): Promise<string | null> {
	return open({ message, ok, danger: false, input: password ? 'password' : 'text' });
}

/** Answer the one showing (the dialog calls this). */
export function answer(value: string | null) {
	const a = dialogs.queue.shift();
	a?.resolve(value);
}
