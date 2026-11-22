<script lang="ts">
	import Path from '#lib/Path.svelte';
	import { ago, ApiError, bytes, del, duration, get, post, rate, type Download, type S } from '#lib/api.ts';

	let downloads = $state<Download[] | null>(null);
	let imports = $state<S['ImportOut'][]>([]);
	// Which clients keep seeding after download (off by default): the others' torrents stop
	// on their own once filed, so there's nothing to stop by hand.
	let seeds = $state<Set<string>>(new Set());
	let error = $state('');
	let notice = $state('');
	let busy = $state('');
	let showDone = $state(false);

	async function load() {
		try {
			let clients: S['ClientOut'][];
			[downloads, imports, clients] = await Promise.all([
				get<Download[]>('/downloads'),
				get<S['ImportOut'][]>('/imports'),
				get<S['ClientOut'][]>('/download-clients')
			]);
			seeds = new Set(clients.filter((c) => c.policy.seed).map((c) => c.id));
			error = '';
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}

	$effect(() => {
		load();
		const t = setInterval(load, 2000);
		return () => clearInterval(t);
	});

	async function act(key: string, fn: () => Promise<unknown>) {
		busy = key;
		try {
			await fn();
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	function remove(d: Download) {
		const active = ['queued', 'downloading', 'paused'].includes(d.status);
		const msg = active
			? `Stop and remove ${d.name}? Its data is deleted and the release is blocklisted, so another one is searched for.`
			: d.imported_as === 'in_place'
				? `Stop seeding ${d.name}?\n\nThe torrent stops. Your file stays where it's filed:\n${d.imported_to}`
				: d.imported_to
					? `Stop seeding ${d.name}?\n\nThe torrent's files in ${d.save_path ?? 'the download folder'} are deleted. ` +
						`The file Fetchbox imported into your library isn't touched (imported to ${d.imported_to}).` +
						(d.imported_as === 'hardlink' ? ' (It was the same file, so no space is freed.)' : '')
					: `Stop seeding ${d.name}? It hasn't been imported into your library, so its files are kept.`;
		if (!confirm(msg)) return;
		act(d.id, () => del(`/downloads/${d.id}`, { delete_data: true, blocklist: active }));
	}

	function again(d: Download) {
		if (!confirm(`Download ${d.name} again? Fetchbox searches for it now and may grab the same release. Nothing is deleted.`)) return;
		act(d.id, async () => {
			const r = await post<{ searching: number; skipped: string[] }>(`/downloads/${d.id}/download-again`);
			notice = r.searching
				? `Searching for ${d.name} again.`
				: `Not searched: ${r.skipped.join('; ') || 'nothing to search for'}`;
		});
	}

	const LABEL: Record<string, string> = {
		queued: 'Queued',
		downloading: 'Downloading',
		paused: 'Paused',
		completed: 'Seeding',
		failed: 'Failed',
		removed: 'Removed'
	};
	const tone = (d: Download) =>
		d.status === 'failed' ? 'error' : d.status === 'completed' ? 'ok' : d.status === 'paused' ? '' : 'info';
	const active = $derived((downloads ?? []).filter((d) => ['queued', 'downloading', 'paused'].includes(d.status)));
	const done = $derived((downloads ?? []).filter((d) => !['queued', 'downloading', 'paused'].includes(d.status)));
	const attention = $derived(imports.filter((i) => i.status === 'needs_attention'));
	// Finished is "Downloaded", whatever happened to the file since (it may have been moved).
	const seeding = (d: Download) => d.status === 'completed' && !d.seeded_at && seeds.has(d.client_id ?? '');
	const label = (d: Download) =>
		d.completed_at ? (seeding(d) ? 'Downloaded · seeding' : 'Downloaded') : (LABEL[d.status] ?? d.status);

	// Ticked downloads (the active list), for pausing, resuming or removing several at once.
	let picked = $state<Set<string>>(new Set());
	$effect(() => {
		// a download that finished or went away drops out of the selection
		const live = new Set(active.map((d) => d.id));
		if ([...picked].some((id) => !live.has(id))) picked = new Set([...picked].filter((id) => live.has(id)));
	});
	const chosen = $derived(active.filter((d) => picked.has(d.id)));
	const allPicked = $derived(active.length > 0 && chosen.length === active.length);

	function pick(id: string, on: boolean) {
		const next = new Set(picked);
		if (on) next.add(id);
		else next.delete(id);
		picked = next;
	}

	async function batch(what: 'pause' | 'resume' | 'remove') {
		const list = chosen.filter((d) =>
			what === 'pause' ? d.status !== 'paused' : what === 'resume' ? d.status === 'paused' : true
		);
		if (list.length === 0) return;
		const n = list.length === 1 ? '1 download' : `${list.length} downloads`;
		if (
			what === 'remove' &&
			!confirm(
				`Stop and remove ${n}?\n\nTheir data is deleted and each release is blocklisted, so another one is searched for.`
			)
		)
			return;
		busy = 'batch';
		const failed: string[] = [];
		try {
			for (const d of list) {
				try {
					if (what === 'remove') await del(`/downloads/${d.id}`, { delete_data: true, blocklist: true });
					else await post(`/downloads/${d.id}/${what}`);
				} catch (e) {
					failed.push(`${d.name}: ${e instanceof ApiError ? e.message : String(e)}`);
				}
			}
			error = failed.length ? `Not done for ${failed.length}: ${failed.join('; ')}` : '';
			notice = failed.length ? '' : `${what === 'pause' ? 'Paused' : what === 'resume' ? 'Resumed' : 'Removed'} ${n}.`;
			if (what === 'remove') picked = new Set();
			await load();
		} finally {
			busy = '';
		}
	}
</script>

<main>
	<div class="spread">
		<h1>Queue</h1>
		<div class="row">
			<button onclick={() => act('pause', () => post('/downloads/pause-all'))} disabled={!!busy || active.length === 0}>Pause all</button>
			<button onclick={() => act('resume', () => post('/downloads/resume-all'))} disabled={!!busy || active.length === 0}>Resume all</button>
		</div>
	</div>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if notice}<p class="notice">{notice}</p>{/if}

	{#each attention as i (i.download_id)}
		<div class="notice warn spread">
			<span><strong>Import needs attention:</strong> {i.name} — {i.error} {i.retry_at ? `(retrying ${ago(i.retry_at)})` : ''}</span>
			<button onclick={() => act(i.download_id, () => post(`/imports/${i.download_id}/retry`))} disabled={!!busy}>Retry now</button>
		</div>
	{/each}

	{#if downloads === null}
		<p class="muted">Loading…</p>
	{:else if active.length === 0}
		<p class="empty card">Nothing downloading. Movies you add are searched for and appear here.</p>
	{:else}
		<div class="row selectbar">
			<label class="inline check">
				<input
					type="checkbox"
					checked={allPicked}
					indeterminate={chosen.length > 0 && !allPicked}
					onchange={(e) => (picked = e.currentTarget.checked ? new Set(active.map((d) => d.id)) : new Set())}
					disabled={!!busy}
				/>
				{chosen.length ? `${chosen.length} selected` : 'Select all'}
			</label>
			{#if chosen.length}
				<button onclick={() => batch('pause')} disabled={!!busy || !chosen.some((d) => d.status !== 'paused')}>Pause</button>
				<button onclick={() => batch('resume')} disabled={!!busy || !chosen.some((d) => d.status === 'paused')}>Resume</button>
				<button class="danger" onclick={() => batch('remove')} disabled={!!busy}>Remove</button>
				<button class="link small" onclick={() => (picked = new Set())} disabled={!!busy}>Clear</button>
			{/if}
		</div>
		<div class="stack">
			{#each active as d (d.id)}
				<div class="card stack item" class:picked={picked.has(d.id)}>
					<div class="spread">
						<label class="inline check name">
							<input
								type="checkbox"
								aria-label="Select {d.name}"
								checked={picked.has(d.id)}
								onchange={(e) => pick(d.id, e.currentTarget.checked)}
								disabled={!!busy}
							/>
							<strong>{d.name}</strong>
						</label>
						<div class="row">
							<span class="badge {tone(d)}">{label(d)}</span>
							{#if d.status === 'paused'}
								<button onclick={() => act(d.id, () => post(`/downloads/${d.id}/resume`))} disabled={!!busy}>Resume</button>
							{:else}
								<button onclick={() => act(d.id, () => post(`/downloads/${d.id}/pause`))} disabled={!!busy}>Pause</button>
							{/if}
							<button class="danger" onclick={() => remove(d)} disabled={!!busy}>Remove</button>
						</div>
					</div>
					<div class="progress" aria-label="Progress"><div style="width: {Math.round(d.progress * 100)}%"></div></div>
					<div class="row small muted">
						<span>{Math.floor(d.progress * 100)}% of {bytes(d.size)}</span>
						{#if d.live}
							<span>· ↓ {rate(d.live.download_rate)}</span>
							<span>· ↑ {rate(d.live.upload_rate)}</span>
							<span>· {d.live.seeds} seeds, {d.live.peers} peers</span>
							{#if d.live.eta_seconds}<span>· {duration(d.live.eta_seconds)} left</span>{/if}
						{/if}
						{#if d.stalls}<span>· stalled {d.stalls}×</span>{/if}
					</div>
					<div class="small muted">
						Saving to {#if d.save_path}<Path path={d.save_path} />{:else}the client's own folder{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<h2>
		<button class="link" onclick={() => (showDone = !showDone)}>
			{showDone ? '▾' : '▸'} Finished ({done.length})
		</button>
	</h2>
	{#if showDone}
		<div class="card table-wrap">
			<table>
				<thead><tr><th>Name</th><th>Status</th><th>Size</th><th>Ratio</th><th>Finished</th><th></th></tr></thead>
				<tbody>
					{#each done as d (d.id)}
						<tr>
							<td class="name">
								{d.name}
								{#if d.save_path && d.imported_as !== 'in_place'}<div class="small muted">Downloaded to <Path path={d.save_path} /></div>{/if}
								{#if d.imported_to}
									<div class="small muted">
										Imported to <Path path={d.imported_to} />
										{#if d.imported_as === 'copy'}<span
												class="badge warn"
												title="A second full copy: the library folder and the download folder are on different drives"
												>copy</span
											>{/if}
									</div>
								{/if}
								{#if d.error}<div class="small err">{d.error}</div>{/if}
							</td>
							<td><span class="badge {tone(d)}">{label(d)}</span></td>
							<td>{bytes(d.size)}</td>
							<td>{d.ratio?.toFixed(2) ?? '—'}</td>
							<td class="muted small">{ago(d.completed_at ?? d.removed_at)}</td>
							<td>
								<div class="row">
									{#if seeding(d)}
										<button class="small" onclick={() => remove(d)} disabled={!!busy}>Stop seeding</button>
									{/if}
									{#if d.completed_at || d.status === 'failed'}
										<button class="small" onclick={() => again(d)} disabled={!!busy}>Download again</button>
									{/if}
								</div>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</main>

<style>
	.name {
		word-break: break-all;
	}
	.item {
		gap: 0.5rem;
	}
	.item.picked {
		outline: 2px solid var(--accent, var(--ok));
		outline-offset: -1px;
	}
	.selectbar {
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.6rem;
	}
	.check {
		display: inline-flex;
		gap: 0.5rem;
		align-items: center;
	}
	.err {
		color: var(--error);
	}
	.link {
		border: 0;
		background: none;
		padding: 0;
		font: inherit;
		color: var(--text);
	}
</style>
