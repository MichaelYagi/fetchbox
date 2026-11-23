<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, get, post, type S, type Title } from '#lib/api.ts';
	import FolderTest from '#lib/FolderTest.svelte';

	let q = $state('');
	let results = $state<S['TitleSummary'][] | null>(null);
	let profiles = $state<S['ProfileOut'][]>([]);
	let profile = $state('');
	let searchNow = $state(true);
	let kind = $state<'movie' | 'series' | 'music'>('movie');
	let artists = $state<S['ArtistHit'][] | null>(null);
	let artistMode = $state('future');
	let albumKinds = $state<string[]>(['album']);
	let addingArtist = $state<string | null>(null);
	const ARTIST_MODES: [string, string][] = [
		['all', 'All their albums'],
		['future', 'Only new albums'],
		['latest', 'The latest album, and new ones'],
		['none', 'Nothing for now']
	];
	const KINDS: [string, string][] = [
		['album', 'Albums'],
		['ep', 'EPs'],
		['single', 'Singles'],
		['live', 'Live'],
		['compilation', 'Compilations'],
		['other', 'Remixes, soundtracks…']
	];
	let monitorMode = $state('future');
	let availability = $state('released');
	const MODES: [string, string][] = [
		['all', 'All episodes'],
		['future', 'Only new episodes'],
		['missing', 'Episodes without a file, and new ones'],
		['first_season', 'First season'],
		['latest_season', 'Latest season'],
		['none', 'Nothing for now']
	];
	const AVAIL: [string, string][] = [
		['released', 'Once released (digital or disc)'],
		['in_cinemas', 'Once in cinemas'],
		['announced', 'Right away']
	];
	const noun = $derived(kind === 'movie' ? 'movie' : kind === 'series' ? 'TV show' : 'music artist');
	let error = $state<ApiError | null>(null);
	let busy = $state(false);
	let adding = $state<number | null>(null);
	let owned = $state(new Map<number, string>());
	// What "Add" was clicked on: the dialog asks how before anything is added or downloaded.
	let pending = $state<
		{ kind: 'movie' | 'series'; r: S['TitleSummary'] } | { kind: 'music'; a: S['ArtistHit'] } | null
	>(null);
	let dialog = $state<HTMLDialogElement | null>(null);
	// Library folders from Settings: the dialog's Folder starts at the default for the kind.
	let folders = $state<S['DestinationOut'][]>([]);
	let folder = $state('');
	const foldersFor = (k: string) => folders.filter((d) => d.media_kind === k);
	const defaultFolder = (k: string) => foldersFor(k).find((d) => d.is_default) ?? foldersFor(k)[0];
	// only sent when changed: left as is, the title follows Settings
	const folderChoice = (k: string) => {
		const f = folder.trim();
		return f && f !== defaultFolder(k)?.path ? { folder: f } : {};
	};
	$effect(() => {
		if (pending && dialog && !dialog.open) dialog.showModal();
	});

	function ask(p: NonNullable<typeof pending>) {
		error = null;
		// each time: only what's new, unless you choose otherwise
		monitorMode = 'future';
		artistMode = 'future';
		searchNow = true;
		folder = defaultFolder(p.kind)?.path ?? '';
		pending = p;
		// fresh, in case Settings changed since the page opened
		get<S['DestinationOut'][]>('/destinations').then((d) => {
			const before = defaultFolder(p.kind)?.path ?? '';
			folders = d;
			if (pending === p && folder === before) folder = defaultFolder(p.kind)?.path ?? '';
		});
	}

	function close() {
		dialog?.close();
		pending = null;
	}

	async function confirmAdd(e: SubmitEvent) {
		e.preventDefault();
		const p = pending;
		if (!p) return;
		if (p.kind === 'music') await addArtist(p.a);
		else await add(p.r);
		if (!error) close();
	}

	$effect(() => {
		const media = kind === 'music' ? 'music' : 'video';
		get<S['ProfileOut'][]>('/quality-profiles', { media }).then((p) => {
			profiles = p;
			const pick = media === 'music' ? 'Lossless' : '1080p';
			profile = (p.find((x) => x.name === pick) ?? p[0])?.id ?? '';
		});
	});
	$effect(() => {
		get<S['DestinationOut'][]>('/destinations').then((d) => (folders = d));
	});
	$effect(() => {
		get<S['TitlePage']>('/titles', { limit: 500 }).then((page) => {
			owned = new Map(page.items.filter((t) => t.tmdb_id).map((t) => [t.tmdb_id as number, t.id]));
		});
	});

	async function search(e: SubmitEvent) {
		e.preventDefault();
		if (!q.trim()) return;
		busy = true;
		error = null;
		try {
			if (kind === 'music') {
				artists = await get<S['ArtistHit'][]>('/music/search', { q: q.trim() });
				return;
			}
			results = await get<S['TitleSummary'][]>('/discovery/search', { q: q.trim(), kind });
		} catch (err) {
			error = err instanceof ApiError ? err : new ApiError(0, String(err));
			results = null;
		} finally {
			busy = false;
		}
	}

	async function addArtist(a: S['ArtistHit']) {
		addingArtist = a.mbid;
		error = null;
		try {
			const t = await post<Title>('/music/artists', {
				mbid: a.mbid,
				profile_id: profile,
				monitor_mode: artistMode,
				album_kinds: albumKinds,
				search: searchNow,
				...folderChoice('music')
			});
			await goto(`/title/${t.id}`);
		} catch (err) {
			error = err instanceof ApiError ? err : new ApiError(0, String(err));
		} finally {
			addingArtist = null;
		}
	}

	async function add(r: S['TitleSummary']) {
		adding = r.tmdb_id;
		error = null;
		try {
			const t = await post<Title>('/titles', {
				kind,
				tmdb_id: r.tmdb_id,
				profile_id: profile,
				search: searchNow,
				...folderChoice(kind),
				...(kind === 'series' ? { monitor_mode: monitorMode } : { minimum_availability: availability })
			});
			await goto(`/title/${t.id}`);
		} catch (err) {
			error = err instanceof ApiError ? err : new ApiError(0, String(err));
		} finally {
			adding = null;
		}
	}
</script>

<main>
	<h1>Add a {noun}</h1>
	<div class="tabs" role="tablist">
		{#each [['movie', 'Movies'], ['series', 'TV shows'], ['music', 'Music']] as [k, label] (k)}
			<button
				role="tab"
				aria-selected={kind === k}
				class:active={kind === k}
				onclick={() => {
					kind = k as 'movie' | 'series' | 'music';
					results = null;
					artists = null;
				}}>{label}</button
			>
		{/each}
	</div>
	<form class="card stack" onsubmit={search}>
		<div class="row">
			<input
				class="grow"
				placeholder={kind === 'movie'
					? 'Movie title, e.g. Dune'
					: kind === 'series'
						? 'Show title, e.g. Severance'
						: 'Artist, e.g. Massive Attack'}
				bind:value={q}
				aria-label="Search"
			/>
			<button class="primary" disabled={busy || !q.trim()}>{busy ? 'Searching…' : 'Search'}</button>
		</div>
		{#if kind === 'music'}
			<p class="muted small hint">Artists, albums and track lists come from MusicBrainz.</p>
		{/if}
	</form>

	{#if error}
		<div class="notice error">
			{error.message}
			{#if error.slug === 'metadata-not-configured'}
				— get a free key at
				<a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer"
					>themoviedb.org/settings/api</a
				>, then <a href="/settings#tmdb">paste it in Settings</a>.
			{/if}
		</div>
	{/if}

	{#if artists !== null}
		{#if artists.length === 0}
			<p class="empty">No artists found for “{q}”.</p>
		{:else}
			<div class="results">
				{#each artists as a (a.mbid)}
					<div class="card result">
						<div class="avatar" aria-hidden="true">{a.name.slice(0, 1)}</div>
						<div class="body">
							<div class="spread">
								<strong>{a.name}</strong>
								{#if a.in_library}
									<a class="button" href="/title/{a.in_library}">In library</a>
								{:else}
									<button
										class="primary"
										disabled={addingArtist !== null || !profile}
										onclick={() => ask({ kind: 'music', a })}
									>
										Add…
									</button>
								{/if}
							</div>
							<div class="muted small">
								{[a.type, a.country, a.begin_year ? `since ${a.begin_year}` : null]
									.filter(Boolean)
									.join(' · ')}
							</div>
							{#if a.disambiguation}<p class="muted small overview">{a.disambiguation}</p>{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}

	{#if results !== null}
		{#if results.length === 0}
			<p class="empty">No {kind === 'movie' ? 'movies' : 'TV shows'} found for “{q}”.</p>
		{:else}
			<div class="results">
				{#each results as r (r.tmdb_id)}
					<div class="card result">
						{#if r.poster_url}
							<img src={r.poster_url} alt="" loading="lazy" />
						{:else}
							<div class="noposter"></div>
						{/if}
						<div class="body">
							<div class="spread">
								<strong>{r.name} {r.year ? `(${r.year})` : ''}</strong>
								{#if owned.has(r.tmdb_id)}
									<a class="button" href="/title/{owned.get(r.tmdb_id)}">In library</a>
								{:else}
									<button
										class="primary"
										disabled={adding !== null || !profile}
										onclick={() => ask({ kind: kind as 'movie' | 'series', r })}
									>
										Add…
									</button>
								{/if}
							</div>
							{#if r.original_name && r.original_name !== r.name}
								<div class="muted small">{r.original_name}</div>
							{/if}
							<p class="muted small overview">{r.overview}</p>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
	<dialog bind:this={dialog} onclose={() => (pending = null)} aria-labelledby="add-title">
		{#if pending}
			{@const isMusic = pending.kind === 'music'}
			{@const name = pending.kind === 'music' ? pending.a.name : `${pending.r.name}${pending.r.year ? ` (${pending.r.year})` : ''}`}
			<form method="dialog" class="stack" onsubmit={confirmAdd}>
				<div class="head">
					{#if pending.kind !== 'music' && pending.r.poster_url}
						<img src={pending.r.poster_url} alt="" />
					{/if}
					<div>
						<h2 id="add-title">Add {name}</h2>
						<p class="muted small">
							{pending.kind === 'movie' ? 'Movie' : pending.kind === 'series' ? 'TV show' : 'Music artist'} · nothing is
							added or downloaded until you confirm
						</p>
					</div>
				</div>

				<label class="field">
					Quality
					<select bind:value={profile} required>
						{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
					</select>
				</label>

				{#if pending.kind === 'series'}
					<label class="field">
						Download
						<select bind:value={monitorMode}>
							{#each MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
						</select>
					</label>
				{:else if pending.kind === 'music'}
					<label class="field">
						Download
						<select bind:value={artistMode}>
							{#each ARTIST_MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
						</select>
					</label>
					<fieldset class="kinds">
						<legend class="small">Which kinds</legend>
						{#each KINDS as [v, l] (v)}
							<label class="inline check small"><input type="checkbox" value={v} bind:group={albumKinds} /> {l}</label>
						{/each}
					</fieldset>
				{:else}
					<label class="field">
						Start searching
						<select bind:value={availability}>
							{#each AVAIL as [v, l] (v)}<option value={v}>{l}</option>{/each}
						</select>
					</label>
				{/if}

				<label class="field">
					Folder
					<input bind:value={folder} list="folder-choices" spellcheck="false" autocomplete="off" />
					<datalist id="folder-choices">
						{#each foldersFor(pending.kind) as d (d.id)}<option value={d.path}>{d.name}</option>{/each}
					</datalist>
					<span class="muted small">
						{#if !folder.trim() || folder.trim() === defaultFolder(pending.kind)?.path}
							The {defaultFolder(pending.kind)?.name ?? ''} folder from Settings (it follows any change there).
						{:else if foldersFor(pending.kind).some((d) => d.path === folder.trim())}
							One of your library folders.
						{:else}
							A new folder: it's created, and added to Settings → Library with the same naming.
						{/if}
					</span>
				</label>
				<FolderTest path={folder.trim() || (defaultFolder(pending.kind)?.path ?? '')} />

				{#if !(pending.kind !== 'movie' && (isMusic ? artistMode : monitorMode) === 'none')}
					<label class="inline check">
						<input type="checkbox" bind:checked={searchNow} /> Search for it right away
					</label>
				{/if}
				<p class="muted small hint">
					{#if pending.kind === 'series' && monitorMode === 'future'}
						Episodes that have already aired aren't downloaded; new ones are, as they air.
					{:else if pending.kind === 'music' && artistMode === 'future'}
						Albums already out aren't downloaded; new ones are, when they're released.
					{:else if pending.kind === 'movie'}
						“Any” quality also accepts releases that don't name theirs, such as public-domain films from Internet
						Archive.
					{:else}
						You can change this later on its page.
					{/if}
				</p>

				{#if error}<p class="notice error">{error.message}</p>{/if}
				<div class="row end">
					<button type="button" onclick={close} disabled={adding !== null || addingArtist !== null}>Cancel</button>
					<button
						class="primary"
						disabled={adding !== null || addingArtist !== null || !profile || (isMusic && albumKinds.length === 0)}
					>
						{#if adding !== null || addingArtist !== null}
							{isMusic ? 'Adding… (asking MusicBrainz)' : 'Adding…'}
						{:else}
							Add
						{/if}
					</button>
				</div>
			</form>
		{/if}
	</dialog>
</main>

<style>
	dialog {
		width: min(32rem, calc(100vw - 2rem));
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
		color: var(--text);
		padding: 1.25rem;
	}
	dialog::backdrop {
		background: rgb(0 0 0 / 0.5);
	}
	.head {
		display: flex;
		gap: 1rem;
		align-items: flex-start;
	}
	.head h2 {
		margin: 0 0 0.25rem;
		font-size: 1.15rem;
	}
	.head p {
		margin: 0;
	}
	.head img {
		width: 56px;
	}
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.end {
		justify-content: flex-end;
	}
	.tabs {
		display: flex;
		gap: 0.25rem;
		margin-bottom: 0.75rem;
	}
	.tabs button.active {
		background: var(--accent);
		color: var(--accent-text);
		border-color: var(--accent);
	}
	.grow {
		flex: 1;
		min-width: 12rem;
	}
	.inline {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.check {
		cursor: pointer;
	}
	.hint {
		margin: 0;
	}
	.results {
		display: grid;
		gap: 0.75rem;
		margin-top: 1rem;
	}
	.result {
		display: flex;
		gap: 1rem;
		padding: 0.75rem;
	}
	img,
	.noposter {
		width: 72px;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		border-radius: 6px;
		background: var(--surface-2);
		flex: none;
	}
	.body {
		flex: 1;
		min-width: 0;
	}
	.avatar {
		width: 56px;
		height: 56px;
		border-radius: 50%;
		background: var(--surface-2);
		display: grid;
		place-items: center;
		font-size: 1.4rem;
		font-weight: 600;
		flex: none;
	}
	.kinds {
		border: none;
		padding: 0;
		margin: 0;
		gap: 0.75rem;
	}
	.kinds legend {
		margin-right: 0.5rem;
	}
	.overview {
		margin: 0.35rem 0 0;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>
