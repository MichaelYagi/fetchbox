<script lang="ts">
	import Badge from '#lib/Badge.svelte';
	import { ApiError, get, type S, type Title } from '#lib/api.ts';

	let titles = $state<Title[] | null>(null);
	let error = $state('');
	let filter = $state('');
	let show = $state<'all' | 'missing' | 'acquired' | 'active'>('all');

	async function load() {
		try {
			const page = await get<S['TitlePage']>('/titles', { limit: 500 });
			titles = page.items;
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}

	$effect(() => {
		load();
		const t = setInterval(load, 15_000);
		return () => clearInterval(t);
	});

	function stateOf(t: Title): string {
		if (t.kind === 'series') {
			const e = t.episodes;
			if (!e) return 'missing';
			if (t.wanted.some((w) => !['missing', 'acquired'].includes(w.state))) return 'downloading';
			return e.missing > 0 ? 'missing' : 'acquired';
		}
		return t.wanted[0]?.state ?? 'missing';
	}
	const shown = $derived(
		(titles ?? []).filter((t) => {
			const s = stateOf(t);
			if (filter && !`${t.name} ${t.year ?? ''}`.toLowerCase().includes(filter.toLowerCase()))
				return false;
			if (show === 'missing') return s === 'missing';
			if (show === 'acquired') return s === 'acquired';
			if (show === 'active') return !['missing', 'acquired'].includes(s);
			return true;
		})
	);
</script>

<main>
	<div class="spread">
		<h1>Library</h1>
		<div class="row">
			<input placeholder="Filter" bind:value={filter} aria-label="Filter titles" />
			<select bind:value={show} aria-label="Show">
				<option value="all">All</option>
				<option value="acquired">In library</option>
				<option value="active">Downloading</option>
				<option value="missing">Missing</option>
			</select>
			<a class="button primary" href="/add">Add movie or show</a>
		</div>
	</div>

	{#if error}
		<p class="notice error">{error}</p>
	{:else if titles === null}
		<p class="muted">Loading…</p>
	{:else if titles.length === 0}
		<div class="empty card">
			<p>Nothing here yet.</p>
			<p><a class="button primary" href="/add">Add your first movie or show</a></p>
		</div>
	{:else if shown.length === 0}
		<p class="empty">No titles match.</p>
	{:else}
		<div class="grid">
			{#each shown as t (t.id)}
				<a class="tile" href="/title/{t.id}">
					{#if t.poster_url}
						<img src={t.poster_url} alt="" loading="lazy" />
					{:else}
						<div class="noposter">{t.name}</div>
					{/if}
					<div class="meta">
						<div class="name" title={t.name}>{t.name}</div>
						<div class="spread">
							<span class="muted small">{t.year ?? ''}</span>
							{#if t.kind === 'series' && t.episodes}
								<span class="badge {t.episodes.missing ? 'warn' : 'ok'}" title="Episodes downloaded of monitored">
									{t.episodes.acquired}/{t.episodes.monitored}
								</span>
							{:else}
								<Badge state={stateOf(t)} />
							{/if}
						</div>
					</div>
				</a>
			{/each}
		</div>
	{/if}
</main>

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 1rem;
		margin-top: 1rem;
	}
	.tile {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
		color: var(--text);
		display: flex;
		flex-direction: column;
	}
	.tile:hover {
		text-decoration: none;
		border-color: var(--accent);
	}
	img,
	.noposter {
		width: 100%;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		background: var(--surface-2);
		display: block;
	}
	.noposter {
		display: grid;
		place-items: center;
		padding: 1rem;
		text-align: center;
		color: var(--muted);
	}
	.meta {
		padding: 0.5rem 0.6rem 0.6rem;
		display: grid;
		gap: 0.25rem;
	}
	.name {
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
