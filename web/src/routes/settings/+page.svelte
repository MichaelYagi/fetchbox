<script lang="ts">
	import { ApiError, del, get, patch, post, type S } from '#lib/api.ts';
	import { session } from '#lib/session.svelte.ts';

	type Cfg = { tmdb: { api_key: string | null; language: string }; search: Record<string, unknown> };
	let config = $state<{ config: Cfg; overridden: string[] } | null>(null);
	let indexers = $state<S['IndexerOut'][]>([]);
	let definitions = $state<S['DefinitionOut'][]>([]);
	let clients = $state<S['ClientOut'][]>([]);
	let destinations = $state<S['DestinationOut'][]>([]);
	let health = $state<S['HealthReport'] | null>(null);
	let tests = $state<Record<string, S['TestResult']>>({});
	let error = $state('');
	let saved = $state('');
	let busy = $state('');

	let tmdbKey = $state('');
	let newIndexer = $state({ kind: 'torznab', name: '', url: '', api_key: '', definition: '' });
	let newClient = $state({ kind: 'qbittorrent', name: '', url: '', username: '', password: '' });

	async function load() {
		try {
			[config, indexers, definitions, clients, destinations, health] = await Promise.all([
				get<{ config: Cfg; overridden: string[] }>('/system/config'),
				get<S['IndexerOut'][]>('/indexers'),
				get<S['DefinitionOut'][]>('/indexer-definitions'),
				get<S['ClientOut'][]>('/download-clients'),
				get<S['DestinationOut'][]>('/destinations'),
				get<S['HealthReport']>('/system/health')
			]);
			session.health = health; // keep the header's badge in step
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
	});

	async function act(key: string, fn: () => Promise<unknown>, done = '') {
		busy = key;
		error = '';
		saved = '';
		try {
			await fn();
			saved = done;
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const saveTmdb = (e: SubmitEvent) => {
		e.preventDefault();
		act('tmdb', () => patch('/system/config', { tmdb: { api_key: tmdbKey.trim() || null } }), 'TMDB key saved.').then(
			() => (tmdbKey = '')
		);
	};

	const test = (kind: 'indexers' | 'download-clients', id: string) =>
		act(`test-${id}`, async () => {
			tests[id] = await post<S['TestResult']>(`/${kind}/${id}/test`);
		});

	function addIndexer(e: SubmitEvent) {
		e.preventDefault();
		const n = newIndexer;
		const body =
			n.kind === 'torznab'
				? { kind: 'torznab', name: n.name, settings: { url: n.url, api_key: n.api_key || null } }
				: {
						kind: 'definition',
						name: n.name || definitions.find((d) => d.slug === n.definition)?.name,
						settings: { definition: n.definition, ...(n.api_key ? { api_key: n.api_key } : {}) }
					};
		act('add-indexer', () => post('/indexers', body), 'Indexer added.').then(() => {
			if (!error) newIndexer = { kind: 'torznab', name: '', url: '', api_key: '', definition: '' };
		});
	}

	function addClient(e: SubmitEvent) {
		e.preventDefault();
		const n = newClient;
		const settings: Record<string, string> = { url: n.url };
		if (n.username) settings.username = n.username;
		if (n.password) settings.password = n.password;
		act('add-client', () => post('/download-clients', { kind: n.kind, name: n.name, settings }), 'Client added.').then(
			() => {
				if (!error) newClient = { kind: 'qbittorrent', name: '', url: '', username: '', password: '' };
			}
		);
	}

	const tone = (s: string) => (s === 'ok' || s === 'online' ? 'ok' : s === 'warning' || s === 'low_space' || s === 'degraded' ? 'warn' : 'error');
	const defn = $derived(definitions.find((d) => d.slug === newIndexer.definition));
	const needsKey = $derived(defn?.settings.some((s) => s.name === 'api_key') ?? false);
</script>

<main>
	<h1>Settings</h1>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if saved}<p class="notice">{saved}</p>{/if}

	<section id="health">
		<h2>Health</h2>
		{#if health}
			<div class="card">
				<table>
					<tbody>
						{#each health.checks as c (c.name)}
							<tr>
								<td class="w"><span class="badge {tone(c.status)}">{c.status}</span></td>
								<td class="w"><strong>{c.name}</strong></td>
								<td class="small">{c.message}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>

	<section id="tmdb">
		<h2>Movie information (TMDB)</h2>
		<form class="card stack" onsubmit={saveTmdb}>
			<p class="muted small">
				Searching for movies needs a free API key from
				<a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">themoviedb.org/settings/api</a>
				(the v3 key or the v4 read access token).
				{#if config?.config.tmdb.api_key}A key is set.{:else}<strong>No key is set yet.</strong>{/if}
			</p>
			{#if config?.overridden.includes('tmdb.api_key')}
				<p class="notice warn">Set by the FETCHBOX_TMDB_API_KEY environment variable.</p>
			{:else}
				<div class="row">
					<input class="grow" type="password" placeholder={config?.config.tmdb.api_key ? '••••••••  (replace)' : 'API key'} bind:value={tmdbKey} autocomplete="off" />
					<button class="primary" disabled={!tmdbKey.trim() || busy === 'tmdb'}>Save</button>
				</div>
			{/if}
		</form>
	</section>

	<section>
		<h2>Indexers</h2>
		<div class="card table-wrap">
			{#if indexers.length === 0}
				<p class="muted">No indexers: nothing can be searched. Add one below.</p>
			{:else}
				<table>
					<thead><tr><th>Name</th><th>Kind</th><th>Status</th><th></th></tr></thead>
					<tbody>
						{#each indexers as i (i.id)}
							<tr>
								<td><strong>{i.name}</strong></td>
								<td class="muted">{i.kind === 'definition' ? (i.settings.definition as string) : i.kind}</td>
								<td class="small">
									{#if !i.enabled}<span class="badge">Disabled</span>
									{:else if i.disabled_until}<span class="badge warn" title={i.last_error ?? ''}>Backing off</span>
									{:else}<span class="badge ok">Enabled</span>{/if}
									{#if tests[i.id]}<div class={tests[i.id].ok ? 'okt' : 'errt'}>{tests[i.id].message}</div>{/if}
								</td>
								<td><div class="row end">
									<button class="small" onclick={() => test('indexers', i.id)} disabled={!!busy}>Test</button>
									<button class="small" onclick={() => act(i.id, () => patch(`/indexers/${i.id}`, { enabled: !i.enabled }, i.etag))} disabled={!!busy}>
										{i.enabled ? 'Disable' : 'Enable'}
									</button>
									<button class="small danger" onclick={() => confirm(`Remove ${i.name}?`) && act(i.id, () => del(`/indexers/${i.id}`, undefined, i.etag))} disabled={!!busy}>Remove</button>
								</div></td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</div>
		<form class="card stack add" onsubmit={addIndexer}>
			<strong>Add an indexer</strong>
			<div class="row">
				<select bind:value={newIndexer.kind} aria-label="Kind">
					<option value="torznab">Torznab (Prowlarr, Jackett…)</option>
					<option value="definition">Built-in definition</option>
				</select>
				{#if newIndexer.kind === 'definition'}
					<select bind:value={newIndexer.definition} aria-label="Definition" required>
						<option value="" disabled>Choose…</option>
						{#each definitions as d (d.slug)}<option value={d.slug}>{d.name}</option>{/each}
					</select>
				{/if}
				<input placeholder="Name" bind:value={newIndexer.name} required={newIndexer.kind === 'torznab'} />
				{#if newIndexer.kind === 'torznab'}
					<input class="grow" placeholder="URL, e.g. http://localhost:9696/1" bind:value={newIndexer.url} required />
				{/if}
				{#if newIndexer.kind === 'torznab' || needsKey}
					<input type="password" placeholder="API key" bind:value={newIndexer.api_key} autocomplete="off" />
				{/if}
				<button class="primary" disabled={busy === 'add-indexer'}>Add</button>
			</div>
			{#if defn}<p class="muted small">{defn.description}</p>{/if}
		</form>
	</section>

	<section>
		<h2>Download clients</h2>
		<div class="card table-wrap">
			<table>
				<thead><tr><th>Name</th><th>Kind</th><th>Status</th><th></th></tr></thead>
				<tbody>
					{#each clients as c (c.id)}
						<tr>
							<td><strong>{c.name}</strong></td>
							<td class="muted">{c.kind}</td>
							<td class="small">
								<span class="badge {c.enabled ? (c.running ? 'ok' : 'warn') : ''}">{c.enabled ? (c.running ? 'Running' : 'Not running') : 'Disabled'}</span>
								{#if tests[c.id]}<div class={tests[c.id].ok ? 'okt' : 'errt'}>{tests[c.id].message}</div>{/if}
							</td>
							<td><div class="row end">
								<button class="small" onclick={() => test('download-clients', c.id)} disabled={!!busy}>Test</button>
								<button class="small" onclick={() => act(c.id, () => patch(`/download-clients/${c.id}`, { enabled: !c.enabled }, c.etag))} disabled={!!busy}>
									{c.enabled ? 'Disable' : 'Enable'}
								</button>
								{#if c.kind !== 'builtin'}
									<button class="small danger" onclick={() => confirm(`Remove ${c.name}?`) && act(c.id, () => del(`/download-clients/${c.id}`, undefined, c.etag))} disabled={!!busy}>Remove</button>
								{/if}
							</div></td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<form class="card stack add" onsubmit={addClient}>
			<strong>Add a download client</strong>
			<p class="muted small">Optional: the built-in engine works on its own.</p>
			<div class="row">
				<select bind:value={newClient.kind} aria-label="Kind">
					<option value="qbittorrent">qBittorrent</option>
					<option value="transmission">Transmission</option>
					<option value="deluge">Deluge</option>
				</select>
				<input placeholder="Name" bind:value={newClient.name} required />
				<input class="grow" placeholder="URL, e.g. http://localhost:8080" bind:value={newClient.url} required />
				{#if newClient.kind !== 'deluge'}<input placeholder="Username" bind:value={newClient.username} autocomplete="off" />{/if}
				<input type="password" placeholder="Password" bind:value={newClient.password} autocomplete="off" />
				<button class="primary" disabled={busy === 'add-client'}>Add</button>
			</div>
		</form>
	</section>

	<section>
		<h2>Library folders</h2>
		<div class="card table-wrap">
			<table>
				<thead><tr><th>Name</th><th>Folder</th><th>Status</th></tr></thead>
				<tbody>
					{#each destinations as d (d.id)}
						<tr>
							<td><strong>{d.name}</strong> <span class="muted small">{d.media_kind === 'movie' ? 'Movies' : 'TV'}</span></td>
							<td><code>{d.path}</code></td>
							<td class="small"><span class="badge {tone(d.status)}">{d.status}</span> {d.status_message ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
</main>

<style>
	section {
		margin-bottom: 1.5rem;
	}
	.grow {
		flex: 1;
		min-width: 14rem;
	}
	.add {
		margin-top: 0.75rem;
		gap: 0.5rem;
	}
	.add p {
		margin: 0;
	}
	.w {
		width: 1%;
		white-space: nowrap;
	}
	.end {
		justify-content: flex-end;
	}
	.okt {
		color: var(--ok);
	}
	.errt {
		color: var(--error);
	}
	code {
		word-break: break-all;
	}
</style>
