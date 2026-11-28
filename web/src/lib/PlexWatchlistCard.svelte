<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import { ago, ApiError, del, get, patch, post, type S } from './api.ts';

	// Plex watchlists: sign in with Plex (its own page: no password here), and what anyone
	// signed in bookmarks with "+ Watchlist" in a Plex app is added and downloaded.
	type Account = S['AccountOut'];
	type Item = S['ItemOut'];
	type Watchlist = {
		movie_profile_id: string | null;
		series_profile_id: string | null;
		series_mode: 'all' | 'future' | 'missing' | 'first_season' | 'latest_season' | 'none';
		ask_first: boolean;
	};

	let accounts = $state<Account[]>([]);
	let items = $state<Item[]>([]);
	let profiles = $state<S['ProfileOut'][]>([]);
	let cfg = $state<Watchlist | null>(null);
	let busy = $state('');
	let error = $state('');
	let message = $state('');
	let signingIn = $state(false);
	// Plex servers under Media servers: the token each was added with reads its watchlist too
	let servers = $state<{ id: string; name: string; kind: string }[]>([]);

	const MODES: [string, string][] = [
		['all', 'Every episode'],
		['future', 'New episodes only'],
		['first_season', 'The first season'],
		['latest_season', 'The latest season']
	];
	const STATE: Record<string, [string, string]> = {
		added: ['ok', 'Added'],
		waiting: ['warn', 'Waiting'],
		in_library: ['', 'Already here'],
		dismissed: ['', 'Dismissed'],
		skipped: ['warn', 'Skipped']
	};

	async function load() {
		try {
			let all: { id: string; name: string; kind: string }[];
			[accounts, items, all] = await Promise.all([
				get<Account[]>('/watchlist/accounts'),
				get<Item[]>('/watchlist/items'),
				get<{ id: string; name: string; kind: string }[]>('/media-servers').catch(() => [])
			]);
			servers = all.filter((x) => x.kind === 'plex');
			if (accounts.length && !cfg) {
				const [c, p] = await Promise.all([
					get<S['ConfigOut']>('/system/config'),
					get<S['ProfileOut'][]>('/quality-profiles')
				]);
				cfg = c.config.watchlist as Watchlist;
				profiles = p.filter((x) => x.media === 'video');
			}
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
		const t = setInterval(load, 30_000);
		return () => clearInterval(t);
	});

	async function act(name: string, fn: () => Promise<unknown>, done = '') {
		busy = name;
		error = message = '';
		try {
			await fn();
			message = done;
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	// Plex's sign-in page opens in a new tab (opened on the click, so it isn't blocked);
	// Fetchbox waits for the approval there, up to the 30 minutes Plex allows.
	async function signIn() {
		error = message = '';
		const tab = window.open('about:blank', '_blank');
		signingIn = true;
		try {
			const start = await post<S['SignInOut']>('/watchlist/sign-in');
			if (tab) tab.location.href = start.url;
			else window.location.href = start.url;
			const until = Date.now() + (start.expires_in ?? 1800) * 1000;
			while (signingIn && Date.now() < until) {
				await new Promise((r) => setTimeout(r, 2000));
				const s = await get<S['SignInStatus']>(`/watchlist/sign-in/${start.pin_id}`);
				if (s.done) {
					message = `Following ${s.account?.username}'s watchlist. New titles arrive within 10 minutes; “Check now” reads it straight away.`;
					await load();
					return;
				}
			}
		} catch (e) {
			tab?.close();
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			signingIn = false;
		}
	}

	const save = (change: Partial<Watchlist>) =>
		act('save', async () => {
			cfg = { ...cfg!, ...change };
			await patch('/system/config', { watchlist: change });
		});

	const waiting = $derived(items.filter((i) => i.state === 'waiting'));
	const recent = $derived(items.filter((i) => i.state !== 'waiting').slice(0, 8));
</script>

<div class="card stack">
	<p class="muted small">
		Anything bookmarked with <strong>+ Watchlist</strong> in a Plex app (TV, phone, plex.tv) is added here and downloaded, then
		shows up in Plex. Sign in once for each person whose watchlist to follow: it's Plex's own page, so no password is
		typed here (or, with Plex under Media servers, use the token it was added with). A title removed from the
		library isn't added back.
	</p>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}

	{#each accounts as a (a.id)}
		<div class="spread account">
			<div>
				<strong>{a.username}</strong>
				{#if !a.enabled}<span class="badge">Paused</span>{/if}
				<div class="muted small">
					{#if a.last_error}<span class="warn-text">{a.last_error}</span>{:else if a.last_sync_at}Read {ago(a.last_sync_at)}{:else}Not read yet{/if}
				</div>
			</div>
			<div class="row">
				<button class="small" disabled={!!busy} onclick={() => act(a.id, () => patch(`/watchlist/accounts/${a.id}`, { enabled: !a.enabled }))}>{a.enabled ? 'Pause' : 'Resume'}</button>
				<button
					class="small danger"
					disabled={!!busy}
					onclick={async () =>
						(await ask(`Stop following ${a.username}'s watchlist? What it added stays in the library.`, 'Stop following', true)) &&
						act(a.id, () => del(`/watchlist/accounts/${a.id}`))}>Remove</button
				>
			</div>
		</div>
	{/each}

	<div class="row">
		{#if !accounts.length}
			{#each servers as srv (srv.id)}
				<button
					class="primary"
					disabled={!!busy || signingIn}
					onclick={() =>
						act('server', () => post('/watchlist/accounts/from-server', { server_id: srv.id }), `Following the watchlist of the Plex account ${srv.name} was added with.`)}
					>Use {srv.name}'s token</button
				>
			{/each}
		{/if}
		<button class={accounts.length || servers.length ? '' : 'primary'} onclick={signIn} disabled={signingIn}>
			{signingIn ? 'Waiting for Plex… (approve Fetchbox in the tab that opened)' : accounts.length ? 'Sign in another Plex account' : 'Sign in with Plex'}
		</button>
		{#if signingIn}<button class="link" onclick={() => (signingIn = false)}>Cancel</button>{/if}
		{#if accounts.length}
			<button onclick={() => act('sync', () => post('/watchlist/sync'), 'Reading the watchlists now.')} disabled={!!busy}>Check now</button>
		{/if}
	</div>

	{#if accounts.length && cfg}
		<div class="options">
			<label class="field"
				><span>Movies' quality</span>
				<select value={cfg.movie_profile_id ?? ''} onchange={(e) => save({ movie_profile_id: e.currentTarget.value || null })}>
					<option value="">{profiles[0]?.name ?? 'The first profile'} (the first)</option>
					{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
				</select></label
			>
			<label class="field"
				><span>Shows' quality</span>
				<select value={cfg.series_profile_id ?? ''} onchange={(e) => save({ series_profile_id: e.currentTarget.value || null })}>
					<option value="">{profiles[0]?.name ?? 'The first profile'} (the first)</option>
					{#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
				</select></label
			>
			<label class="field"
				><span>Of a show, download</span>
				<select value={cfg.series_mode} onchange={(e) => save({ series_mode: e.currentTarget.value as Watchlist['series_mode'] })}>
					{#each MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
				</select></label
			>
		</div>
		<label class="check">
			<input type="checkbox" checked={cfg.ask_first} onchange={(e) => save({ ask_first: e.currentTarget.checked })} />
			Ask me first: watchlisted titles wait here for approval instead of downloading
		</label>
	{/if}

	{#if waiting.length}
		<div class="block">
			<strong>Waiting for you ({waiting.length})</strong>
			{#each waiting as i (i.id)}
				<div class="spread line">
					<span>{i.name} <span class="muted">{i.year ?? ''} · {i.kind === 'series' ? 'show' : 'movie'} · {i.account}'s watchlist</span></span>
					<span class="row">
						<button class="small primary" disabled={!!busy} onclick={() => act(i.id, () => post(`/watchlist/items/${i.id}/approve`), `${i.name}: added, and being searched for.`)}>Approve</button>
						<button class="small" disabled={!!busy} onclick={() => act(i.id, () => post(`/watchlist/items/${i.id}/dismiss`))}>Dismiss</button>
					</span>
				</div>
			{/each}
		</div>
	{/if}

	{#if recent.length}
		<div class="block">
			<strong>Recently from the watchlists</strong>
			{#each recent as i (i.id)}
				<div class="line small">
					<span class="badge {STATE[i.state]?.[0] ?? ''}">{STATE[i.state]?.[1] ?? i.state}</span>
					{#if i.title_id}<a href="/title/{i.title_id}">{i.name}</a>{:else}{i.name}{/if}
					<span class="muted">{i.year ?? ''} · {i.account} · {ago(i.updated_at)}</span>
					{#if i.state === 'skipped' && i.reason}<span class="muted">· {i.reason}</span>{/if}
					{#if i.state === 'skipped' && i.tmdb_id}
						<button class="link small" onclick={() => act(i.id, () => post(`/watchlist/items/${i.id}/approve`), `${i.name}: added.`)}>Try again</button>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.account {
		border-bottom: 1px solid var(--border);
		padding-bottom: 0.5rem;
	}
	.options {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
		gap: 0.75rem;
	}
	.field {
		display: grid;
		gap: 0.25rem;
	}
	.block {
		display: grid;
		gap: 0.35rem;
	}
	.line {
		gap: 0.5rem;
	}
	.warn-text {
		color: var(--warn, #d9a441);
	}
</style>
