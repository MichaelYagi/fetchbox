<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import { ago, ApiError, del, get, type S } from '#lib/api.ts';

	// Activity: what Fetchbox did, newest first (the activity log), and the releases it
	// won't grab again (the blocklist), each with Unblock.
	type Entry = S['ActivityOut'];
	type Block = S['BlockOut'];
	let tab = $state<'history' | 'blocklist'>('history');
	let entries = $state<Entry[] | null>(null);
	let cursor = $state<string | null>(null);
	let blocks = $state<Block[] | null>(null);
	let names = $state<Record<string, string>>({}); // title id -> name, for links
	let category = $state('');
	let problems = $state(false);
	let error = $state('');
	let busy = $state(false);

	// What there is to filter by (the log's categories, in words)
	const CATEGORIES: [string, string][] = [
		['', 'Everything'],
		['title', 'Titles'],
		['release', 'Grabs'],
		['download', 'Downloads'],
		['import', 'Imports'],
		['file', 'Files'],
		['library', 'Library'],
		['plugin', 'Plugins'],
		['source', 'Indexers'],
		['storage', 'Folders'],
		['system', 'System'],
		['job', 'Jobs']
	];

	async function loadHistory(more = false) {
		busy = true;
		error = '';
		try {
			const params: Record<string, string | number> = { limit: 100 };
			if (category) params['filter[category]'] = category;
			if (problems) params['filter[outcome]'] = 'failure';
			if (more && cursor) params.cursor = cursor;
			const page = await get<S['ActivityPage']>('/activity', params);
			entries = more && entries ? [...entries, ...page.items] : page.items;
			cursor = page.next_cursor ?? null;
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
	async function loadBlocks() {
		try {
			blocks = await get<Block[]>('/blocklist');
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	async function loadNames() {
		try {
			const page = await get<S['TitlePage']>('/titles', { limit: 500 });
			names = Object.fromEntries(page.items.map((t) => [t.id, t.year ? `${t.name} (${t.year})` : t.name]));
		} catch {
			names = {};
		}
	}
	$effect(() => {
		loadNames();
		loadBlocks();
	});
	$effect(() => {
		// again whenever a filter changes
		void category;
		void problems;
		loadHistory();
	});

	// the title an entry is about, if it's one still in the library
	function titleOf(e: Entry): string | null {
		const id = e.subject_type === 'title' ? e.subject_id : ((e.data?.title_id as string | undefined) ?? null);
		return id && names[id] ? id : null;
	}
	const label = (c: string) => CATEGORIES.find(([k]) => k === c)?.[1] ?? c;

	async function unblock(b: Block) {
		const what = b.release_title ?? b.info_hash ?? 'this release';
		if (!(await ask(`Unblock ${what}? It may be grabbed again.`, 'Unblock'))) return;
		try {
			await del(`/blocklist/${b.id}`);
			blocks = (blocks ?? []).filter((x) => x.id !== b.id);
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
</script>

<main>
	<div class="spread">
		<h1>Activity</h1>
		<div class="row tabs" role="tablist">
			<button role="tab" aria-selected={tab === 'history'} class:active={tab === 'history'} onclick={() => (tab = 'history')}>History</button>
			<button role="tab" aria-selected={tab === 'blocklist'} class:active={tab === 'blocklist'} onclick={() => (tab = 'blocklist')}>
				Blocklist{#if blocks?.length} ({blocks.length}){/if}
			</button>
		</div>
	</div>
	{#if error}<p class="notice error">{error}</p>{/if}

	{#if tab === 'history'}
		<div class="row small filters">
			<select bind:value={category} aria-label="What">
				{#each CATEGORIES as [value, name] (value)}<option {value}>{name}</option>{/each}
			</select>
			<label class="inline"><input type="checkbox" bind:checked={problems} /> Only problems</label>
		</div>
		{#if entries === null}
			<p class="muted">Loading…</p>
		{:else if entries.length === 0}
			<p class="muted">Nothing yet{category || problems ? ' for that' : ''}.</p>
		{:else}
			<div class="table-wrap">
				<table class="log">
					<tbody>
						{#each entries as e (e.id)}
							{@const tid = titleOf(e)}
							<tr class:failed={e.outcome === 'failure'}>
								<td class="when muted small" title={new Date(e.created_at).toLocaleString()}>{ago(e.created_at)}</td>
								<td class="what"><span class="badge">{label(e.category)}</span></td>
								<td>
									{e.message}
									{#if tid}<a class="small" href="/title/{tid}">{names[tid]}</a>{/if}
								</td>
								<td class="who muted small">{e.actor ?? ''}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			{#if cursor}<button onclick={() => loadHistory(true)} disabled={busy}>{busy ? 'Loading…' : 'Older'}</button>{/if}
		{/if}
	{:else if blocks === null}
		<p class="muted">Loading…</p>
	{:else if blocks.length === 0}
		<p class="muted">Nothing is blocklisted. A release is, when you use Redownload or when its download fails, so it isn't grabbed again.</p>
	{:else}
		<p class="muted small">Releases Fetchbox won't grab again. Unblock one to let it be grabbed.</p>
		<div class="table-wrap">
			<table class="log">
				<tbody>
					{#each blocks as b (b.id)}
						<tr>
							<td class="when muted small" title={new Date(b.created_at).toLocaleString()}>{ago(b.created_at)}</td>
							<td>
								<span class="release">{b.release_title ?? b.info_hash ?? b.guid ?? '?'}</span>
								{#if b.title_id && names[b.title_id]}<a class="small" href="/title/{b.title_id}">{names[b.title_id]}</a>{/if}
								{#if b.reason}<div class="muted small">{b.reason}</div>{/if}
							</td>
							<td class="act"><button class="small" onclick={() => unblock(b)}>Unblock</button></td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</main>

<style>
	.tabs button.active {
		border-color: var(--accent);
		color: var(--text);
	}
	.filters {
		margin: 0.5rem 0 0.75rem;
	}
	.log {
		width: 100%;
	}
	.log td {
		vertical-align: middle;
	}
	.log a {
		margin-left: 0.4rem;
	}
	.when {
		white-space: nowrap;
		width: 6rem;
	}
	.what {
		width: 6.5rem;
	}
	.who {
		white-space: nowrap;
		text-align: right;
	}
	.act {
		text-align: right;
		width: 6rem;
	}
	.release {
		overflow-wrap: anywhere;
	}
	tr.failed td:not(.when) {
		color: var(--error, #e5484d);
	}
	@media (max-width: 640px) {
		.who,
		.what {
			display: none;
		}
	}
</style>
