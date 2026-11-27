<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Badge from '#lib/Badge.svelte';
	import Path from '#lib/Path.svelte';
	import { profileMedia } from '#lib/kinds.ts';
	import ReleasesTable from '#lib/ReleasesTable.svelte';
	import PluginBits, { NO_FILE, pluginActions, runAction, type Act } from '#lib/PluginBits.svelte';
	import { ago, ApiError, bytes, del, duration, get, patch, post, put, type S, type Title } from '#lib/api.ts';

	const id = $derived(page.params.id ?? '');
	let title = $state<Title | null>(null);
	// The header's text, measured: the poster beside it is as tall (see .poster)
	let infoHeight = $state(0);
	// Under the name: runtime and genres, a dot only between what's there
	const details = $derived(
		title ? [title.runtime ? `${title.runtime} min` : null, title.genres.join(', ') || null].filter(Boolean) : []
	);
	let files = $state<S['FileOut'][]>([]);
	// where its files go (or went): its library folder and its own folder in there
	let folder = $state<S['TitleFolderOut'] | null>(null);
	let history = $state<S['HistoryOut'][]>([]);
	let profiles = $state<S['ProfileOut'][]>([]);
	let destinations = $state<S['DestinationOut'][]>([]);
	let releases = $state<S['InteractiveOut'] | null>(null);
	let releasesFor = $state('');
	let seasons = $state<S['SeasonOut'][]>([]);
	let open = $state<Record<number, boolean>>({});
	let albums = $state<S['AlbumOut'][]>([]);
	let openAlbum = $state<Record<string, boolean>>({});
	// Pictures from TMDB, when it has them: a scene behind the header, a frame per episode.
	// Fetched once per title (not with the 5-second refresh); nothing waits for them.
	let backdrop = $state<string | null>(null);
	let stills = $state<Record<string, string>>({});
	let picturesFor = '';
	$effect(() => {
		const t = title;
		if (!t || !t.tmdb_id || (t.kind !== 'movie' && t.kind !== 'series')) return;
		const key = `${t.kind}:${t.tmdb_id}`;
		if (key === picturesFor) return;
		picturesFor = key;
		backdrop = null;
		stills = {};
		const path = t.kind === 'movie' ? `/discovery/movies/${t.tmdb_id}` : `/discovery/series/${t.tmdb_id}`;
		get<S['MovieDetail'] | S['SeriesDetail']>(path)
			.then((d) => {
				if (picturesFor !== key) return;
				backdrop = d.backdrop_urls?.[0] ?? null;
				const got: Record<string, string> = {};
				for (const season of 'seasons' in d ? d.seasons : [])
					for (const ep of season.episodes ?? []) if (ep.still_url) got[`${ep.season}:${ep.number}`] = ep.still_url;
				stills = got;
			})
			.catch(() => {});
	});
	let editions = $state<Record<string, S['EditionOut'][]>>({});
	let showKinds = $state<Record<string, boolean>>({});
	let error = $state('');
	let message = $state('');
	let busy = $state('');
	// Plugins' actions for one episode (none when no plugin offers any)
	let itemActions = $state<Act[]>([]);
	$effect(() => {
		pluginActions().then((a) => (itemActions = a.filter((x) => x.on === 'item')));
	});
	// only the ones for this kind of title (Subtitles: movies and shows, not comics)
	const forHere = (a: Act) => !a.kinds || (!!title && a.kinds.includes(title.kind));
	const itemActionsHere = $derived(itemActions.filter(forHere));
	// a season's (a group's): beside its Choose a release
	let groupActions = $state<Act[]>([]);
	$effect(() => {
		pluginActions().then((a) => (groupActions = a.filter((x) => x.on === 'group')));
	});
	const groupActionsHere = $derived(groupActions.filter(forHere));
	// the last column grows to hold them, rather than pushing the table past the screen
	// (each button: its label at the small size, plus its padding and the gap)
	const actWidth = $derived(
		itemActionsHere.length ? `${5.5 + itemActionsHere.reduce((w, a) => w + a.label.length * 0.5 + 1.75, 0)}rem` : null
	);
	// A plugin's button on a row: its result in a notice at the bottom of the screen, seen
	// wherever on the page it was pressed
	let toast = $state('');
	let toastTimer: ReturnType<typeof setTimeout> | undefined;
	async function runItem(a: Act, item: string) {
		clearTimeout(toastTimer);
		toast = `${a.label}: working…`;
		try {
			toast = await runAction(a, item);
		} catch (e) {
			toast = e instanceof ApiError ? e.message : String(e);
		}
		toastTimer = setTimeout(() => (toast = ''), 10000);
	}
	// Change: another folder for it (full path), moving what's there or not
	let editingFolder = $state(false);
	// The poster's height follows the header's text, but not the folder form: opening it
	// (Change) would otherwise make the poster grow
	let posterHeight = $state(0);
	$effect(() => {
		if (!editingFolder) posterHeight = infoHeight;
	});
	let newFolder = $state('');
	let moveFiles = $state(true);

	async function saveFolder(e: SubmitEvent) {
		e.preventDefault();
		busy = 'folder';
		error = '';
		try {
			const r = await put<S['FolderChangeOut']>(`/titles/${id}/folder`, { path: newFolder.trim(), move: moveFiles });
			editingFolder = false;
			message = r.failed.length
				? `Folder changed; ${r.moved} files moved, ${r.failed.length} couldn't be (left where they were): ${r.failed[0].error}`
				: r.moved
					? `Folder changed; ${r.moved} files moved.`
					: 'Folder changed: new files go there.';
			await load();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = '';
		}
	}

	const series = $derived(title?.kind === 'series');
	const music = $derived(title?.kind === 'music');
	// A plugin media kind's title (a comic series): its items, as the plugin listed them
	const pluginTitle = $derived(!!title && !['movie', 'series', 'music'].includes(title.kind));
	let pluginItems = $state<S['PluginItemOut'][]>([]);
	let itemWord = $state('item'); // the kind's word for them: "issue"
	$effect(() => {
		if (pluginTitle && title)
			get<S['MediaKindOut'][]>('/plugins/kinds')
				.then((ks) => (itemWord = ks.find((k) => k.id === title?.kind)?.levels.at(-1) ?? 'item'))
				.catch(() => {});
	});
	$effect(() => {
		if (pluginTitle && title) get<S['PluginItemOut'][]>(`/plugins/titles/${title.id}/items`).then((x) => (pluginItems = x)).catch(() => {});
	});
	const refreshPlugin = () => act('refresh', async () => {
		await post(`/plugins/titles/${id}/refresh`);
		pluginItems = await get<S['PluginItemOut'][]>(`/plugins/titles/${id}/items`);
	});
	// Which of the kind's qualities this title takes, and when it's good enough
	let pq = $state<S['TitleQualities'] | null>(null);
	$effect(() => {
		if (pluginTitle && title) get<S['TitleQualities']>(`/plugins/titles/${title.id}/qualities`).then((q) => (pq = q)).catch(() => {});
	});
	const savePq = () => act('qualities', async () => {
		if (pq) pq = await put<S['TitleQualities']>(`/plugins/titles/${id}/qualities`, { qualities: pq.qualities, cutoff: pq.cutoff });
		message = 'Saved';
	});
	const togglePq = (q: string, on: boolean) => {
		if (!pq) return;
		const set = on ? [...pq.qualities, q] : pq.qualities.filter((x) => x !== q);
		const qualities = pq.available.filter((x) => set.includes(x));
		pq = { ...pq, qualities, cutoff: qualities.includes(pq.cutoff) ? pq.cutoff : (qualities[0] ?? pq.cutoff) };
	};
	// Search all missing, or the items given: what happened, said beside the button
	let searchNote = $state('');
	const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;
	const searchPlugin = (itemIds: string[] | null = null) =>
		act('search', async () => {
			searchNote = '';
			const r = await post<S['PluginSearchOut']>(`/plugins/titles/${id}/search`, itemIds ? { item_ids: itemIds } : {});
			searchNote = !r.searched
				? `Nothing to search: no released ${itemWord} is wanted and missing`
				: r.grabbed
					? `Searched ${plural(r.searched, itemWord)}: grabbed ${plural(r.grabbed, 'release')} (see Queue)`
					: r.results
						? `Searched ${plural(r.searched, itemWord)}: ${plural(r.results, 'release')} found, none fitting (the quality or the ${itemWord}s)`
						: `Searched ${plural(r.searched, itemWord)}: nothing found on your indexers`;
			pluginItems = await get<S['PluginItemOut'][]>(`/plugins/titles/${id}/items`);
		});
	const monitorAll = (on: boolean) =>
		act('monitor', async () => {
			await put(`/plugins/titles/${id}/monitored`, { monitored: on });
			pluginItems = await get<S['PluginItemOut'][]>(`/plugins/titles/${id}/items`);
		});
	const monitorItem = (i: S['PluginItemOut'], on: boolean) =>
		act('monitor', async () => {
			await patch(`/wanted/${i.id}`, { monitored: on });
			pluginItems = await get<S['PluginItemOut'][]>(`/plugins/titles/${id}/items`);
		});
	const wanted = $derived(series || music ? null : (title?.wanted[0] ?? null));
	const profileName = $derived(profiles.find((p) => p.id === title?.profile_id)?.name ?? '');

	async function load() {
		try {
			const [t, f, h] = await Promise.all([
				get<Title>(`/titles/${id}`),
				get<S['FileOut'][]>(`/titles/${id}/files`),
				get<S['HistoryPage']>(`/titles/${id}/history`, { limit: 50 })
			]);
			title = t;
			files = f;
			history = h.items;
			get<S['TitleFolderOut']>(`/titles/${id}/folder`)
				.then((x) => (folder = x))
				.catch(() => (folder = null)); // no library folder for its kind yet
			if (t.kind === 'music') albums = await get<S['AlbumOut'][]>(`/titles/${id}/albums`);
			if (t.kind === 'series') {
				seasons = await get<S['SeasonOut'][]>(`/titles/${id}/episodes`);
				if (Object.keys(open).length === 0) {
					// open the newest season with something to do, else the newest
					const regular = seasons.filter((x) => x.number > 0);
					const todo = [...regular].reverse().find((x) => x.episodes.some((e) => e.wanted.state !== 'acquired' && e.wanted.monitored));
					const pick = todo ?? regular[regular.length - 1];
					if (pick) open = { [pick.number]: true };
				}
			}
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}

	$effect(() => {
		void id;
		load();
		get<S['ProfileOut'][]>('/quality-profiles').then((p) => (profiles = p));
		get<S['DestinationOut'][]>('/destinations').then((d) => (destinations = d));
		const t = setInterval(load, 5000);
		return () => clearInterval(t);
	});

	async function act(name: string, fn: () => Promise<unknown>, done?: string) {
		busy = name;
		error = '';
		message = '';
		try {
			await fn();
			if (done) message = done;
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const searchNow = () =>
		act('search', () => post(`/titles/${id}/search`), 'Searching every indexer; the best release is grabbed automatically.');
	const interactive = () =>
		act('interactive', async () => {
			releasesFor = '';
			releases = await get<S['InteractiveOut']>('/releases', { wanted_item_id: wanted!.id });
		});
	const reveal = () => setTimeout(() => document.getElementById('releases')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
	const pad = (n: number) => String(n).padStart(2, '0');
	const seasonName = (n: number) => (n === 0 ? 'Specials' : `Season ${n}`);
	const chooseSeason = (n: number) =>
		act('interactive', async () => {
			releases = null;
			releasesFor = `${seasonName(n)}: season packs first, then single episodes`;
			releases = await get<S['InteractiveOut']>('/releases', { title_id: id, season: n });
			reveal();
		});
	const chooseEpisode = (sn: number, e: S['EpisodeOut']) =>
		act('interactive', async () => {
			releases = null;
			releasesFor = `S${pad(sn)}E${pad(e.number)}${e.name ? ` · ${e.name}` : ''}`;
			releases = await get<S['InteractiveOut']>('/releases', { wanted_item_id: e.wanted.id });
			reveal();
		});
	const grab = (r: S['ReleaseOut']) =>
		act('grab', async () => {
			const item = releases?.wanted_item_id;
			await post('/grabs', {
				release_id: r.release_id,
				...(item ? { wanted_item_ids: [item] } : {}),
				allow_rejected: !r.accepted
			});
			releases = null;
		}, `Grabbed ${r.candidate.title}.`);
	const searchSeason = (n: number) =>
		act('search', () => post(`/titles/${id}/seasons/${n}/search`), `Searching ${seasonName(n)}: a season pack if there is one, else each episode.`);
	const monitorSeason = (n: number, on: boolean) =>
		act('monitor', () => patch(`/titles/${id}/seasons/${n}`, { monitored: on }));
	const monitorEpisode = (w: S['WantedOut'], on: boolean) =>
		act('monitor', () => patch(`/wanted/${w.id}`, { monitored: on }, w.etag));
	const setMode = (mode: string) =>
		act('mode', () => patch(`/titles/${id}`, { monitor_mode: mode }, title!.etag));
	const setAvailability = (v: string) =>
		act('availability', () => patch(`/titles/${id}`, { minimum_availability: v }, title!.etag));
	const aired = (e: S['EpisodeOut']) => !!e.air_date && e.air_date <= new Date().toISOString().slice(0, 10);
	function epState(e: S['EpisodeOut']): string {
		if (e.wanted.state === 'missing' && !aired(e)) return e.air_date ? `Airs ${e.air_date}` : 'Not aired';
		return '';
	}
	function seasonCount(x: S['SeasonOut']): string {
		const got = x.episodes.filter((e) => e.wanted.state === 'acquired').length;
		return `${got} of ${x.episodes.length}`;
	}
	// ---- music ----
	const ARTIST_MODES: [string, string][] = [
		['all', 'All their albums'],
		['future', 'Only new albums'],
		['latest', 'The latest album, and new ones'],
		['none', 'Nothing']
	];
	const KIND_NAMES: [string, string][] = [
		['album', 'Albums'],
		['ep', 'EPs'],
		['single', 'Singles'],
		['live', 'Live'],
		['compilation', 'Compilations'],
		['other', 'Remixes, soundtracks and more']
	];
	const albumKinds = $derived(
		KIND_NAMES.map(([k, l]) => ({ kind: k, label: l, items: albums.filter((a) => a.kind === k) })).filter((g) => g.items.length)
	);
	// a kind is shown open when one of its albums is wanted
	const kindOpen = (k: string, items: S['AlbumOut'][]) => showKinds[k] ?? items.some((a) => a.wanted.monitored);
	const released = (a: S['AlbumOut']) => !!a.release_date && a.release_date <= new Date().toISOString().slice(0, 10);
	const fmtLen = (ms: number | null) => (ms ? `${Math.floor(ms / 60000)}:${String(Math.round((ms % 60000) / 1000)).padStart(2, '0')}` : '');
	const albumLen = (a: S['AlbumOut']) => fmtLen(a.tracks.reduce((n, t) => n + (t.length_ms ?? 0), 0));
	const monitorAlbum = (a: S['AlbumOut'], on: boolean) =>
		act('monitor', () => patch(`/wanted/${a.wanted.id}`, { monitored: on }, a.wanted.etag));
	const searchAlbum = (a: S['AlbumOut']) =>
		act('search', () => post(`/wanted/${a.wanted.id}/search`), `Searching for ${a.name}; the best release is grabbed automatically.`);
	const chooseAlbum = (a: S['AlbumOut']) =>
		act('interactive', async () => {
			releases = null;
			releasesFor = a.name;
			releases = await get<S['InteractiveOut']>('/releases', { wanted_item_id: a.wanted.id });
			reveal();
		});
	async function toggleAlbum(a: S['AlbumOut']) {
		openAlbum = { ...openAlbum, [a.id]: !openAlbum[a.id] };
		if (openAlbum[a.id] && !editions[a.id] && a.editions) {
			try {
				editions = { ...editions, [a.id]: await get<S['EditionOut'][]>(`/albums/${a.id}/editions`) };
			} catch {
				/* the track list still shows */
			}
		}
	}
	const chooseEdition = (a: S['AlbumOut'], mbid: string) =>
		act('edition', () => put(`/albums/${a.id}/edition`, { edition_mbid: mbid }), 'Edition changed: its track list is now the one wanted.');
	const editionLabel = (e: S['EditionOut']) =>
		[e.date?.slice(0, 4), e.country, e.formats.join('+'), `${e.tracks} tracks`, fmtLen(e.length_ms)].filter(Boolean).join(' · ');

	const MODES: [string, string][] = [
		['all', 'All episodes'],
		['future', 'Only new episodes'],
		['missing', 'Episodes without a file, and new ones'],
		['first_season', 'First season'],
		['latest_season', 'Latest season'],
		['none', 'Nothing']
	];
	const AVAIL: [string, string][] = [
		['released', 'Once released (digital or disc)'],
		['in_cinemas', 'Once in cinemas'],
		['announced', 'Right away']
	];
	const monitor = (on: boolean) =>
		act('monitor', () => patch(`/wanted/${wanted!.id}`, { monitored: on }, wanted!.etag));
	const setProfile = (pid: string) =>
		act('profile', () => patch(`/titles/${id}`, { profile_id: pid }, title!.etag));
	const unlock = () =>
		act('unlock', () => post(`/wanted/${wanted!.id}/unlock`), 'Unlocked: Fetchbox may grab an upgrade.');
	// A bad copy, gone: to the library folder's recycle folder (emptied after the days in
	// Settings), dropped from Plex; nothing is downloaded instead (Redownload does that)
	const deleteFile = async (f: S['FileOut']) => {
		const name = f.relative_path.split('/').pop();
		const ok = await ask(
			`Delete ${name}?\n\nIt goes to the library folder's recycle folder (deleted for good after the days set in Settings > Library), and Plex drops it. Nothing is downloaded instead: use Unlock or Redownload for that.`,
			'Delete',
			true
		);
		if (ok) act('delete', () => del(`/files/${f.id}`), 'Deleted.');
	};
	const redownload = async () => {
		if (!(await ask('Blocklist the current release, move the file to the recycle folder, and download again?', 'Download again', true))) return;
		act('redownload', () => post(`/wanted/${wanted!.id}/redownload`), 'Searching for a new copy.');
	};
	const remove = async () => {
		if (!(await ask(`Remove ${title!.name} from Fetchbox? Files on disk are kept.`, 'Remove', true))) return;
		act('delete', async () => {
			await del(`/titles/${id}`, undefined, title!.etag);
			await goto('/');
		});
	};

	function fullPath(f: S['FileOut']): string {
		const dest = destinations.find((d) => d.id === f.destination_id);
		if (!dest) return f.relative_path;
		const sep = dest.path.includes('\\') && !dest.path.includes('/') ? '\\' : '/';
		return dest.path.replace(/[\\/]+$/, '') + sep + f.relative_path;
	}

	const EVENTS: Record<string, string> = {
		grabbed: 'Grabbed',
		imported: 'Imported',
		upgraded: 'Upgraded',
		failed: 'Download failed',
		deleted: 'File deleted',
		unlocked: 'Unlocked'
	};
	// One row per event; a grab or import of several episodes at once shows once, counted.
	const grouped = $derived.by(() => {
		const out: { h: S['HistoryOut']; n: number }[] = [];
		for (const h of history) {
			const last = out[out.length - 1];
			const d = h.data as Record<string, unknown>;
			const ld = last?.h.data as Record<string, unknown> | undefined;
			if (last && last.h.type === h.type && ld?.release_title === d.release_title && ld?.path === d.path && ld?.reason === d.reason) last.n++;
			else out.push({ h, n: 1 });
		}
		return out;
	});
	function describe(h: S['HistoryOut']): string {
		const d = h.data as Record<string, unknown>;
		return [d.release_title, d.quality, d.reason, d.path].filter(Boolean).join(' · ');
	}
</script>

<main>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}

	{#if title === null}
		{#if !error}<p class="muted">Loading…</p>{/if}
	{:else}
		<div class="head" class:backdrop={!!backdrop} style:--backdrop={backdrop ? `url("${backdrop}")` : null}>
			{#if title.poster_url}<img class="poster" class:square={title.kind === 'music'} src={title.poster_url} alt="" style:--info-height={posterHeight ? `${posterHeight}px` : null} />{/if}
			<div class="stack info" bind:clientHeight={infoHeight}>
				<div>
					<h1>{title.name} <span class="muted">{title.year ?? ''}</span></h1>
					<div class="row muted small">
						{#each details as d, i (i)}<span>{i ? '· ' : ''}{d}</span>{/each}
						{#if title.tmdb_id}
							<span>{details.length ? '· ' : ''}<a href="https://www.themoviedb.org/{series ? 'tv' : 'movie'}/{title.tmdb_id}" target="_blank" rel="noreferrer">TMDB</a></span>
						{/if}
					</div>
				</div>
				{#if title.overview}<p class="overview">{title.overview}</p>{/if}
				{#if folder}
					<!-- the title's folder, as this system sees it; Change moves it -->
					<div class="small where">
						<div class="where-head muted">
							<span>Folder{#if title.kind === 'music' && folder.path === folder.library_folder_path} (one folder per album){/if}</span>
							{#if !(title.kind === 'music' && folder.path === folder.library_folder_path) && !editingFolder}
								<button class="change" onclick={() => ((newFolder = folder!.path), (moveFiles = true), (editingFolder = true))}>Change</button>
							{/if}
						</div>
						<div><Path path={folder.path} /></div>
					</div>
					{#if editingFolder}
						<form class="card stack small" onsubmit={saveFolder}>
							<label class="stack">
								<span>{series ? 'Show folder' : music ? 'Artist folder' : 'Movie folder'} (full path)</span>
								<input bind:value={newFolder} required spellcheck="false" aria-label="New folder" />
							</label>
							<label>
								<input type="checkbox" bind:checked={moveFiles} /> Move what's in {folder.path} there
							</label>
							<span class="muted">
								{moveFiles
									? 'Everything in the folder moves (subtitles and artwork too); files a torrent seeds from move with it.'
									: "Files stay where they are; new ones go to the new folder."}
								New files go there whatever the naming says.
							</span>
							<div class="row">
								<button class="primary" disabled={busy === 'folder' || !newFolder.trim()}>{busy === 'folder' ? 'Moving…' : 'Save'}</button>
								<button type="button" onclick={() => (editingFolder = false)}>Cancel</button>
							</div>
						</form>
					{/if}
				{/if}
				{#if wanted}
					<div class="row">
						<Badge state={wanted.state} />
						{#if wanted.lock}<span class="badge" title="Automatic search leaves it alone">Locked</span>{/if}
						{#if wanted.state === 'acquired'}
							<span class="badge {wanted.cutoff_met ? 'ok' : 'warn'}">
								{wanted.cutoff_met ? 'Quality target met' : 'Below quality target'}
							</span>
						{/if}
						<span class="muted small">
							Searched {ago(wanted.last_search_at)}{wanted.next_search_at && wanted.state !== 'acquired' ? `, next ${ago(wanted.next_search_at)}` : ''}
						</span>
					</div>
					<div class="row">
						<label class="inline">
							Quality
							<select value={title.profile_id} onchange={(e) => setProfile(e.currentTarget.value)} disabled={!!busy}>
								{#each profiles.filter((x) => x.media === profileMedia(title?.kind ?? '')) as p (p.id)}<option value={p.id} selected={p.id === title.profile_id}>{p.name}</option>{/each}
							</select>
						</label>
						<label class="inline">
							<input type="checkbox" checked={wanted.monitored} onchange={(e) => monitor(e.currentTarget.checked)} disabled={!!busy} />
							Monitored
						</label>
					</div>
					<div class="row">
						<button class="primary" onclick={searchNow} disabled={!!busy}>Search now</button>
						<button onclick={interactive} disabled={!!busy}>{busy === 'interactive' ? 'Searching…' : 'Choose a release'}</button>
						{#if wanted.state === 'acquired'}
							{#if wanted.lock}<button onclick={unlock} disabled={!!busy}>Unlock</button>{/if}
							<button onclick={redownload} disabled={!!busy}>Redownload</button>
						{/if}
						<button class="danger" onclick={remove} disabled={!!busy}>Remove</button>
					</div>
					<div class="row small muted">
						<label class="inline">
							Start searching
							<select value={title.minimum_availability} onchange={(e) => setAvailability(e.currentTarget.value)} disabled={!!busy}>
								{#each AVAIL as [v, l] (v)}<option value={v} selected={v === title.minimum_availability}>{l}</option>{/each}
							</select>
						</label>
						{#if title.available_from && wanted.state !== 'acquired'}
							<span>Automatic search waits until {title.available_from}.</span>
						{/if}
					</div>
					{#if title.theatrical_date || title.digital_date || title.physical_date}
						<div class="row small muted">
							{#if title.theatrical_date}<span>Cinemas {title.theatrical_date}</span>{/if}
							{#if title.digital_date}<span>· Digital {title.digital_date}</span>{/if}
							{#if title.physical_date}<span>· Disc {title.physical_date}</span>{/if}
						</div>
					{/if}
				{:else if music}
					{#if title.albums}
						<div class="row small">
							<span class="badge ok">{title.albums.acquired} downloaded</span>
							{#if title.albums.missing}<span class="badge warn">{title.albums.missing} missing</span>{/if}
							<span class="muted">{title.albums.monitored} of {title.albums.albums} albums wanted</span>
						</div>
					{/if}
					<div class="row">
						<label class="inline">
							Quality
							<select value={title.profile_id} onchange={(e) => setProfile(e.currentTarget.value)} disabled={!!busy}>
								{#each profiles.filter((x) => x.media === 'music') as p (p.id)}<option value={p.id} selected={p.id === title.profile_id}>{p.name}</option>{/each}
							</select>
						</label>
						<label class="inline">
							Download
							<select value={title.monitor_mode} onchange={(e) => setMode(e.currentTarget.value)} disabled={!!busy}>
								{#each ARTIST_MODES as [v, l] (v)}<option value={v} selected={v === title.monitor_mode}>{l}</option>{/each}
							</select>
						</label>
					</div>
					<div class="row">
						<button class="primary" onclick={searchNow} disabled={!!busy} title="Every wanted, released album still to get">Search all missing</button>
						{#if title.mbid}<a class="button" href="https://musicbrainz.org/artist/{title.mbid}" target="_blank" rel="noreferrer">MusicBrainz</a>{/if}
						<button class="danger" onclick={remove} disabled={!!busy}>Remove</button>
					</div>
				{:else if series}
					{#if title.episodes}
						<div class="row small">
							<span class="badge ok">{title.episodes.acquired} downloaded</span>
							{#if title.episodes.missing}<span class="badge warn">{title.episodes.missing} missing</span>{/if}
							<span class="muted">{title.episodes.monitored} of {title.episodes.episodes} episodes monitored</span>
						</div>
					{/if}
					<div class="row">
						<label class="inline">
							Quality
							<select value={title.profile_id} onchange={(e) => setProfile(e.currentTarget.value)} disabled={!!busy}>
								{#each profiles.filter((x) => x.media === profileMedia(title?.kind ?? '')) as p (p.id)}<option value={p.id} selected={p.id === title.profile_id}>{p.name}</option>{/each}
							</select>
						</label>
						<label class="inline">
							Download
							<select value={title.monitor_mode} onchange={(e) => setMode(e.currentTarget.value)} disabled={!!busy}>
								{#each MODES as [v, l] (v)}<option value={v} selected={v === title.monitor_mode}>{l}</option>{/each}
							</select>
						</label>
					</div>
					<div class="row">
						<button class="primary" onclick={searchNow} disabled={!!busy} title="Every season with aired episodes still to get">Search all missing</button>
						<button class="danger" onclick={remove} disabled={!!busy}>Remove</button>
					</div>
				{:else if pluginTitle}
					{#if pluginItems.length}
						<div class="row small">
							<span class="badge ok">{pluginItems.filter((i) => i.state === 'acquired').length} downloaded</span>
							<span class="muted">{pluginItems.filter((i) => i.monitored).length} of {pluginItems.length} wanted</span>
						</div>
					{/if}
					{#if pq}
						<div class="row small">
							<span class="muted">Quality:</span>
							{#each pq.available as q (q)}
								<label class="inline"><input type="checkbox" checked={pq.qualities.includes(q)} onchange={(e) => togglePq(q, e.currentTarget.checked)} disabled={!!busy || (pq.qualities.length === 1 && pq.qualities.includes(q))} /> {q}</label>
							{/each}
							<label class="inline">good enough at
								<select bind:value={pq.cutoff} disabled={!!busy}>{#each pq.qualities as q (q)}<option value={q}>{q}</option>{/each}</select>
							</label>
							<button class="small" onclick={savePq} disabled={!!busy}>Save</button>
						</div>
					{/if}
					<div class="row">
						<button class="primary" onclick={() => searchPlugin()} disabled={!!busy} title="Every wanted, released item still to get">{busy === 'search' ? 'Searching…' : 'Search all missing'}</button>
		<button onclick={refreshPlugin} disabled={!!busy} title="Ask the plugin again for its items">Refresh</button>
						<button class="danger" onclick={remove} disabled={!!busy}>Remove</button>
					</div>
					{#if busy === 'search'}<p class="muted small">Searching your indexers for each {itemWord}: this can take a minute.</p>{:else if searchNote}<p class="small">{searchNote}</p>{/if}
				{/if}
			</div>
		</div>

		{#if music}
			{#each albumKinds as g (g.kind)}
				<h2>
					<button class="link" onclick={() => (showKinds = { ...showKinds, [g.kind]: !kindOpen(g.kind, g.items) })} aria-expanded={kindOpen(g.kind, g.items)}>
						{kindOpen(g.kind, g.items) ? '▾' : '▸'} {g.label} <span class="muted small">{g.items.length}</span>
					</button>
				</h2>
				{#if kindOpen(g.kind, g.items)}
					{#each g.items as a (a.id)}
						<div class="card album" class:unmonitored={!a.wanted.monitored}>
							<div class="album-head">
								{#if a.cover_url}<img class="cover" src={a.cover_url} alt="" loading="lazy" onerror={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')} />{:else}<div class="cover"></div>{/if}
								<div class="grow">
									<button class="link" onclick={() => toggleAlbum(a)} aria-expanded={!!openAlbum[a.id]}>
										<strong>{a.name}</strong>
									</button>
									<div class="muted small">
										{a.release_date ?? 'No date'}{a.tracks.length ? ` · ${a.tracks.length} tracks · ${albumLen(a)}` : ''}
									</div>
								</div>
								<div class="row">
									{#if a.wanted.state === 'missing' && !released(a)}
										<span class="muted small">{a.release_date ? `Out ${a.release_date}` : 'Not out yet'}</span>
									{:else}<Badge state={a.wanted.state} />{/if}
									<input type="checkbox" title="Wanted" aria-label="Wanted" checked={a.wanted.monitored} onchange={(e) => monitorAlbum(a, e.currentTarget.checked)} disabled={!!busy} />
									<button class="small" onclick={() => searchAlbum(a)} disabled={!!busy || !released(a)}>Search</button>
									<button class="small" onclick={() => chooseAlbum(a)} disabled={!!busy || !released(a)}>Choose a release</button>
								</div>
							</div>
							{#if openAlbum[a.id]}
								{#if editions[a.id]?.length}
									<label class="inline small edition">
										Edition
										<select value={a.edition_mbid ?? ''} onchange={(e) => chooseEdition(a, e.currentTarget.value)} disabled={!!busy}>
											{#each editions[a.id] as e (e.mbid)}<option value={e.mbid} selected={e.mbid === a.edition_mbid}>{editionLabel(e)}</option>{/each}
										</select>
									</label>
								{/if}
								{#if a.tracks.length}
									<table class="tracks">
										<tbody>
											{#each a.tracks as t (t.id)}
												<tr>
													<td class="num muted">{a.tracks.some((x) => x.disc > 1) ? `${t.disc}-` : ''}{t.number}</td>
													<td>{t.name}</td>
													<td class="muted small">{fmtLen(t.length_ms)}</td>
												</tr>
											{/each}
										</tbody>
									</table>
								{:else}
									<p class="muted small">The track list is fetched from MusicBrainz when the album is wanted.</p>
								{/if}
							{/if}
						</div>
					{/each}
				{/if}
			{/each}
		{/if}

		{#if pluginTitle && pluginItems.length}
			<h2>{pluginItems.length} {itemWord}{pluginItems.length === 1 ? '' : 's'}</h2>
			<div class="table-wrap">
				<table class="episodes">
					<colgroup>
						<col class="c-num" /><col class="c-name" /><col class="c-date" /><col class="c-state" /><col class="c-mon" /><col class="c-act" style:--act-width={actWidth} /><col class="c-rest" />
					</colgroup>
					<thead>
						<!-- every item at once, lined up with theirs: wanted, and Search for all missing -->
						<tr class="season-head">
							<td colspan="4"><strong>All {itemWord}s</strong> <span class="muted small">{pluginItems.filter((i) => i.monitored).length} of {pluginItems.length} wanted</span></td>
							<td>
								<input type="checkbox" title="Every {itemWord} wanted, or none" aria-label="All {itemWord}s wanted" checked={pluginItems.every((i) => i.monitored)} indeterminate={pluginItems.some((i) => i.monitored) && !pluginItems.every((i) => i.monitored)} onchange={(e) => monitorAll(e.currentTarget.checked)} disabled={!!busy} />
							</td>
							<td class="act"><button class="small" onclick={() => searchPlugin()} disabled={!!busy} title="Every wanted, released {itemWord} still to get">Search</button></td>
							<td class="rest"></td>
						</tr>
					</thead>
					<tbody>
						{#each pluginItems as i (i.id)}
							<tr class:unmonitored={!i.monitored}>
								<td class="num muted">{i.number ?? ''}</td>
								<td>{i.name ?? `#${i.number ?? ''}`}</td>
								<td class="muted small date">{i.released ?? ''}</td>
								<td><Badge state={i.state} />{#if i.quality}<span class="muted small"> {i.quality}</span>{/if}</td>
								<td><input type="checkbox" title="Wanted" aria-label="Wanted" checked={i.monitored} onchange={(ev) => monitorItem(i, ev.currentTarget.checked)} disabled={!!busy} /></td>
								<td class="act">
									<button class="small" onclick={() => searchPlugin([i.id])} disabled={!!busy} title="Search for this {itemWord} and grab the best release">Search</button>
									{#each itemActionsHere as a (`${a.plugin}/${a.addon}/${a.method}`)}<button class="small plugin-act" onclick={() => runItem(a, i.id)} disabled={a.needs_file && i.state !== 'acquired'} title={a.needs_file && i.state !== 'acquired' ? NO_FILE : undefined}>{a.label}</button>{/each}
								</td>
								<td class="rest"></td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if series}
			<h2>Seasons</h2>
			{#each [...seasons].sort((a, b) => (a.number === 0 ? 1 : b.number === 0 ? -1 : b.number - a.number)) as x (x.number)}
				<div class="card season">
					<!-- the season's line shares the episodes' columns: its checkbox above theirs, its Search above theirs -->
					<div class="table-wrap">
						<table class="episodes">
							<colgroup>
								<col class="c-num" /><col class="c-name" /><col class="c-date" /><col class="c-state" /><col class="c-mon" /><col class="c-act" style:--act-width={actWidth} /><col class="c-rest" />
							</colgroup>
							<thead>
								<tr class="season-head">
									<td colspan="4">
										<div class="season-name">
											<button class="link" onclick={() => (open = { ...open, [x.number]: !open[x.number] })} aria-expanded={!!open[x.number]}>
												{open[x.number] ? '▾' : '▸'} <strong>{seasonName(x.number)}</strong>
												<span class="muted small">{seasonCount(x)} downloaded</span>
											</button>
											<button class="small" onclick={() => chooseSeason(x.number)} disabled={!!busy}><span class="long">Choose a release</span><span class="short">Choose</span></button>
											{#each groupActionsHere as a (`${a.plugin}/${a.addon}/${a.method}`)}
												{@const noFile = a.needs_file && !x.episodes.some((e) => e.wanted.state === 'acquired')}
												<button class="small" onclick={() => runItem(a, `${title!.id}:${x.number}`)} disabled={!!busy || noFile} title={noFile ? NO_FILE : undefined}>{a.label}</button>
											{/each}
										</div>
									</td>
									<td>
										<input type="checkbox" title="Monitor the whole season" aria-label="Season monitored" checked={x.monitored} onchange={(e) => monitorSeason(x.number, e.currentTarget.checked)} disabled={!!busy} />
									</td>
									<td class="act"><button class="small" onclick={() => searchSeason(x.number)} disabled={!!busy} title="Search for the season's missing episodes">Search</button></td>
									<td class="rest"></td>
								</tr>
							</thead>
							{#if open[x.number]}
								<tbody>
									{#each x.episodes as e (e.id)}
										<tr class:unmonitored={!e.wanted.monitored}>
											<td class="num muted">{e.number}</td>
											<td>
												<span class="ep-name">
													{#if stills[`${x.number}:${e.number}`]}<img class="still" src={stills[`${x.number}:${e.number}`]} alt="" loading="lazy" />{/if}
													<span>{e.name ?? `Episode ${e.number}`}</span>
												</span>
											</td>
											<td class="muted small date">{e.air_date ?? ''}</td>
											<td>
												{#if epState(e)}<span class="muted small">{epState(e)}</span>{:else}<Badge state={e.wanted.state} />{/if}
											</td>
											<td>
												<input type="checkbox" title="Monitored" aria-label="Monitored" checked={e.wanted.monitored} onchange={(ev) => monitorEpisode(e.wanted, ev.currentTarget.checked)} disabled={!!busy} />
											</td>
											<td class="act">
												<button class="small" onclick={() => chooseEpisode(x.number, e)} disabled={!!busy || !aired(e)} title="Search and choose a release">Search</button>
												{#each itemActionsHere as a (`${a.plugin}/${a.addon}/${a.method}`)}<button class="small plugin-act" onclick={() => runItem(a, e.wanted.id)} disabled={a.needs_file && e.wanted.state !== 'acquired'} title={a.needs_file && e.wanted.state !== 'acquired' ? NO_FILE : undefined}>{a.label}</button>{/each}
											</td>
											<td class="rest"></td>
										</tr>
									{/each}
								</tbody>
							{/if}
						</table>
					</div>
				</div>
			{/each}
		{/if}

		{#if busy === 'interactive'}<p class="muted">Searching every indexer…</p>{/if}
		{#if releases}
			<div class="spread" id="releases">
				<h2>Releases {#if releasesFor}<span class="muted small">for {releasesFor}</span>{/if}</h2>
				<button class="link" onclick={() => (releases = null)}>Close</button>
			</div>
			<ReleasesTable result={releases} busy={!!busy} onGrab={grab} />
		{/if}

		<PluginBits titleId={title.id} kind={title.kind} itemId={title.kind === 'movie' ? (title.wanted[0]?.id ?? null) : null} hasFile={files.length > 0} />

		<h2>Files</h2>
		{#if files.length === 0}
			<p class="muted">No file yet.</p>
		{:else}
			{#each files as f (f.id)}
				<div class="card stack file">
					<div><Path path={fullPath(f)} /></div>
					<div class="row small muted">
						<span class="badge">{f.quality.name}</span>
						<span>{bytes(f.size)}</span>
						{#if f.mediainfo}
							{@const m = f.mediainfo as Record<string, any>}
							<span>· {m.width}×{m.height} {m.video_codec ?? ''}</span>
							{#if m.hdr?.length}<span>· {m.hdr.join(' ')}</span>{/if}
							{#if m.audio_codecs?.length}<span>· {m.audio_codecs.join(', ')} {m.audio_channels ?? ''}</span>{/if}
							{#if m.duration_seconds}<span>· {duration(m.duration_seconds)}</span>{/if}
						{/if}
						{#if f.link_mode}<span>· {f.link_mode}</span>{/if}
						<span>· imported {ago(f.imported_at)}</span>
						<button class="small danger delete" onclick={() => deleteFile(f)} disabled={!!busy}>Delete</button>
					</div>
				</div>
			{/each}
		{/if}

		<h2>History</h2>
		{#if history.length === 0}
			<p class="muted">Nothing yet. {profileName ? `Quality: ${profileName}.` : ''}</p>
		{:else}
			<div class="card table-wrap">
				<table>
					<tbody>
						{#each grouped as { h, n } (h.id)}
							<tr>
								<td class="when muted small">{ago(h.created_at)}</td>
								<td><strong>{EVENTS[h.type] ?? h.type}</strong>{#if n > 1}<span class="muted small count">{n} episodes</span>{/if}</td>
								<td class="small">{describe(h)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</main>

{#if toast}<p class="notice toast" role="status">{toast}</p>{/if}

<style>
	.toast {
		position: fixed;
		left: 50%;
		bottom: 1rem;
		transform: translateX(-50%);
		z-index: 20;
		max-width: min(36rem, calc(100vw - 2rem));
		box-shadow: 0 4px 16px rgb(0 0 0 / 0.35);
	}
	.file .delete {
		margin-left: auto;
	}
	.head {
		display: flex;
		gap: 1.5rem;
		align-items: flex-start;
	}
	/* A scene from the title behind the whole header, evenly toned down so the text on it
	   stays readable in every theme; the poster sits on top of it */
	.head.backdrop {
		padding: 1.25rem;
		border-radius: var(--radius);
		background-image:
			linear-gradient(
				color-mix(in srgb, var(--bg) 72%, transparent),
				color-mix(in srgb, var(--bg) 72%, transparent)
			),
			var(--backdrop);
		background-size: cover;
		background-position: center 25%;
	}
	.ep-name {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}
	.still {
		width: 96px;
		aspect-ratio: 16 / 9;
		object-fit: cover;
		border-radius: 4px;
		flex: none;
		background: var(--surface-2);
	}
	@media (max-width: 560px) {
		.still {
			width: 64px;
		}
	}
	/* Beside the text and as tall as it, as wide as that makes a 2:3 poster: the whole
	   poster, never cropped; between 270px and 540px tall */
	.poster {
		flex: none;
		height: clamp(270px, var(--info-height, 270px), 540px);
		width: auto;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		border-radius: var(--radius);
		background: var(--surface-2);
	}
	/* An artist's picture is an album cover: square, and capped lower so it isn't huge */
	.poster.square {
		aspect-ratio: 1;
		height: clamp(200px, var(--info-height, 200px), 360px);
	}
	.info {
		flex: 1;
		min-width: 18rem;
		align-content: start;
	}
	/* Phones: the poster above the title, 180px wide */
	@media (max-width: 760px) {
		.head {
			flex-direction: column;
			gap: 1rem;
		}
		.poster,
		.poster.square {
			height: auto;
			width: 180px;
		}
		.info {
			min-width: 0;
			align-self: stretch;
		}
	}
	.where {
		display: grid;
		gap: 0.2rem;
	}
	.where-head {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.6rem;
	}
	button.change {
		background: none;
		border: 0;
		padding: 0;
		font: inherit;
		color: var(--accent);
		cursor: pointer;
	}
	button.change:hover {
		text-decoration: underline;
	}
	h1 {
		margin: 0 0 0.25rem;
	}
	.overview {
		margin: 0;
		max-width: 60ch;
	}
	.inline {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.album {
		margin-bottom: 0.5rem;
		padding: 0.6rem 0.75rem;
	}
	.album.unmonitored {
		opacity: 0.7;
	}
	.album-head {
		display: flex;
		gap: 0.75rem;
		align-items: center;
		flex-wrap: wrap;
	}
	.cover {
		width: 56px;
		height: 56px;
		border-radius: 4px;
		object-fit: cover;
		background: var(--surface-2);
		flex: none;
	}
	.album .grow {
		flex: 1;
		min-width: 10rem;
	}
	.edition {
		margin: 0.5rem 0;
	}
	.tracks {
		width: 100%;
		margin-top: 0.4rem;
	}
	.tracks td.num {
		width: 3rem;
		text-align: right;
		padding-right: 0.75rem;
	}
	h2 .link {
		font: inherit;
	}
	.season {
		padding: 0.5rem 0.75rem;
		margin-bottom: 0.5rem;
	}
	.season-name {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem 1rem;
	}
	.season-head td {
		padding-top: 0.15rem;
		padding-bottom: 0.15rem;
	}
	button.link {
		background: none;
		border: none;
		padding: 0.25rem 0;
		cursor: pointer;
		color: inherit;
		display: flex;
		gap: 0.5rem;
		align-items: baseline;
	}
	.episodes {
		table-layout: fixed;
		width: 100%;
	}
	/* The columns, for the season's line and its episodes alike */
	.episodes .c-num {
		width: 3.5rem;
	}
	/* the name as wide as it needs, up to a point; the spare room after the buttons */
	.episodes .c-name {
		width: min(26rem, 40vw);
	}
	.episodes td.rest {
		padding: 0;
	}
	.episodes .c-date {
		width: 7rem;
	}
	.episodes .c-state {
		width: 10rem;
	}
	.episodes .c-mon {
		width: 2.5rem;
	}
	.episodes .c-act {
		width: var(--act-width, 5.5rem); /* wider when plugins add buttons to each row */
	}
	.episodes td.act {
		white-space: nowrap; /* its buttons side by side, never stacked; Search above Search */
	}
	.episodes td.act button + button {
		margin-left: 0.35rem;
	}
	/* No line under the last row: a collapsed season's own line, or its last episode */
	.episodes thead:last-child tr td,
	.episodes tbody tr:last-child td {
		border-bottom: none;
	}
	.episodes td {
		vertical-align: middle;
	}
	.episodes .short {
		display: none;
	}
	/* Phones: no date column, a narrower number, "Choose": the names get the room */
	@media (max-width: 640px) {
		/* phones: each row's own Search only; a plugin's row buttons need the room the name
		   has (the season's and the title's plugin buttons are still there) */
		.episodes .c-act {
			width: 5.5rem;
		}
		.episodes .plugin-act {
			display: none;
		}
		.episodes .c-name {
			width: auto; /* phones: the name takes the room, none left over */
		}
		.episodes .c-rest {
			width: 0;
		}
		.episodes .c-num {
			width: 2rem;
		}
		.episodes .c-date {
			width: 0;
		}
		.episodes td.date {
			padding: 0;
			overflow: hidden;
			font-size: 0;
		}
		.episodes .c-state {
			width: 6rem;
		}
		.episodes .long {
			display: none;
		}
		.episodes .short {
			display: inline;
		}
	}
	.episodes td {
		padding: 0.3rem 0.5rem;
	}
	.num {
		width: 2.5rem;
		text-align: right;
	}
	.date {
		white-space: nowrap;
	}
	tr.unmonitored td {
		opacity: 0.55;
	}
	.count {
		margin-left: 0.4rem;
	}
	.when {
		white-space: nowrap;
		width: 7rem;
	}
	.file {
		gap: 0.4rem;
		margin-bottom: 0.5rem;
	}
</style>
