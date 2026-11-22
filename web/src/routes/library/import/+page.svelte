<script lang="ts">
	import { page } from '$app/state';
	import Path from '#lib/Path.svelte';
	import { ApiError, bytes, get, post, type S } from '#lib/api.ts';

	// Import what's already in a library folder: scan (changes nothing), review, import.
	type Match = { tmdb_id: number; name: string; year: number | null; poster_url: string | null };
	type Item = {
		folder: string;
		files: { path: string; size: number }[];
		match: Match | null;
		confidence: 'exact' | 'high' | 'low' | 'none';
		status: 'new' | 'in_library' | 'imported';
		size: number;
		quality: string;
		error?: string;
	};
	type Scan = { destination_id: string; kind: 'movie' | 'series'; path: string; items: Item[] };

	const destId = $derived(page.url.searchParams.get('dest') ?? '');
	let dest = $state<S['DestinationOut'] | null>(null);
	let profiles = $state<S['ProfileOut'][]>([]);
	let profile = $state('');
	let monitored = $state(true);
	let monitorMode = $state('all');
	let scan = $state<Scan | null>(null);
	let chosen = $state<Record<string, boolean>>({});
	let job = $state<{ progress: number | null; status: string } | null>(null);
	let result = $state<{ added: { name: string; title_id: string; new: boolean; files: number }[]; skipped: { folder: string; reason: string }[]; files: number } | null>(null);
	let show = $state<'all' | 'check' | 'ready' | 'done'>('all');
	let fixing = $state<string | null>(null);
	let query = $state('');
	let found = $state<S['TitleSummary'][]>([]);
	let error = $state('');

	const tv = $derived(dest?.media_kind === 'series');
	const noun = $derived(tv ? 'shows' : 'movies');
	const MODES: [string, string][] = [
		['all', 'All episodes'],
		['future', 'Only new episodes'],
		['missing', 'Episodes without a file, and new ones'],
		['none', 'Nothing (just keep what I have)']
	];

	$effect(() => {
		if (!destId) return;
		get<S['DestinationOut'][]>('/destinations').then((d) => (dest = d.find((x) => x.id === destId) ?? null));
		get<S['ProfileOut'][]>('/quality-profiles', { media: 'video' }).then((p) => {
			profiles = p;
			profile = (p.find((x) => x.name === '1080p') ?? p[0])?.id ?? '';
		});
	});

	async function waitFor(id: string): Promise<Record<string, unknown>> {
		for (;;) {
			const j = await get<S['JobOut']>(`/jobs/${id}`);
			job = { progress: j.progress, status: j.status };
			if (j.status === 'succeeded') return j.result ?? {};
			if (j.status === 'failed' || j.status === 'cancelled') throw new Error(j.error ?? 'the job failed');
			await new Promise((r) => setTimeout(r, 600));
		}
	}

	async function runScan() {
		error = '';
		result = null;
		scan = null;
		try {
			const j = await post<S['JobOut']>('/library/scan', { destination_id: destId });
			scan = (await waitFor(j.id)) as unknown as Scan;
			chosen = Object.fromEntries(
				scan.items.map((i) => [i.folder, i.status !== 'imported' && !!i.match && (i.confidence === 'exact' || i.confidence === 'high')])
			);
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			job = null;
		}
	}

	const picked = $derived(scan ? scan.items.filter((i) => chosen[i.folder] && i.match && i.status !== 'imported') : []);
	const counts = $derived({
		all: scan?.items.length ?? 0,
		check: scan?.items.filter((i) => i.status !== 'imported' && (i.confidence === 'low' || i.confidence === 'none')).length ?? 0,
		ready: scan?.items.filter((i) => i.status !== 'imported' && (i.confidence === 'exact' || i.confidence === 'high')).length ?? 0,
		done: scan?.items.filter((i) => i.status === 'imported').length ?? 0
	});
	const shown = $derived(
		(scan?.items ?? []).filter((i) =>
			show === 'all' ? true
			: show === 'done' ? i.status === 'imported'
			: show === 'check' ? i.status !== 'imported' && (i.confidence === 'low' || i.confidence === 'none')
			: i.status !== 'imported' && (i.confidence === 'exact' || i.confidence === 'high')
		)
	);

	async function runImport() {
		if (!scan || picked.length === 0) return;
		error = '';
		try {
			const j = await post<S['JobOut']>('/library/import', {
				destination_id: destId,
				items: picked.map((i) => ({
					folder: i.folder,
					files: i.files.map((f) => f.path),
					tmdb_id: i.match!.tmdb_id,
					profile_id: profile,
					monitored,
					monitor_mode: monitorMode
				}))
			});
			result = (await waitFor(j.id)) as unknown as typeof result;
			scan = null;
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			job = null;
		}
	}

	async function search(e: SubmitEvent) {
		e.preventDefault();
		if (!query.trim()) return;
		found = await get<S['TitleSummary'][]>('/discovery/search', { q: query.trim(), kind: dest!.media_kind });
	}
	function pick(item: Item, m: S['TitleSummary']) {
		item.match = { tmdb_id: m.tmdb_id, name: m.name, year: m.year ?? null, poster_url: m.poster_url ?? null };
		item.confidence = 'exact'; // chosen by the user
		chosen[item.folder] = true;
		fixing = null;
		found = [];
	}
	function startFix(item: Item) {
		fixing = item.folder;
		query = item.match?.name ?? item.folder.replace(/\.[a-z0-9]+$/i, '');
		found = [];
	}

	const CONF: Record<string, [string, string]> = {
		exact: ['ok', 'Sure'],
		high: ['ok', 'Good match'],
		low: ['warn', 'Check this'],
		none: ['warn', 'No match']
	};
</script>

<main>
	<p><a href="/settings#library">← Library settings</a></p>
	<h1>Import existing {noun}</h1>
	{#if !destId}
		<p class="notice error">No library folder chosen.</p>
	{:else if dest}
		<p class="muted">From <Path path={dest.path} /></p>
		<p class="muted small">
			Scanning only reads the folder: each folder inside it is one {tv ? 'show' : 'movie'}. Nothing is
			added, moved or renamed until you click Import, and even then files stay exactly where they are.
		</p>
	{/if}
	{#if error}<p class="notice error">{error}</p>{/if}

	{#if result}
		<div class="card stack">
			<h2>Done</h2>
			<p>
				{result.added.filter((a) => a.new).length} {noun} added, {result.files} file{result.files === 1 ? '' : 's'} recorded.
				{#if result.added.some((a) => !a.new)}{result.added.filter((a) => !a.new).length} were already in Fetchbox.{/if}
			</p>
			{#if result.skipped.length}
				<p class="notice warn">Skipped: {result.skipped.map((s) => `${s.folder} (${s.reason})`).join('; ')}</p>
			{/if}
			<p class="muted small">
				Fetchbox searches for what's missing or below your quality target within the hour. To rename the files to
				your naming style, use “Review renames” in Library settings.
			</p>
			<div class="row">
				<a class="button primary" href="/">Go to the library</a>
				<button onclick={runScan}>Scan again</button>
			</div>
		</div>
	{:else if !scan}
		<button class="primary" onclick={runScan} disabled={!!job || !dest}>
			{job ? `Scanning… ${job.progress != null ? Math.round(job.progress * 100) + '%' : ''}` : 'Scan this folder'}
		</button>
	{:else}
		<div class="row tabs" role="tablist">
			{#each [['all', 'All'], ['ready', 'Ready'], ['check', 'Needs a look'], ['done', 'Already imported']] as [k, l] (k)}
				<button role="tab" aria-selected={show === k} class:on={show === k} onclick={() => (show = k as typeof show)}>
					{l} <span class="muted small">{counts[k as keyof typeof counts]}</span>
				</button>
			{/each}
		</div>
		{#if scan.items.length === 0}
			<p class="empty">No {noun} found in this folder.</p>
		{/if}
		<div class="list">
			{#each shown as item (item.folder)}
				<div class="card item" class:off={!chosen[item.folder] || item.status === 'imported'}>
					<input
						type="checkbox"
						aria-label="Import {item.folder}"
						bind:checked={chosen[item.folder]}
						disabled={item.status === 'imported' || !item.match}
					/>
					{#if item.match?.poster_url}<img src={item.match.poster_url} alt="" loading="lazy" />{:else}<div class="noposter"></div>{/if}
					<div class="body">
						<div class="folder" title={item.folder}>{item.folder}</div>
						<div class="muted small">
							{item.files.length} file{item.files.length === 1 ? '' : 's'} · {bytes(item.size)} · {item.quality}
						</div>
						<div class="row match">
							{#if item.match}
								<span>→ <strong>{item.match.name}</strong> {item.match.year ?? ''}</span>
							{:else}
								<span class="muted">Not found on TMDB</span>
							{/if}
							<span class="badge {CONF[item.confidence][0]}">{CONF[item.confidence][1]}</span>
							{#if item.status === 'in_library'}<span class="badge">Already in Fetchbox: files are added to it</span>{/if}
							{#if item.status === 'imported'}<span class="badge ok">Already imported</span>{/if}
							{#if item.status !== 'imported'}<button class="small" onclick={() => startFix(item)}>Change</button>{/if}
						</div>
						{#if fixing === item.folder}
							<form class="row" onsubmit={search}>
								<input class="grow" bind:value={query} aria-label="Search TMDB" />
								<button>Search</button>
								<button type="button" onclick={() => (fixing = null)}>Cancel</button>
							</form>
							{#each found as m (m.tmdb_id)}
								<button class="pick" onclick={() => pick(item, m)}>
									{#if m.poster_url}<img src={m.poster_url} alt="" />{/if}
									<span><strong>{m.name}</strong> {m.year ?? ''}<br /><span class="muted small">{m.overview.slice(0, 140)}</span></span>
								</button>
							{/each}
						{/if}
					</div>
				</div>
			{/each}
		</div>

		<div class="card footer">
			<label class="inline">
				Quality
				<select bind:value={profile}>
					{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
				</select>
			</label>
			{#if tv}
				<label class="inline">
					Download
					<select bind:value={monitorMode}>
						{#each MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
					</select>
				</label>
			{:else}
				<label class="inline"><input type="checkbox" bind:checked={monitored} /> Look for upgrades</label>
			{/if}
			<button class="primary" onclick={runImport} disabled={picked.length === 0 || !!job || !profile}>
				{job ? `Importing… ${job.progress != null ? Math.round(job.progress * 100) + '%' : ''}` : `Import ${picked.length} ${noun}`}
			</button>
		</div>
	{/if}
</main>

<style>
	.tabs {
		gap: 0.35rem;
		margin-bottom: 0.75rem;
		flex-wrap: wrap;
	}
	.tabs button.on {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.list {
		display: grid;
		gap: 0.5rem;
		margin-bottom: 5rem;
	}
	.item {
		display: flex;
		gap: 0.75rem;
		align-items: flex-start;
		padding: 0.6rem 0.75rem;
		min-width: 0;
	}
	.item.off {
		opacity: 0.65;
	}
	.item > input {
		margin-top: 0.4rem;
	}
	img,
	.noposter {
		width: 46px;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		border-radius: 4px;
		background: var(--surface-2);
		flex: none;
	}
	.body {
		flex: 1;
		min-width: 0;
		display: grid;
		gap: 0.25rem;
	}
	.folder {
		font-weight: 600;
		overflow-wrap: anywhere;
	}
	.match {
		gap: 0.5rem;
		flex-wrap: wrap;
		align-items: center;
	}
	.grow {
		flex: 1;
		min-width: 10rem;
	}
	.pick {
		display: flex;
		gap: 0.6rem;
		text-align: left;
		white-space: normal;
		align-items: flex-start;
	}
	.footer {
		position: sticky;
		bottom: 0.75rem;
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		align-items: center;
		box-shadow: 0 4px 18px rgb(0 0 0 / 0.12);
	}
	.inline {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
</style>
