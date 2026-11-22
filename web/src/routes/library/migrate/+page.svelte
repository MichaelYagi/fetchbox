<script lang="ts">
	import { ApiError, get, post, type S } from '#lib/api.ts';

	// Move from Radarr / Sonarr: connect, look at what's there, import. Neither is changed.
	type Scan = {
		kind: 'radarr' | 'sonarr';
		app_version: string;
		titles: { arr_id: number; name: string; year: number | null; files: number; monitored: boolean; in_fetchbox: boolean; tmdb_id: number | null }[];
		root_folders: { path: string; titles: number }[];
		profiles: { id: number; name: string; maps_to: string | null }[];
		indexers: { name: string; implementation: string; url: string | null }[];
		download_clients: { name: string; implementation: string; host: string | null }[];
		counts: { titles: number; with_files: number; missing_tmdb: number };
	};
	let kind = $state<'radarr' | 'sonarr'>('radarr');
	let url = $state('');
	let key = $state('');
	let scan = $state<Scan | null>(null);
	let seenAs = $state<Record<string, string>>({});
	let profileMap = $state<Record<string, string>>({});
	let profiles = $state<S['ProfileOut'][]>([]);
	let result = $state<{ added: number; files: number; skipped: { name: string; reason: string }[]; library_folders: string[] } | null>(null);
	let progress = $state<number | null>(null);
	let busy = $state('');
	let error = $state('');

	const label = $derived(kind === 'radarr' ? 'Radarr' : 'Sonarr');
	const port = $derived(kind === 'radarr' ? '7878' : '8989');
	$effect(() => {
		get<S['ProfileOut'][]>('/quality-profiles', { media: 'video' }).then((p) => (profiles = p));
	});

	async function waitFor(id: string): Promise<Record<string, unknown>> {
		for (;;) {
			const j = await get<S['JobOut']>(`/jobs/${id}`);
			progress = j.progress;
			if (j.status === 'succeeded') return j.result ?? {};
			if (j.status === 'failed') throw new Error(j.error ?? 'failed');
			await new Promise((r) => setTimeout(r, 700));
		}
	}

	async function act(name: string, fn: () => Promise<unknown>) {
		busy = name;
		error = '';
		try {
			await fn();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
			progress = null;
		}
	}

	const connect = (e: SubmitEvent) => {
		e.preventDefault();
		act('scan', async () => {
			result = null;
			const j = await post<S['JobOut']>('/library/migrate/scan', { kind, url: url.trim(), api_key: key.trim() });
			scan = (await waitFor(j.id)) as unknown as Scan;
			seenAs = Object.fromEntries(scan.root_folders.map((r) => [r.path, r.path]));
			profileMap = Object.fromEntries(scan.profiles.map((p) => [String(p.id), p.maps_to ?? '']));
		});
	};
	const importAll = () =>
		act('apply', async () => {
			const path_map = Object.entries(seenAs)
				.filter(([from, to]) => to.trim() && to.trim() !== from)
				.map(([from, to]) => ({ from, to: to.trim() }));
			const j = await post<S['JobOut']>('/library/migrate/apply', {
				kind,
				url: url.trim(),
				api_key: key.trim(),
				path_map,
				profile_map: Object.fromEntries(Object.entries(profileMap).filter(([, v]) => v))
			});
			result = (await waitFor(j.id)) as unknown as typeof result;
		});
	const fresh = $derived(scan ? scan.titles.filter((t) => !t.in_fetchbox && t.tmdb_id).length : 0);
</script>

<main>
	<p><a href="/settings#library">← Library settings</a></p>
	<h1>Move from Radarr or Sonarr</h1>
	<p class="muted small">
		Fetchbox reads your movies or shows, what's monitored, your quality profiles and which files you have, and adds them
		here with the files where they are. Radarr and Sonarr aren't changed, so you can run both side by side; turn their
		monitoring off once you've switched, or both will download.
	</p>
	{#if error}<p class="notice error">{error}</p>{/if}

	<form class="card stack" onsubmit={connect}>
		<div class="row seg" role="radiogroup" aria-label="App">
			{#each [['radarr', 'Radarr (movies)'], ['sonarr', 'Sonarr (TV)']] as [k, l] (k)}
				<button type="button" role="radio" aria-checked={kind === k} class:on={kind === k}
					onclick={() => ((kind = k as typeof kind), (scan = null), (result = null))}>{l}</button>
			{/each}
		</div>
		<div class="row">
			<label class="grow">Address <input bind:value={url} required placeholder="http://192.168.1.10:{port}" /></label>
			<label class="grow">API key <input type="password" bind:value={key} required autocomplete="off" spellcheck="false" placeholder="{label}: Settings › General › API Key" /></label>
			<button class="primary" disabled={!!busy}>{busy === 'scan' ? `Reading… ${progress != null ? Math.round(progress * 100) + '%' : ''}` : 'Connect'}</button>
		</div>
	</form>

	{#if scan && !result}
		<div class="card stack">
			<h2>{label} {scan.app_version}</h2>
			<p>
				<strong>{scan.counts.titles}</strong> {kind === 'radarr' ? 'movies' : 'shows'}, {scan.counts.with_files} with files.
				{fresh} new to Fetchbox{scan.counts.missing_tmdb ? `; ${scan.counts.missing_tmdb} without a TMDB id are skipped` : ''}.
			</p>

			<h3>Folders</h3>
			<p class="muted small">Each becomes a Fetchbox library folder. If {label} runs on another machine or in Docker, type where this computer sees the same folder.</p>
			{#each scan.root_folders as r (r.path)}
				<div class="row">
					<span class="mono grow">{r.path} <span class="muted small">({r.titles})</span></span>
					<label class="grow small">Fetchbox sees it at <input bind:value={seenAs[r.path]} /></label>
				</div>
			{/each}

			<h3>Quality profiles</h3>
			{#each scan.profiles as p (p.id)}
				<div class="row">
					<span class="grow">{p.name}</span>
					<select bind:value={profileMap[String(p.id)]} aria-label="Fetchbox profile for {p.name}">
						{#each profiles as ours (ours.id)}<option value={ours.id}>{ours.name}</option>{/each}
					</select>
				</div>
			{/each}

			{#if scan.indexers.length || scan.download_clients.length}
				<h3>Add these in Fetchbox yourself</h3>
				<p class="muted small">{label} doesn't share passwords or API keys, so indexers and download clients aren't copied. One Prowlarr indexer in Fetchbox replaces them all.</p>
				{#each scan.indexers as i (i.name)}<div class="small">Indexer <strong>{i.name}</strong> ({i.implementation}) <span class="mono">{i.url ?? ''}</span></div>{/each}
				{#each scan.download_clients as d (d.name)}<div class="small">Download client <strong>{d.name}</strong> ({d.implementation}) <span class="mono">{d.host ?? ''}</span></div>{/each}
				<p class="small"><a href="/settings">Open Settings</a></p>
			{/if}

			<div>
				<button class="primary" onclick={importAll} disabled={!!busy || fresh === 0 && scan.counts.with_files === 0}>
					{busy === 'apply' ? `Importing… ${progress != null ? Math.round(progress * 100) + '%' : ''}` : `Import ${scan.counts.titles} ${kind === 'radarr' ? 'movies' : 'shows'}`}
				</button>
			</div>
		</div>
	{/if}

	{#if result}
		<div class="card stack">
			<h2>Done</h2>
			<p>{result.added} added, {result.files} file{result.files === 1 ? '' : 's'} recorded in place. Library folders: {result.library_folders.join(', ')}.</p>
			{#if result.skipped.length}<p class="notice warn small">Skipped: {result.skipped.map((s) => `${s.name} (${s.reason})`).join('; ')}</p>{/if}
			<p class="muted small">Fetchbox starts searching for what's missing within the hour.</p>
			<div class="row"><a class="button primary" href="/">Go to the library</a>
				<button onclick={() => ((scan = null), (result = null), (kind = kind === 'radarr' ? 'sonarr' : 'radarr'))}>Now {kind === 'radarr' ? 'Sonarr' : 'Radarr'}</button></div>
		</div>
	{/if}
</main>

<style>
	.grow {
		flex: 1;
		min-width: 12rem;
	}
	.seg {
		gap: 0.35rem;
	}
	.seg button.on {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.mono {
		font-family: var(--mono, ui-monospace, monospace);
		overflow-wrap: anywhere;
	}
	h3 {
		margin: 0.5rem 0 0;
	}
</style>
