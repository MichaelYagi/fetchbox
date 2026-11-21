<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, get, post, type S, type Title } from '#lib/api.ts';

	let q = $state('');
	let results = $state<S['TitleSummary'][] | null>(null);
	let profiles = $state<S['ProfileOut'][]>([]);
	let profile = $state('');
	let searchNow = $state(true);
	let error = $state<ApiError | null>(null);
	let busy = $state(false);
	let adding = $state<number | null>(null);
	let owned = $state(new Map<number, string>());

	$effect(() => {
		get<S['ProfileOut'][]>('/quality-profiles').then((p) => {
			profiles = p;
			profile = (p.find((x) => x.name === 'Standard HD') ?? p[0])?.id ?? '';
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
			results = await get<S['TitleSummary'][]>('/discovery/search', { q: q.trim(), kind: 'movie' });
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
				tmdb_id: r.tmdb_id,
				profile_id: profile,
				search: searchNow
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
	<h1>Add a movie</h1>
	<form class="card stack" onsubmit={search}>
		<div class="row">
			<input
				class="grow"
				placeholder="Movie title, e.g. Dune"
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
			<label class="inline check">
				<input type="checkbox" bind:checked={searchNow} /> Search for it right away
			</label>
		</div>
	</form>

	{#if error}
		<div class="notice error">
			{error.message}
			{#if error.slug === 'metadata-not-configured'}
				— <a href="/settings#tmdb">add a TMDB API key in Settings</a>.
			{/if}
		</div>
	{/if}

	{#if results !== null}
		{#if results.length === 0}
			<p class="empty">No movies found for “{q}”.</p>
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
