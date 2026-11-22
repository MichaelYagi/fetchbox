<script lang="ts">
	import { ApiError, del, get, patch, post, type S } from '#lib/api.ts';
	import BackupsCard from '#lib/BackupsCard.svelte';
	import LibraryCard from '#lib/LibraryCard.svelte';
	import LibraryCheckCard from '#lib/LibraryCheckCard.svelte';
	import MediaServersCard from '#lib/MediaServersCard.svelte';
	import NotificationsCard from '#lib/NotificationsCard.svelte';
	import Path from '#lib/Path.svelte';
	import { session } from '#lib/session.svelte.ts';

	type Cfg = {
		tmdb: { api_key: string | null; language: string };
		tvdb?: { api_key: string | null; pin: string | null };
		search: Record<string, unknown>;
	};
	let config = $state<{ config: Cfg; overridden: string[] } | null>(null);
	let indexers = $state<S['IndexerOut'][]>([]);
	let definitions = $state<S['DefinitionOut'][]>([]);
	let clients = $state<S['ClientOut'][]>([]);
	let destinations = $state<S['DestinationOut'][]>([]);
	let namingOptions = $state<S['NamingOptionsOut'] | null>(null);
	let health = $state<S['HealthReport'] | null>(null);
	let tests = $state<Record<string, S['TestResult']>>({});
	let error = $state('');
	let saved = $state('');
	let busy = $state('');

	let tmdbKey = $state('');
	let tvdbKey = $state('');
	let tvdbPin = $state('');
	let newIndexer = $state({ kind: 'prowlarr', name: '', url: '', api_key: '', definition: '' });
	let newClient = $state({ kind: 'qbittorrent', name: '', url: '', username: '', password: '' });
	// Inline editing: one row at a time. Secrets left blank keep what is stored.
	let editing = $state<string | null>(null);
	let draft = $state({
		name: '',
		url: '',
		api_key: '',
		username: '',
		password: '',
		priority: 25,
		local: '',
		remote: '',
		folder: '',
		iface: '',
		kill: true
	});
	let interfaces = $state<S['NetworkInterface'][]>([]);

	function editIndexer(i: S['IndexerOut']) {
		const st = i.settings as Record<string, unknown>;
		draft = {
			name: i.name,
			url: (st.url as string) ?? '',
			api_key: '',
			username: '',
			password: '',
			priority: i.priority,
			local: '',
			remote: '',
			folder: '',
			iface: '',
			kill: true
		};
		editing = i.id;
	}

	function editClient(c: S['ClientOut']) {
		const st = c.settings as Record<string, unknown>;
		const map = (c.policy.path_mappings ?? [])[0];
		draft = {
			name: c.name,
			url: (st.url as string) ?? '',
			api_key: '',
			username: (st.username as string) ?? '',
			password: '',
			priority: c.priority,
			local: map?.local ?? '',
			remote: map?.client ?? '',
			folder: c.policy.save_path ?? '',
			iface: (st.interface as string) ?? '',
			kill: (st.kill_switch as boolean) ?? true
		};
		editing = c.id;
		if (c.kind === 'builtin') get<S['NetworkInterface'][]>('/system/network-interfaces').then((x) => (interfaces = x));
	}

	// Torrent clients in the order they're tried; the first enabled, running one is used.
	const torrentClients = $derived(clients.filter((c) => c.kind !== 'direct'));
	const directClients = $derived(clients.filter((c) => c.kind === 'direct'));
	const inUse = $derived(torrentClients.find((c) => c.enabled && c.running)?.id);

	function move(c: S['ClientOut'], by: -1 | 1) {
		const order = [...torrentClients];
		const at = order.findIndex((x) => x.id === c.id);
		const to = at + by;
		if (to < 0 || to >= order.length) return;
		[order[at], order[to]] = [order[to], order[at]];
		// Store the visible order as priorities 1, 2, 3... (only those that change).
		act(`move-${c.id}`, async () => {
			for (const [n, x] of order.entries()) {
				if (x.priority !== n + 1) {
					await patch(`/download-clients/${x.id}`, { priority: n + 1 }, x.etag);
				}
			}
		});
	}

	function saveIndexer(e: SubmitEvent, i: S['IndexerOut']) {
		e.preventDefault();
		const settings: Record<string, unknown> = {};
		if (i.kind === 'torznab' || i.kind === 'prowlarr') settings.url = draft.url;
		if (draft.api_key.trim()) settings.api_key = draft.api_key.trim();
		const body: Record<string, unknown> = { name: draft.name, priority: draft.priority };
		if (Object.keys(settings).length) body.settings = settings;
		act(`save-${i.id}`, () => patch(`/indexers/${i.id}`, body, i.etag), `${draft.name} saved.`).then(
			() => {
				if (!error) editing = null;
			}
		);
	}

	function saveClient(e: SubmitEvent, c: S['ClientOut']) {
		e.preventDefault();
		// The built-in clients have nothing to connect to: only their priority is edited
		// (sending settings would restart the torrent engine for nothing).
		const builtIn = c.kind === 'builtin' || c.kind === 'direct';
		const settings: Record<string, unknown> = { url: draft.url };
		if (c.kind !== 'deluge') settings.username = draft.username || null;
		if (draft.password) settings.password = draft.password;
		const mappings =
			draft.local.trim() && draft.remote.trim()
				? [{ local: draft.local.trim(), client: draft.remote.trim() }]
				: [];
		const folder = draft.folder.trim() || null; // empty: back to the default
		const st = c.settings as Record<string, unknown>;
		// Only touch the engine's settings when they change: that restarts it.
		const bindChanged =
			c.kind === 'builtin' && ((st.interface ?? '') !== draft.iface || (st.kill_switch ?? true) !== draft.kill);
		const body = builtIn
			? {
					policy: { save_path: folder },
					...(bindChanged ? { settings: { interface: draft.iface || null, kill_switch: draft.kill } } : {})
				}
			: { settings, policy: { path_mappings: mappings, save_path: folder } };
		act(`save-${c.id}`, () => patch(`/download-clients/${c.id}`, body, c.etag), `${c.name} saved.`).then(
			() => {
				if (!error) editing = null;
			}
		);
	}

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
		get<S['NamingOptionsOut']>('/library/naming-options').then((o) => (namingOptions = o));
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

	const saveTvdb = (e: SubmitEvent) => {
		e.preventDefault();
		const tvdb: Record<string, string | null> = { api_key: tvdbKey.trim() || null };
		if (tvdbPin.trim()) tvdb.pin = tvdbPin.trim();
		act('tvdb', () => patch('/system/config', { tvdb }), tvdb.api_key ? 'TVDB key saved.' : 'TVDB key removed.').then(() => {
			tvdbKey = '';
			tvdbPin = '';
		});
	};

	const test = (kind: 'indexers' | 'download-clients', id: string) =>
		act(`test-${id}`, async () => {
			tests[id] = await post<S['TestResult']>(`/${kind}/${id}/test`);
		});

	function addIndexer(e: SubmitEvent) {
		e.preventDefault();
		const n = newIndexer;
		const body =
			n.kind === 'torznab' || n.kind === 'prowlarr'
				? {
						kind: n.kind,
						name: n.name || (n.kind === 'prowlarr' ? 'Prowlarr' : 'Torznab'),
						settings: { url: n.url, api_key: n.api_key || null }
					}
				: {
						kind: 'definition',
						name: n.name || definitions.find((d) => d.slug === n.definition)?.name,
						settings: { definition: n.definition, ...(n.api_key ? { api_key: n.api_key } : {}) }
					};
		act('add-indexer', () => post('/indexers', body), 'Indexer added.').then(() => {
			if (!error) newIndexer = { kind: 'prowlarr', name: '', url: '', api_key: '', definition: '' };
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

	{#if session.host}
		<section id="computer">
			<h2>This computer</h2>
			<div class="card">
				<table>
					<tbody>
						<tr>
							<td class="w muted">Running on</td>
							<td>
								<strong>{session.host.name}</strong>
								{session.host.wsl_distro
									? `· Linux (${session.host.wsl_distro}) in WSL on Windows`
									: `· ${session.host.os}`}
								<div class="muted small">
									Every folder in these settings is a folder on this machine{session.host.wsl_distro
										? '; under each one is where Windows finds it'
										: ''}.
								</div>
							</td>
						</tr>
						<tr><td class="w muted">Downloads</td><td><Path path={session.host.downloads_root} /></td></tr>
						<tr><td class="w muted">Fetchbox data</td><td><Path path={session.host.data_dir} /></td></tr>
					</tbody>
				</table>
			</div>
		</section>
	{/if}

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
		<h2>Movie and show information (TMDB)</h2>
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
		<form class="card stack tvdb" onsubmit={saveTvdb}>
			<p class="muted small">
				<strong>Anime, optional:</strong> releases numbered by absolute episode (“Show - 1071”) need TheTVDB's numbering,
				which TMDB doesn't have. Get an API key at
				<a href="https://thetvdb.com/api-information" target="_blank" rel="noreferrer">thetvdb.com/api-information</a>
				(a personal key also needs your subscriber PIN). Shows are updated at their next refresh.
				{#if config?.config.tvdb?.api_key}A key is set.{/if}
			</p>
			<div class="row">
				<input class="grow" type="password" placeholder={config?.config.tvdb?.api_key ? '••••••••  (replace)' : 'TVDB API key'} bind:value={tvdbKey} autocomplete="off" />
				<input type="password" placeholder="PIN (if needed)" bind:value={tvdbPin} autocomplete="off" />
				<button class="primary" disabled={!tvdbKey.trim() || busy === 'tvdb'}>Save</button>
			</div>
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
								<td class="muted">
									{i.kind === 'definition' ? (i.settings.definition as string) : i.kind}
									<span class="small">· priority {i.priority}</span>
								</td>
								<td class="small">
									{#if !i.enabled}<span class="badge">Disabled</span>
									{:else if i.disabled_until}<span class="badge warn" title={i.last_error ?? ''}>Backing off</span>
									{:else}<span class="badge ok">Enabled</span>{/if}
									{#if tests[i.id]}<div class={tests[i.id].ok ? 'okt' : 'errt'}>{tests[i.id].message}</div>{/if}
								</td>
								<td><div class="row end">
									<button class="small" onclick={() => (editing === i.id ? (editing = null) : editIndexer(i))} disabled={!!busy}>
										{editing === i.id ? 'Cancel' : 'Edit'}
									</button>
									<button class="small" onclick={() => test('indexers', i.id)} disabled={!!busy}>Test</button>
									<button class="small" onclick={() => act(i.id, () => patch(`/indexers/${i.id}`, { enabled: !i.enabled }, i.etag))} disabled={!!busy}>
										{i.enabled ? 'Disable' : 'Enable'}
									</button>
									<button class="small danger" onclick={() => confirm(`Remove ${i.name}?`) && act(i.id, () => del(`/indexers/${i.id}`, undefined, i.etag))} disabled={!!busy}>Remove</button>
								</div></td>
							</tr>
							{#if editing === i.id}
								<tr class="editrow">
									<td colspan="4">
										<form class="row" onsubmit={(e) => saveIndexer(e, i)}>
											<label>Name <input bind:value={draft.name} required /></label>
											{#if i.kind === 'torznab' || i.kind === 'prowlarr'}
												<label class="grow">URL <input bind:value={draft.url} required placeholder="http://localhost:9696/3" /></label>
											{/if}
											<label>API key <input type="password" bind:value={draft.api_key} placeholder="unchanged" autocomplete="off" /></label>
											<label class="narrow">Priority <input type="number" min="1" max="50" bind:value={draft.priority} /></label>
											<button class="primary" disabled={!!busy}>Save</button>
										</form>
										<p class="muted small hint">Lower priority numbers are preferred. Leave the API key empty to keep the stored one.</p>
									</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			{/if}
		</div>
		<form class="card stack add" onsubmit={addIndexer}>
			<strong>Add an indexer</strong>
			<div class="row">
				<select bind:value={newIndexer.kind} aria-label="Kind">
					<option value="prowlarr">Prowlarr (all its indexers)</option>
					<option value="torznab">Torznab feed (Jackett, one Prowlarr indexer…)</option>
					<option value="definition">Built-in definition</option>
				</select>
				{#if newIndexer.kind === 'definition'}
					<select bind:value={newIndexer.definition} aria-label="Definition" required>
						<option value="" disabled>Choose…</option>
						{#each definitions as d (d.slug)}<option value={d.slug}>{d.name}</option>{/each}
					</select>
				{/if}
				<input placeholder="Name" bind:value={newIndexer.name} />
				{#if newIndexer.kind === 'prowlarr'}
					<input class="grow" placeholder="Prowlarr address, e.g. http://192.168.1.10:9696" bind:value={newIndexer.url} required />
				{:else if newIndexer.kind === 'torznab'}
					<input class="grow" placeholder="Torznab URL" bind:value={newIndexer.url} required />
				{/if}
				{#if newIndexer.kind === 'torznab' || newIndexer.kind === 'prowlarr' || needsKey}
					<input type="password" placeholder="API key" bind:value={newIndexer.api_key} autocomplete="off" />
				{/if}
				<button class="primary" disabled={busy === 'add-indexer'}>Add</button>
			</div>
			{#if newIndexer.kind === 'prowlarr'}
				<p class="muted small">
					The address you open Prowlarr at, and its API key from Prowlarr's Settings → General →
					Security. Every enabled indexer in Prowlarr is searched; ones you add there later are
					picked up automatically.
				</p>
			{/if}
			{#if defn}<p class="muted small">{defn.description}</p>{/if}
		</form>
	</section>

	<section>
		<h2>Download clients</h2>
		<div class="card table-wrap">
			<table>
				<thead><tr><th>Name</th><th>Kind</th><th>Status</th><th></th></tr></thead>
				<tbody>
					{#each torrentClients as c, n (c.id)}
						<tr>
							<td>
								<div class="row">
									<div class="order">
										<button class="tiny" title="Move up" aria-label="Move {c.name} up" onclick={() => move(c, -1)} disabled={!!busy || n === 0}>▲</button>
										<button class="tiny" title="Move down" aria-label="Move {c.name} down" onclick={() => move(c, 1)} disabled={!!busy || n === torrentClients.length - 1}>▼</button>
									</div>
									<strong>{c.name}</strong>
									{#if c.id === inUse}<span class="badge info">Used for torrents</span>{/if}
								</div>
								<div class="muted small where">
									Saves to {#if c.saves_to}<Path path={c.saves_to} />{:else}{c.name}'s own download folder{/if}
								</div>
							</td>
							<td class="muted">{c.kind}</td>
							<td class="small">
								<span class="badge {c.enabled ? (c.running ? 'ok' : 'warn') : ''}">{c.enabled ? (c.running ? 'Running' : 'Not running') : 'Disabled'}</span>
								{#if tests[c.id]}<div class={tests[c.id].ok ? 'okt' : 'errt'}>{tests[c.id].message}</div>{/if}
							</td>
							<td><div class="row end">
								<button class="small" onclick={() => (editing === c.id ? (editing = null) : editClient(c))} disabled={!!busy}>
									{editing === c.id ? 'Cancel' : 'Edit'}
								</button>
								<button class="small" onclick={() => test('download-clients', c.id)} disabled={!!busy}>Test</button>
								<button class="small" onclick={() => act(c.id, () => patch(`/download-clients/${c.id}`, { enabled: !c.enabled }, c.etag))} disabled={!!busy}>
									{c.enabled ? 'Disable' : 'Enable'}
								</button>
								{#if c.kind !== 'builtin' && c.kind !== 'direct'}
									<button class="small danger" onclick={() => confirm(`Remove ${c.name}?`) && act(c.id, () => del(`/download-clients/${c.id}`, undefined, c.etag))} disabled={!!busy}>Remove</button>
								{/if}
							</div></td>
						</tr>
						{#if editing === c.id}
							<tr class="editrow">
								<td colspan="4">
									<form class="row" onsubmit={(e) => saveClient(e, c)}>
										{#if c.kind !== 'builtin' && c.kind !== 'direct'}
											<label class="grow">URL <input bind:value={draft.url} required /></label>
											{#if c.kind !== 'deluge'}<label>Username <input bind:value={draft.username} autocomplete="off" /></label>{/if}
											<label>Password <input type="password" bind:value={draft.password} placeholder="unchanged" autocomplete="off" /></label>
										{/if}
										<label class="grow">
											Download folder
											<input
												bind:value={draft.folder}
												placeholder={c.kind === 'builtin' || c.kind === 'direct' ? 'default: ~/Downloads/Fetchbox/Torrents' : `empty: ${c.name}'s own folder`}
											/>
										</label>
										<button class="primary" disabled={!!busy}>Save</button>
									</form>
									{#if c.kind === 'builtin'}
										<div class="mapping">
											<div class="row">
												<label class="grow">
													Only download through
													<select bind:value={draft.iface}>
														<option value="">Any network connection</option>
														{#each interfaces as n (n.name)}
															<option value={n.name}>
																{n.name}{n.vpn ? ' (VPN)' : ''} — {n.address ?? 'down'}
															</option>
														{/each}
														{#if draft.iface && !interfaces.some((n) => n.name === draft.iface)}
															<option value={draft.iface}>{draft.iface} (not found now)</option>
														{/if}
													</select>
												</label>
												{#if draft.iface}
													<label class="inline-check"
														><input type="checkbox" bind:checked={draft.kill} /> Pause all torrents if it disconnects
														(kill switch)</label
													>
												{/if}
											</div>
											<p class="muted small hint">
												Pick your VPN's connection to keep torrent traffic on it. With the kill switch, every torrent pauses
												while the VPN is down and resumes when it's back. Saving this restarts the engine briefly.
											</p>
										</div>
									{/if}
									{#if c.kind !== 'builtin' && c.kind !== 'direct'}
									<div class="mapping">
										<p class="muted small hint">
											<strong>Path mapping</strong>, only if this client runs on another machine or in
											Docker: the same folder as Fetchbox sees it and as the client sees it.
										</p>
										<div class="row">
											<label class="grow">Fetchbox path <input bind:value={draft.local} placeholder="/mnt/t/Torrents" /></label>
											<label class="grow">Client path <input bind:value={draft.remote} placeholder="/Users/me/Torrents" /></label>
										</div>
									</div>
									{/if}
									<p class="muted small hint">The download folder applies to new downloads; ones in progress stay where they are.</p>
								</td>
							</tr>
						{/if}
					{/each}
					{#each directClients as c (c.id)}
						<tr class="direct">
							<td><div class="row"><span class="order"></span><strong>{c.name}</strong>
								{#if c.enabled && c.running}<span class="badge">Used for direct files</span>{/if}</div>
								<div class="muted small where">Saves to <Path path={c.saves_to} /></div></td>
							<td class="muted">{c.kind}</td>
							<td class="small">
								<span class="badge {c.enabled ? (c.running ? 'ok' : 'warn') : ''}">{c.enabled ? (c.running ? 'Running' : 'Not running') : 'Disabled'}</span>
								{#if tests[c.id]}<div class={tests[c.id].ok ? 'okt' : 'errt'}>{tests[c.id].message}</div>{/if}
							</td>
							<td><div class="row end">
								<button class="small" onclick={() => (editing === c.id ? (editing = null) : editClient(c))} disabled={!!busy}>
				{editing === c.id ? 'Cancel' : 'Edit'}
			</button>
			<button class="small" onclick={() => test('download-clients', c.id)} disabled={!!busy}>Test</button>
								<button class="small" onclick={() => act(c.id, () => patch(`/download-clients/${c.id}`, { enabled: !c.enabled }, c.etag))} disabled={!!busy}>
									{c.enabled ? 'Disable' : 'Enable'}
								</button>
							</div></td>
						</tr>
	{#if editing === c.id}
		<tr class="editrow">
			<td colspan="4">
				<form class="row" onsubmit={(e) => saveClient(e, c)}>
					<label class="grow">
						Download folder
						<input bind:value={draft.folder} placeholder="default: ~/Downloads/Fetchbox/Torrents" />
					</label>
					<button class="primary" disabled={!!busy}>Save</button>
				</form>
				<p class="muted small hint">The download folder applies to new downloads; ones in progress stay where they are.</p>
			</td>
		</tr>
	{/if}
{/each}
				</tbody>
			</table>
		</div>
		<p class="muted small hint">
			Torrents go to the first client in this list that is enabled and running; use ▲ ▼ to
			change the order. Direct downloads (web-seeded files, e.g. Internet Archive films) always
			use the direct client.
		</p>
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

	<section id="library">
		<h2>Library</h2>
		<p class="muted small">Where finished downloads go and how they're named. Most people only pick a folder and a style.</p>
		{#if namingOptions}
			<div class="libs">
				{#each ['movie', 'series'] as const as kind (kind)}
					<LibraryCard {kind} dest={destinations.find((d) => d.media_kind === kind && d.is_default) ?? destinations.find((d) => d.media_kind === kind)} options={namingOptions} onChanged={load} />
				{/each}
				<LibraryCheckCard />
			</div>
		{/if}
	</section>

	<section id="media-servers">
		<h2>Media servers</h2>
		<MediaServersCard {destinations} />
	</section>

	<section id="notifications">
		<h2>Notifications</h2>
		<NotificationsCard />
	</section>

	<section id="backups">
		<h2>Backups</h2>
		<BackupsCard />
	</section>
</main>

<style>
	.inline-check {
		display: flex;
		gap: 0.4rem;
		align-items: center;
		align-self: flex-end;
	}
	section {
		margin-bottom: 1.5rem;
	}
	.libs {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 1rem;
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
	.order {
		display: inline-flex;
		flex-direction: column;
		gap: 2px;
		width: 1.6rem;
	}
	.tiny {
		padding: 0 0.3rem;
		font-size: 0.6rem;
		line-height: 1.3;
	}
	.where {
		margin: 0.2rem 0 0 2.1rem;
	}
	.mapping {
		margin-top: 0.6rem;
	}
	tr.direct td {
		color: var(--muted);
	}
	.editrow td {
		background: var(--surface-2);
	}
	.editrow form {
		align-items: end;
	}
	.narrow input {
		width: 5rem;
	}
	.hint {
		margin: 0.4rem 0 0;
	}
	.okt {
		color: var(--ok);
	}
	.errt {
		color: var(--error);
	}
</style>
