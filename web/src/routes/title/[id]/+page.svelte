<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Badge from '#lib/Badge.svelte';
	import { ago, ApiError, bytes, del, duration, get, patch, post, type S, type Title } from '#lib/api.ts';

	const id = $derived(page.params.id ?? '');
	let title = $state<Title | null>(null);
	let files = $state<S['FileOut'][]>([]);
	let history = $state<S['HistoryOut'][]>([]);
	let profiles = $state<S['ProfileOut'][]>([]);
	let releases = $state<S['InteractiveOut'] | null>(null);
	let error = $state('');
	let message = $state('');
	let busy = $state('');

	const wanted = $derived(title?.wanted[0] ?? null);
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
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}

	$effect(() => {
		void id;
		load();
		get<S['ProfileOut'][]>('/quality-profiles').then((p) => (profiles = p));
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
			releases = await get<S['InteractiveOut']>('/releases', { wanted_item_id: wanted!.id });
		});
	const grab = (r: S['ReleaseOut']) =>
		act('grab', async () => {
			await post('/grabs', {
				release_id: r.release_id,
				wanted_item_ids: [wanted!.id],
				allow_rejected: !r.accepted
			});
			releases = null;
		}, `Grabbed ${r.candidate.title}.`);
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

	const EVENTS: Record<string, string> = {
		grabbed: 'Grabbed',
		imported: 'Imported',
		upgraded: 'Upgraded',
		failed: 'Download failed',
		deleted: 'File deleted',
		unlocked: 'Unlocked'
	};
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
							<span>· <a href="https://www.themoviedb.org/movie/{title.tmdb_id}" target="_blank" rel="noreferrer">TMDB</a></span>
						{/if}
					</div>
				</div>
				{#if title.overview}<p class="overview">{title.overview}</p>{/if}
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
								{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
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
				{/if}
			</div>
		</div>

		{#if releases}
			<h2>Releases <span class="muted small">from {releases.indexers_searched} indexer{releases.indexers_searched === 1 ? '' : 's'}</span></h2>
			{#each releases.failures as f}<p class="notice warn">{f.indexer_name}: {f.error}</p>{/each}
			{#if releases.releases.length === 0}
				<p class="empty card">No releases found. {releases.indexers_searched === 0 ? 'Add an indexer in Settings.' : ''}</p>
			{:else}
				<div class="card table-wrap">
					<table>
						<thead><tr><th>Release</th><th>Quality</th><th>Size</th><th>Seeds</th><th>Indexer</th><th></th></tr></thead>
						<tbody>
							{#each releases.releases as r (r.release_id)}
								<tr class:rejected={!r.accepted}>
									<td>
										<div class="name">{r.candidate.title}</div>
										{#each r.rejections as rej}<div class="small reason">{rej.message}</div>{/each}
									</td>
									<td>{r.quality}{r.format_score ? ` (${r.format_score > 0 ? '+' : ''}${r.format_score})` : ''}</td>
									<td>{bytes(r.candidate.size)}</td>
									<td>{r.candidate.seeders ?? '—'}</td>
									<td class="muted">{r.candidate.source_name}</td>
									<td>
										<button class={r.accepted ? 'primary' : ''} onclick={() => grab(r)} disabled={!!busy}
											title={r.accepted ? '' : 'Rejected: grab anyway'}>Grab</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		{/if}

		<h2>Files</h2>
		{#if files.length === 0}
			<p class="muted">No file yet.</p>
		{:else}
			{#each files as f (f.id)}
				<div class="card stack file">
					<code>{f.relative_path}</code>
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
						{#each history as h (h.id)}
							<tr>
								<td class="when muted small">{ago(h.created_at)}</td>
								<td><strong>{EVENTS[h.type] ?? h.type}</strong></td>
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
	.name {
		word-break: break-all;
	}
	.reason {
		color: var(--warn);
	}
	tr.rejected .name {
		color: var(--muted);
	}
	.when {
		white-space: nowrap;
		width: 7rem;
	}
	.file {
		gap: 0.4rem;
		margin-bottom: 0.5rem;
	}
	code {
		word-break: break-all;
	}
</style>
