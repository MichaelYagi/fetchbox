<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, get, post, type S, type Title } from '#lib/api.ts';

	let q = $state('');
	let results = $state<S['TitleSummary'][] | null>(null);
	let profiles = $state<S['ProfileOut'][]>([]);
	let profile = $state('');
	let searchNow = $state(true);
	let kind = $state<'movie' | 'series'>('movie');
	let monitorMode = $state('all');
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
	const noun = $derived(kind === 'movie' ? 'movie' : 'TV show');
	let error = $state<ApiError | null>(null);
	let busy = $state(false);
	let adding = $state<number | null>(null);
	let owned = $state(new Map<number, string>());

	$effect(() => {
		get<S['ProfileOut'][]>('/quality-profiles', { media: 'video' }).then((p) => {
			profiles = p;
			profile = (p.find((x) => x.name === '1080p') ?? p[0])?.id ?? '';
		});
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
			results = await get<S['TitleSummary'][]>('/discovery/search', { q: q.trim(), kind });
		} catch (err) {
			error = err instanceof ApiError ? err : new ApiError(0, String(err));
			results = null;
		} finally {
			busy = false;
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
		{#each [['movie', 'Movies'], ['series', 'TV shows']] as [k, label] (k)}
			<button
				role="tab"
				aria-selected={kind === k}
				class:active={kind === k}
				onclick={() => {
					kind = k as 'movie' | 'series';
					results = null;
				}}>{label}</button
			>
		{/each}
	</div>
	<form class="card stack" onsubmit={search}>
		<div class="row">
			<input
				class="grow"
				placeholder={kind === 'movie' ? 'Movie title, e.g. Dune' : 'Show title, e.g. Severance'}
				bind:value={q}
				aria-label="Search"
			/>
			<button class="primary" disabled={busy || !q.trim()}>{busy ? 'Searching…' : 'Search'}</button>
		</div>
		<div class="row">
			<label class="inline">
				Quality
				<select bind:value={profile}>
					{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
				</select>
			</label>
			{#if kind === 'series'}
				<label class="inline">
					Download
					<select bind:value={monitorMode}>
						{#each MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
					</select>
				</label>
			{:else}
				<label class="inline">
					Start searching
					<select bind:value={availability}>
						{#each AVAIL as [v, l] (v)}<option value={v}>{l}</option>{/each}
					</select>
				</label>
			{/if}
			<label class="inline check">
				<input type="checkbox" bind:checked={searchNow} /> Search for it right away
			</label>
		</div>
		<p class="muted small hint">
			“Any” also accepts releases that don't name their quality, such as the public-domain
			films from Internet Archive.
		</p>
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
										onclick={() => add(r)}
									>
										{adding === r.tmdb_id ? 'Adding…' : 'Add'}
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
</main>

<style>
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
	.overview {
		margin: 0.35rem 0 0;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>
