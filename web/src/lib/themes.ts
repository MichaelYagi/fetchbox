// Colour themes: the tokens are in app.css ([data-theme='…']); the choice is per browser.
export const THEMES = [
	{ id: 'dark', name: 'Dark', swatch: ['#0f1216', '#5b8def', '#e6e9ee'] },
	{ id: 'light', name: 'Light', swatch: ['#ffffff', '#2563eb', '#1c2129'] },
	{ id: 'nord', name: 'Nord', swatch: ['#2e3440', '#88c0d0', '#e5e9f0'] },
	{ id: 'carnival', name: 'Carnival', swatch: ['#1a1025', '#ffb627', '#ff6ec7'] },
	{ id: 'garbagefire', name: 'Garbagefire', swatch: ['#ffff00', '#ff00ff', '#0000ff'] }
] as const;
export type ThemeId = (typeof THEMES)[number]['id'];

const KEY = 'fetchbox-theme';

export function currentTheme(): ThemeId {
	let saved: string | null = null;
	try {
		saved = localStorage.getItem(KEY);
	} catch {
		/* storage blocked */
	}
	return THEMES.find((t) => t.id === saved)?.id ?? 'dark';
}

export function applyTheme(id: ThemeId) {
	document.documentElement.setAttribute('data-theme', id);
	try {
		localStorage.setItem(KEY, id);
	} catch {
		/* storage blocked: this page only */
	}
}
