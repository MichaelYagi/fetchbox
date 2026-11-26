// Media kinds as the pages show them: the same list as fetchbox.core.kinds on the server.
// Movies, TV and music are built in, always first and in this order. Plugin kinds join
// this list later (from the server), and the pages that read it show them the same way.

export type CoreKind = 'movie' | 'series' | 'music';

export type MediaKind = {
	key: string;
	tab: string; // the Add page's tab: "TV shows"
	noun: string; // "Add a TV show"
	label: string; // a title's kind, capitalised: "TV show"
	plural: string; // "No TV shows found"
	placeholder: string; // the search box
	folder: string; // the add dialog's folder legend: "Show folder"
	possessive: string; // "looks like the show's own folder"
	profileMedia: 'video' | 'music'; // which quality profiles fit
	defaultProfile: string; // picked first when adding
};

export const CORE_KINDS: readonly (MediaKind & { key: CoreKind })[] = [
	{
		key: 'movie',
		tab: 'Movies',
		noun: 'movie',
		label: 'Movie',
		plural: 'movies',
		placeholder: 'Movie title, e.g. Dune',
		folder: 'Movie folder',
		possessive: "movie's",
		profileMedia: 'video',
		defaultProfile: '1080p'
	},
	{
		key: 'series',
		tab: 'TV shows',
		noun: 'TV show',
		label: 'TV show',
		plural: 'TV shows',
		placeholder: 'Show title, e.g. Severance',
		folder: 'Show folder',
		possessive: "show's",
		profileMedia: 'video',
		defaultProfile: '1080p'
	},
	{
		key: 'music',
		tab: 'Music',
		noun: 'music artist',
		label: 'Music artist',
		plural: 'music artists',
		placeholder: 'Artist, e.g. Massive Attack',
		folder: 'Artist folder',
		possessive: "artist's",
		profileMedia: 'music',
		defaultProfile: 'Lossless'
	}
];

const BY_KEY = new Map<string, MediaKind>(CORE_KINDS.map((k) => [k.key, k]));

export function kindOf(key: string): MediaKind {
	const k = BY_KEY.get(key);
	if (!k) throw new Error(`${key}: not a media kind`);
	return k;
}

/** Which quality profiles fit a title of this kind. */
export function profileMedia(key: string): 'video' | 'music' {
	return BY_KEY.get(key)?.profileMedia ?? 'video';
}
