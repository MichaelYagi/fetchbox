<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Badge from '#lib/Badge.svelte';
	import Path from '#lib/Path.svelte';
	import ReleasesTable from '#lib/ReleasesTable.svelte';
	import { ago, ApiError, bytes, del, duration, get, patch, post, put, type S, type Title } from '#lib/api.ts';

	const id = $derived(page.params.id ?? '');
	let title = $state<Title | null>(null);
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
	let editions = $state<Record<string, S['EditionOut'][]>>({});
	let showKinds = $state<Record<string, boolean>>({});
	let error = $state('');
	let message = $state('');
	let busy = $state('');
	// Change: another folder for it (full path), moving what's there or not
	let editingFolder = $state(false);
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
	const redownload = () => {
		if (!confirm('Blocklist the current release, move the file to the recycle folder, and download again?')) return;
		act('redownload', () => post(`/wanted/${wanted!.id}/redownload`), 'Searching for a new copy.');
	};
	const remove = () => {
		if (!confirm(`Remove ${title!.name} from Fetchbox? Files on disk are kept.`)) return;
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
		<div class="head">
			{#if title.poster_url}<img class="poster" src={title.poster_url} alt="" />{/if}
			<div class="stack info">
				<div>
					<h1>{title.name} <span class="muted">{title.year ?? ''}</span></h1>
					<div class="row muted small">
						{#if title.runtime}<span>{title.runtime} min</span>{/if}
						{#if title.genres.length}<span>· {title.genres.join(', ')}</span>{/if}
						{#if title.tmdb_id}
							<span>· <a href="https://www.themoviedb.org/{series ? 'tv' : 'movie'}/{title.tmdb_id}" target="_blank" rel="noreferrer">TMDB</a></span>
						{/if}
					</div>
				</div>
				{#if title.overview}<p class="overview">{title.overview}</p>{/if}
				{#if folder}
					<div class="small where">
						<span class="muted">{files.length || albums.some((a) => a.wanted.state === 'acquired') ? 'Files in' : 'Downloads to'}</span>
						<Path path={folder.path} />
						<span class="muted">
							· {folder.own ? `its own library folder, ${folder.library_folder_name}` : `the ${folder.library_folder_name} folder from Settings`}{#if title.kind === 'music' && folder.path === folder.library_folder_path}, one folder per album{/if}
						</span>
						{#if !(title.kind === 'music' && folder.path === folder.library_folder_path) && !editingFolder}
							<button class="small" onclick={() => ((newFolder = folder!.path), (moveFiles = true), (editingFolder = true))}>Change</button>
						{/if}
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
								{#each profiles.filter((x) => x.media === (title?.kind === 'music' ? 'music' : 'video')) as p (p.id)}<option value={p.id} selected={p.id === title.profile_id}>{p.name}</option>{/each}
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
								{#each profiles.filter((x) => x.media === (title?.kind === 'music' ? 'music' : 'video')) as p (p.id)}<option value={p.id} selected={p.id === title.profile_id}>{p.name}</option>{/each}
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

		{#if series}
			<h2>Seasons</h2>
			{#each [...seasons].sort((a, b) => (a.number === 0 ? 1 : b.number === 0 ? -1 : b.number - a.number)) as x (x.number)}
				<div class="card season">
					<div class="spread season-head">
						<button class="link" onclick={() => (open = { ...open, [x.number]: !open[x.number] })} aria-expanded={!!open[x.number]}>
							{open[x.number] ? '▾' : '▸'} <strong>{seasonName(x.number)}</strong>
							<span class="muted small">{seasonCount(x)} downloaded</span>
						</button>
						<div class="row">
							<label class="inline small">
								<input type="checkbox" checked={x.monitored} onchange={(e) => monitorSeason(x.number, e.currentTarget.checked)} disabled={!!busy} />
								Monitored
							</label>
							<button onclick={() => searchSeason(x.number)} disabled={!!busy}>Search</button>
							<button onclick={() => chooseSeason(x.number)} disabled={!!busy}>Choose a release</button>
						</div>
					</div>
					{#if open[x.number]}
						<div class="table-wrap">
							<table class="episodes">
								<tbody>
									{#each x.episodes as e (e.id)}
										<tr class:unmonitored={!e.wanted.monitored}>
											<td class="num muted">{e.number}</td>
											<td>{e.name ?? `Episode ${e.number}`}</td>
											<td class="muted small date">{e.air_date ?? ''}</td>
											<td>
												{#if epState(e)}<span class="muted small">{epState(e)}</span>{:else}<Badge state={e.wanted.state} />{/if}
											</td>
											<td>
												<input type="checkbox" title="Monitored" aria-label="Monitored" checked={e.wanted.monitored} onchange={(ev) => monitorEpisode(e.wanted, ev.currentTarget.checked)} disabled={!!busy} />
											</td>
											<td>
												<button class="small" onclick={() => chooseEpisode(x.number, e)} disabled={!!busy || !aired(e)} title="Search and choose a release">Search</button>
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
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
						<span>· {f.link_mode}</span>
						<span>· imported {ago(f.imported_at)}</span>
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

<style>
	.head {
		display: flex;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.poster {
		width: 180px;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		border-radius: var(--radius);
		background: var(--surface-2);
	}
	.info {
		flex: 1;
		min-width: 18rem;
		align-content: start;
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
	.season-head {
		flex-wrap: wrap;
		gap: 0.5rem;
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
	.episodes td:nth-child(3) {
		width: 7rem;
	}
	.episodes td:nth-child(4) {
		width: 7.5rem;
	}
	.episodes td:nth-child(5) {
		width: 2.5rem;
	}
	.episodes td:nth-child(6) {
		width: 5.5rem;
		text-align: right;
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
