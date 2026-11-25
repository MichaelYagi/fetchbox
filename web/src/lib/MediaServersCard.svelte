<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import { ApiError, del, get, patch, post, type S } from './api.ts';

	// Plex / Jellyfin / Emby: told to rescan the folders Fetchbox changes.
	type Server = S['MediaServerOut'];
	type Test = S['MediaServerTestOut'];
	let { destinations }: { destinations: S['DestinationOut'][] } = $props();

	let servers = $state<Server[]>([]);
	let tests = $state<Record<string, Test>>({});
	let editing = $state<string | null>(null); // a server id, or 'new'
	let draft = $state({ kind: 'plex', name: 'Plex', url: '', secret: '', mappings: [] as { local: string; remote: string }[] });
	let paths = $state<Record<string, string>>({}); // a folder's path as typed in the Test results
	let busy = $state('');
	let error = $state('');

	const KINDS: Record<string, { label: string; port: string; secret: string; where: string }> = {
		plex: {
			label: 'Plex',
			port: '32400',
			secret: 'token',
			where: "Plex token: in Plex Web open any movie › ⋯ › Get Info › View XML; it's the X-Plex-Token at the end of the address."
		},
		jellyfin: {
			label: 'Jellyfin',
			port: '8096',
			secret: 'api_key',
			where: 'API key: Dashboard › API Keys › +, name it Fetchbox.'
		},
		emby: {
			label: 'Emby',
			port: '8096',
			secret: 'api_key',
			where: 'API key: Settings › Advanced › API Keys › New API Key, name it Fetchbox.'
		}
	};

	async function load() {
		try {
			servers = await get<Server[]>('/media-servers');
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
	});

	async function act(name: string, fn: () => Promise<unknown>) {
		busy = name;
		error = '';
		try {
			await fn();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const test = (s: Server) =>
		act(`test-${s.id}`, async () => {
			tests[s.id] = await post<Test>(`/media-servers/${s.id}/test`);
		});

	function startAdd() {
		draft = { kind: 'plex', name: 'Plex', url: '', secret: '', mappings: [] };
		editing = 'new';
	}
	function startEdit(s: Server) {
		draft = {
			kind: s.kind,
			name: s.name,
			url: String(s.settings.url ?? ''),
			secret: String(s.settings[KINDS[s.kind].secret] ?? ''),
			mappings: s.path_mappings.map((m) => ({ ...m }))
		};
		editing = s.id;
	}
	// only for a folder the server's own folders don't reveal: the user says where it is
	const setPath = (s: Server, local: string, remote: string) =>
		act(`path-${s.id}`, async () => {
			const path_mappings = [...s.path_mappings.filter((m) => m.local !== local), ...(remote.trim() ? [{ local, remote: remote.trim() }] : [])];
			await patch(`/media-servers/${s.id}`, { path_mappings }, s.etag);
			await load();
			tests[s.id] = await post<Test>(`/media-servers/${s.id}/test`);
		});
	const byHand = (s: Server, local: string) => s.path_mappings.some((m) => m.local === local);

	const save = (e: SubmitEvent) => {
		e.preventDefault();
		act('save', async () => {
			const body = {
				name: draft.name.trim(),
				settings: { url: draft.url.trim(), [KINDS[draft.kind].secret]: draft.secret },
				path_mappings: draft.mappings // set from the Test results; kept as they are
			};
			const row =
				editing === 'new'
					? await post<Server>('/media-servers', { kind: draft.kind, ...body })
					: await patch<Server>(`/media-servers/${editing}`, body, servers.find((s) => s.id === editing)?.etag);
			editing = null;
			await load();
			tests[row.id] = await post<Test>(`/media-servers/${row.id}/test`);
		});
	};
	const remove = async (s: Server) => {
		if (!(await ask(`Stop telling ${s.name} about new files?`, 'Remove', true))) return;
		act(`rm-${s.id}`, async () => {
			await del(`/media-servers/${s.id}`, undefined, s.etag);
			await load();
		});
	};
	const toggle = (s: Server) =>
		act(`en-${s.id}`, async () => {
			await patch(`/media-servers/${s.id}`, { enabled: !s.enabled }, s.etag);
			await load();
		});

	const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : 'not yet');
</script>

<div class="stack">
	<p class="muted small">
		After every import, upgrade or rename, Fetchbox asks your media server to rescan just that folder, so new things show
		up in a minute instead of at the next scheduled scan.
	</p>
	{#if error}<p class="notice error">{error}</p>{/if}

	{#each servers as s (s.id)}
		<div class="card stack server">
			<div class="spread">
				<div>
					<strong>{s.name}</strong> <span class="badge">{KINDS[s.kind]?.label ?? s.kind}</span>
					{#if !s.enabled}<span class="badge">Off</span>{/if}
					<div class="muted small">{s.settings.url} · last rescan {when(s.last_refresh_at)}</div>
				</div>
				<div class="row">
					<button class="small" onclick={() => test(s)} disabled={!!busy}>{busy === `test-${s.id}` ? 'Testing…' : 'Test'}</button>
					<button class="small" onclick={() => (editing === s.id ? (editing = null) : startEdit(s))} disabled={!!busy}>
						{editing === s.id ? 'Cancel' : 'Edit'}
					</button>
					<button class="small" onclick={() => toggle(s)} disabled={!!busy}>{s.enabled ? 'Turn off' : 'Turn on'}</button>
					<button class="small danger" onclick={() => remove(s)} disabled={!!busy}>Remove</button>
				</div>
			</div>
			{#if s.last_error}<p class="notice warn small">Last rescan: {s.last_error}</p>{/if}
			{#if tests[s.id]}
				{@const t = tests[s.id]}
				<p class="notice {t.ok ? '' : 'error'} small">{t.message}</p>
				{#if t.ok}
					<table class="coverage small">
						<tbody>
							{#each t.coverage as c (c.folder)}
								<tr>
									<td><strong>{c.folder}</strong></td>
									<td class="mono">{c.remote}</td>
									<td>
										{#if c.library}
											{#if c.visible === false}
												<span class="badge warn">in “{c.library}”, but {KINDS[s.kind]?.label} can't see this folder</span>
											{:else}
												<span class="badge ok">✓ in “{c.library}”</span>
											{/if}
											{#if c.how === 'files'}<span class="muted small">matched by files {KINDS[s.kind]?.label} already has</span>
											{:else if c.how === 'names'}<span class="muted small">matched by folder names</span>{/if}
											{#if byHand(s, c.local)}
												<button class="small" onclick={() => setPath(s, c.local, '')} disabled={!!busy}>Forget the path I typed</button>
											{/if}
										{:else}
											<span class="badge warn">not found in {KINDS[s.kind]?.label}</span>
											<form class="row pathfix" onsubmit={(e) => (e.preventDefault(), setPath(s, c.local, paths[c.local] ?? ''))}>
												<input class="small" bind:value={paths[c.local]} placeholder="this folder's path in {KINDS[s.kind]?.label}" aria-label="{c.folder}: its path in {KINDS[s.kind]?.label}" />
												<button class="small" disabled={!!busy || !paths[c.local]?.trim()}>Save</button>
											</form>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
					{#if t.coverage.some((c) => !c.library)}
						<p class="muted small">
							A folder you don't keep in {KINDS[s.kind]?.label} can be ignored. Otherwise add it to a library in
							{KINDS[s.kind]?.label}, or type where {KINDS[s.kind]?.label} sees it (the library's settings in
							{KINDS[s.kind]?.label} show its folders).
						</p>
					{/if}
				{/if}
			{/if}
			{#if editing === s.id}{@render form()}{/if}
		</div>
	{/each}

	{#if editing === 'new'}
		<div class="card stack">{@render form()}</div>
	{:else}
		<div><button onclick={startAdd} disabled={!!busy}>Add a media server</button></div>
	{/if}
</div>

{#snippet form()}
	<form class="stack" onsubmit={save}>
		{#if editing === 'new'}
			<div class="row seg" role="radiogroup" aria-label="Kind">
				{#each Object.entries(KINDS) as [k, v] (k)}
					<button type="button" role="radio" aria-checked={draft.kind === k} class:on={draft.kind === k}
						onclick={() => ((draft.kind = k), (draft.name = v.label))}>{v.label}</button>
				{/each}
			</div>
		{/if}
		<div class="row">
			<label class="grow">Name <input bind:value={draft.name} required /></label>
			<label class="grow2">Address <input bind:value={draft.url} required placeholder="http://192.168.1.10:{KINDS[draft.kind].port}" /></label>
		</div>
		<label>
			{KINDS[draft.kind].secret === 'token' ? 'Plex token' : 'API key'}
			<input bind:value={draft.secret} required autocomplete="off" spellcheck="false" />
		</label>
		<p class="muted small hint">{KINDS[draft.kind].where}</p>
		<div class="row">
			<button class="primary" disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save and test'}</button>
			{#if editing === 'new'}<button type="button" onclick={() => (editing = null)}>Cancel</button>{/if}
		</div>
	</form>
{/snippet}

<style>
	.server {
		gap: 0.5rem;
	}
	.grow {
		flex: 1;
		min-width: 12rem;
	}
	.grow2 {
		flex: 2;
		min-width: 14rem;
	}
	.seg {
		gap: 0.35rem;
	}
	.seg button.on {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.hint {
		margin: 0;
	}
	.pathfix {
		margin-top: 0.35rem;
		gap: 0.35rem;
	}
	.pathfix input {
		min-width: 0;
		flex: 1 1 12rem;
	}
	.coverage {
		width: 100%;
	}
	@media (max-width: 640px) {
		/* one folder per block: name, then its path, then the verdict */
		.coverage tr {
			display: block;
			padding: 0.4rem 0;
			border-bottom: 1px solid var(--border);
		}
		.coverage td {
			display: block;
			padding: 0.1rem 0;
		}
	}
	.coverage td {
		padding: 0.25rem 0.5rem 0.25rem 0;
		vertical-align: top;
	}
	.mono {
		font-family: var(--mono, ui-monospace, monospace);
		overflow-wrap: anywhere;
	}
	.server :global(.row) {
		flex-wrap: wrap;
	}
</style>
