<script lang="ts">
	import { goto } from '$app/navigation';
	import MediaServersCard from '#lib/MediaServersCard.svelte';
	import { ApiError, get, patch, post, type S } from '#lib/api.ts';

	// First-run setup: one step at a time, every step optional (Skip), the whole thing too
	// (Skip setup). Each step uses the same endpoints as Settings, so what's done here is
	// what Settings shows. Run it again any time from Settings > General.
	type Status = S['SetupStatus'];
	const STEPS = ['Welcome', 'Library folders', 'Movies and TV', 'Indexer', 'Downloads', 'Media server', 'Done'] as const;
	let step = $state(0);
	let status = $state<Status | null>(null);
	let destinations = $state<S['DestinationOut'][]>([]);
	let skipped = $state<Set<number>>(new Set());
	let saved = $state<Set<number>>(new Set()); // steps done here (folders saved, the engine chosen)
	let busy = $state(false);
	let error = $state('');
	let note = $state('');

	async function load() {
		try {
			[status, destinations] = await Promise.all([get<Status>('/system/setup'), get<S['DestinationOut'][]>('/destinations')]);
			if (!Object.keys(paths).length) paths = Object.fromEntries(status.folders.map((f) => [f.id, f.path]));
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
	});

	// whether a step has something set up (a ✓ beside it)
	const done = (i: number): boolean => {
		const s = status;
		if (!s) return false;
		return [false, saved.has(1), s.tmdb, s.indexers > 0, saved.has(4) || s.download_clients > 0, s.media_servers > 0, false][i] ?? false;
	};
	const go = (i: number) => ((step = i), (error = ''), (note = ''));
	const skip = () => ((skipped = new Set([...skipped, step])), go(step + 1));

	async function finish() {
		try {
			await patch('/system/config', { setup: { dismissed: true } });
		} catch {
			// not essential: it just may open again
		}
	}
	async function leave() {
		await finish();
		await goto('/');
	}

	async function run(what: () => Promise<void>) {
		busy = true;
		error = note = '';
		try {
			await what();
			await load();
		} catch (e) {
			error = e instanceof ApiError ? [e.message, ...e.fields.map((f) => f.message)].join(' · ') : String(e);
		} finally {
			busy = false;
		}
	}

	// 1. library folders
	let paths = $state<Record<string, string>>({});
	const KIND: Record<string, string> = { movie: 'Movies', series: 'TV shows', music: 'Music' };
	const ORDER = ['movie', 'series', 'music'];
	const saveFolders = () =>
		run(async () => {
			for (const f of status?.folders ?? []) {
				const p = (paths[f.id] ?? '').trim();
				if (p && p !== f.path) await patch(`/destinations/${f.id}`, { path: p }, f.etag);
			}
			saved = new Set([...saved, 1]);
			go(step + 1);
		});

	// 2. TMDB
	let tmdbKey = $state('');
	const saveTmdb = () =>
		run(async () => {
			await patch('/system/config', { tmdb: { api_key: tmdbKey.trim() } });
			note = 'TMDB key saved.';
		});

	// 3. indexer (Prowlarr)
	let prowlarr = $state({ url: '', api_key: '' });
	const addIndexer = () =>
		run(async () => {
			const ix = await post<S['IndexerOut']>('/indexers', {
				kind: 'prowlarr',
				name: 'Prowlarr',
				settings: { url: prowlarr.url.trim(), api_key: prowlarr.api_key.trim() || null }
			});
			const t = await post<S['TestResult']>(`/indexers/${ix.id}/test`);
			note = t.ok ? `Prowlarr added: ${t.message}` : '';
			if (!t.ok) throw new Error(`Added, but the test failed: ${t.message}`);
		});

	// 4. downloads: the built-in engine, or a client of your own
	let own = $state(false);
	let client = $state({ kind: 'qbittorrent', url: '', username: '', password: '', api_key: '' });
	const CLIENT_NAMES: Record<string, string> = {
		qbittorrent: 'qBittorrent', transmission: 'Transmission', deluge: 'Deluge', sabnzbd: 'SABnzbd', nzbget: 'NZBGet'
	};
	const addClient = () =>
		run(async () => {
			const settings: Record<string, string> = { url: client.url.trim() };
			if (client.kind === 'sabnzbd') settings.api_key = client.api_key.trim();
			else {
				if (client.username && client.kind !== 'deluge') settings.username = client.username;
				if (client.password) settings.password = client.password;
			}
			const name = CLIENT_NAMES[client.kind] ?? client.kind;
			const c = await post<S['ClientOut']>('/download-clients', { kind: client.kind, name, settings });
			const t = await post<S['TestResult']>(`/download-clients/${c.id}/test`);
			note = t.ok ? `${name} added: ${t.message}` : '';
			if (!t.ok) throw new Error(`Added, but the test failed: ${t.message}`);
		});
</script>

<main class="setup">
	<div class="spread">
		<h1>Set up Fetchbox</h1>
		{#if step < STEPS.length - 1}<button class="link" onclick={leave}>Skip setup</button>{/if}
	</div>
	<ol class="steps" aria-label="Steps">
		{#each STEPS as name, i (name)}
			<li class:current={i === step} class:past={i < step}>
				<button class="link" onclick={() => go(i)}>
					<span class="mark">{done(i) && i > 0 && i < STEPS.length - 1 ? '✓' : i + 1}</span>
					{name}
				</button>
			</li>
		{/each}
	</ol>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if note}<p class="notice">{note}</p>{/if}

	<div class="card stack">
		{#if step === 0}
			<h2>Welcome</h2>
			<p>
				Fetchbox finds the movies, shows and music you want, downloads them and files them in your library. A few things
				help it do that: where your library is, where it looks things up, and where it searches for releases.
			</p>
			<p class="muted">Every step is optional: skip any of them, or all of them, and set them up later in Settings.</p>
			<div class="row">
				<button class="primary" onclick={() => go(1)}>Start</button>
				<button onclick={leave}>Skip setup</button>
			</div>
		{:else if step === 1}
			<h2>Library folders</h2>
			<p class="muted">Where each kind goes. Point them at your NAS, for example. You can change them, and add more, in Settings > Library.</p>
			{#each [...(status?.folders ?? [])].sort((a, b) => ORDER.indexOf(a.media_kind) - ORDER.indexOf(b.media_kind)) as f (f.id)}
				<label class="field">
					<span>{KIND[f.media_kind] ?? f.name}</span>
					<input bind:value={paths[f.id]} spellcheck="false" aria-label="{KIND[f.media_kind] ?? f.name} folder" />
				</label>
			{/each}
			<div class="row">
				<button class="primary" onclick={saveFolders} disabled={busy}>Save and continue</button>
				<button onclick={skip}>Skip</button>
			</div>
		{:else if step === 2}
			<h2>Movies and TV information</h2>
			<p class="muted">
				Movies and shows are looked up on TMDB, which needs a free key: sign up at
				<a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">themoviedb.org/settings/api</a> and paste
				the API key (v3) or the read access token. Music needs nothing (MusicBrainz).
			</p>
			{#if status?.tmdb}<p class="ok small">✓ A TMDB key is set.</p>{/if}
			<label class="field"><span>TMDB API key</span><input bind:value={tmdbKey} autocomplete="off" spellcheck="false" /></label>
			<div class="row">
				<button class="primary" onclick={saveTmdb} disabled={busy || !tmdbKey.trim()}>Save</button>
				<button onclick={() => go(step + 1)}>{status?.tmdb ? 'Continue' : 'Skip'}</button>
			</div>
		{:else if step === 3}
			<h2>Indexer</h2>
			<p class="muted">
				Fetchbox searches indexers for releases. With <a href="https://prowlarr.com" target="_blank" rel="noreferrer">Prowlarr</a>
				it uses every indexer Prowlarr has, torrent and Usenet, including ones added later. Without one, it finds only public-domain films (Internet Archive).
			</p>
			{#if status?.indexers}<p class="ok small">✓ {status.indexers} indexer{status.indexers === 1 ? '' : 's'} set up.</p>{/if}
			<label class="field"><span>Prowlarr address</span><input bind:value={prowlarr.url} placeholder="http://192.168.1.10:9696" spellcheck="false" /></label>
			<label class="field"
				><span>Prowlarr API key <span class="muted small">(Prowlarr: Settings > General > Security)</span></span><input
					bind:value={prowlarr.api_key}
					autocomplete="off"
					spellcheck="false"
				/></label
			>
			<div class="row">
				<button class="primary" onclick={addIndexer} disabled={busy || !prowlarr.url.trim()}>{busy ? 'Testing…' : 'Add and test'}</button>
				<button onclick={() => go(step + 1)}>{status?.indexers ? 'Continue' : 'Skip'}</button>
			</div>
		{:else if step === 4}
			<h2>Downloads</h2>
			<p class="muted">Fetchbox has its own torrent engine: nothing to set up. Downloads go straight into the library folder and are filed in place.</p>
			<label class="check"><input type="checkbox" bind:checked={own} /> Add a client of my own: qBittorrent, Transmission or Deluge, or SABnzbd or NZBGet for Usenet</label>
			{#if own}
				{#if status?.download_clients}<p class="ok small">✓ {status.download_clients} client{status.download_clients === 1 ? '' : 's'} set up.</p>{/if}
				<label class="field"
					><span>Client</span>
					<select bind:value={client.kind}>
						<optgroup label="Torrents">
							<option value="qbittorrent">qBittorrent</option><option value="transmission">Transmission</option><option value="deluge">Deluge</option>
						</optgroup>
						<optgroup label="Usenet">
							<option value="sabnzbd">SABnzbd</option><option value="nzbget">NZBGet</option>
						</optgroup>
					</select></label
				>
				<label class="field"><span>Address</span><input bind:value={client.url} placeholder={`http://192.168.1.10:${client.kind === 'nzbget' ? 6789 : 8080}`} spellcheck="false" /></label>
				{#if client.kind === 'sabnzbd'}
					<label class="field"><span>API key</span><input type="password" bind:value={client.api_key} autocomplete="off" placeholder="Config → General → Security" /></label>
				{:else}
					{#if client.kind !== 'deluge'}<label class="field"><span>Username</span><input bind:value={client.username} autocomplete="off" /></label>{/if}
					<label class="field"><span>Password</span><input type="password" bind:value={client.password} autocomplete="off" /></label>
				{/if}
				{#if client.kind === 'sabnzbd' || client.kind === 'nzbget'}
					<p class="muted small">Usenet releases go to it; torrents still use the built-in engine. Your news server account is set up in {CLIENT_NAMES[client.kind]} itself.</p>
				{/if}
				<div class="row">
					<button class="primary" onclick={addClient} disabled={busy || !client.url.trim()}>{busy ? 'Testing…' : 'Add and test'}</button>
					<button onclick={() => go(step + 1)}>Continue</button>
				</div>
			{:else}
				<div class="row"><button class="primary" onclick={() => ((saved = new Set([...saved, 4])), go(step + 1))}>Use the built-in engine</button></div>
			{/if}
		{:else if step === 5}
			<h2>Media server</h2>
			<p class="muted">Plex, Jellyfin or Emby: Fetchbox asks it to rescan just the folder it changed, after each download.</p>
			<MediaServersCard {destinations} />
			<div class="row">
				<button class="primary" onclick={() => (load(), go(step + 1))}>Continue</button>
				<button onclick={skip}>Skip</button>
			</div>
		{:else}
			<h2>Done</h2>
			<ul class="summary">
				<li>{status?.tmdb ? '✓' : '·'} Movies and TV information {status?.tmdb ? '' : '(skipped: needed to add movies and shows)'}</li>
				<li>{status?.indexers ? '✓' : '·'} Indexer {status?.indexers ? '' : '(skipped: only public-domain films can be found)'}</li>
				<li>✓ Downloads: {status?.download_clients ? 'your client' : 'the built-in engine'}</li>
				<li>{status?.media_servers ? '✓' : '·'} Media server {status?.media_servers ? '' : '(skipped)'}</li>
			</ul>
			<p class="muted small">All of it is in Settings, to change or finish later; Settings > General can run this setup again.</p>
			<div class="row">
				<button class="primary" onclick={async () => (await finish(), goto('/add'))}>Add your first title</button>
				<button onclick={leave}>Go to the library</button>
			</div>
		{/if}
	</div>
</main>

<style>
	.setup {
		max-width: 46rem;
	}
	.setup .card {
		gap: 0.85rem;
	}
	.setup .card h2,
	.setup .card p {
		margin: 0;
	}
	.steps {
		list-style: none;
		padding: 0;
		margin: 0.5rem 0 1rem;
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem 1rem;
	}
	.steps li {
		color: var(--muted);
	}
	.steps li.current {
		color: var(--text);
		font-weight: 600;
	}
	.steps .mark {
		display: inline-grid;
		place-items: center;
		width: 1.4rem;
		height: 1.4rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		font-size: 0.8rem;
		margin-right: 0.2rem;
	}
	.steps li.current .mark {
		border-color: var(--accent);
		color: var(--accent);
	}
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.summary {
		list-style: none;
		padding: 0;
		display: grid;
		gap: 0.35rem;
	}
	button.link {
		background: none;
		border: 0;
		padding: 0;
		color: inherit;
		font: inherit;
		cursor: pointer;
	}
</style>
