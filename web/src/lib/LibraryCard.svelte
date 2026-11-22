<script lang="ts">
	import Path from './Path.svelte';
	import { ApiError, get, patch, post, put, type S } from './api.ts';

	// Settings for one kind of library (movies or TV): where files go, how they're named
	// (a preset, or custom patterns), a live preview, and renaming files already there.
	let {
		kind,
		dest,
		options,
		onChanged
	}: {
		kind: 'movie' | 'series';
		dest: S['DestinationOut'] | undefined;
		options: S['NamingOptionsOut'];
		onChanged: () => void;
	} = $props();

	const tv = $derived(kind === 'series');
	const FOLDER = $derived(tv ? 'series_folder' : 'movie_folder');
	const FILE = $derived(tv ? 'episode_file' : 'movie_file');
	const TV_ONLY = ['Season', 'Episode', 'EpisodeEnd', 'EpisodeTitle', 'AirDate', 'TvdbId'];
	const tokens = $derived(
		Object.entries(options.tokens).filter(([t]) => (tv ? t !== 'TmdbId' && t !== 'Edition' : !TV_ONLY.includes(t)))
	);

	let saved = $state<S['NamingOut'] | null>(null);
	let draft = $state<Record<string, string>>({});
	let custom = $state(false);
	let preview = $state<S['SampleOut'][]>([]);
	let previewError = $state('');
	let renames = $state<S['RenameStepOut'][] | null>(null);
	let showRenames = $state(false);
	let editingFolder = $state(false);
	let folder = $state('');
	let busy = $state('');
	let error = $state('');
	let message = $state('');
	let focused: HTMLInputElement | null = null;

	const dirty = $derived(!!saved && Object.entries(draft).some(([k, v]) => saved!.patterns[k] !== v));
	const presetId = $derived.by(() => {
		if (custom) return 'custom';
		const p = options.presets.find((x) => x.patterns[FOLDER] === draft[FOLDER] && x.patterns[FILE] === draft[FILE]);
		return p?.id ?? 'custom';
	});

	async function load() {
		try {
			saved = await get<S['NamingOut']>(`/library/naming/${kind}`);
			draft = { ...saved.patterns };
			custom = saved.preset === 'custom';
			preview = saved.samples;
			renames = await get<S['RenameStepOut'][]>(`/library/rename/${kind}`);
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		void kind;
		load();
	});

	// live preview of the draft, debounced
	let timer: ReturnType<typeof setTimeout> | undefined;
	$effect(() => {
		const body = { patterns: { ...draft } };
		if (!saved) return;
		clearTimeout(timer);
		timer = setTimeout(async () => {
			try {
				const r = await post<S['NamingOut']>(`/library/naming/${kind}/preview`, body);
				preview = r.samples;
				previewError = '';
			} catch (e) {
				previewError = e instanceof ApiError ? e.message : String(e);
			}
		}, 250);
		return () => clearTimeout(timer);
	});

	function pickPreset(id: string) {
		if (id === 'custom') {
			custom = true;
			return;
		}
		custom = false;
		const p = options.presets.find((x) => x.id === id)!;
		draft = { ...draft, [FOLDER]: p.patterns[FOLDER], [FILE]: p.patterns[FILE] };
	}

	function insert(token: string) {
		const el = focused;
		if (!el) return;
		const key = el.dataset.key!;
		const v = draft[key] ?? '';
		const at = el.selectionStart ?? v.length;
		const end = el.selectionEnd ?? at;
		const text = `{${token}}`;
		draft = { ...draft, [key]: v.slice(0, at) + text + v.slice(end) };
		requestAnimationFrame(() => {
			el.focus();
			el.setSelectionRange(at + text.length, at + text.length);
		});
	}

	async function act(name: string, fn: () => Promise<unknown>, done = '') {
		busy = name;
		error = '';
		message = '';
		try {
			await fn();
			if (done) message = done; // an action may set its own message
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const save = () =>
		act('save', async () => {
			saved = await put<S['NamingOut']>(`/library/naming/${kind}`, { patterns: draft });
			draft = { ...saved.patterns };
			custom = saved.preset === 'custom';
			renames = await get<S['RenameStepOut'][]>(`/library/rename/${kind}`);
		}, 'Saved. New files will be named this way.');

	const rename = () =>
		act('rename', async () => {
			const r = await post<S['RenameOut']>(`/library/rename/${kind}`, {});
			renames = await get<S['RenameStepOut'][]>(`/library/rename/${kind}`);
			if (r.failed.length) {
				error = `${r.failed.length} file(s) could not be renamed: ` + r.failed.map((f) => `${f.current} (${f.error})`).join('; ');
			}
			message = `Renamed ${r.renamed.length} file${r.renamed.length === 1 ? '' : 's'}.`;
			showRenames = renames.length > 0;
		});

	const LINK_MODES: [string, string][] = [
		['hardlink_or_copy', 'Hardlink when possible, else copy (recommended: keeps seeding, no double space)'],
		['hardlink_only', 'Hardlink only (fail rather than use double space)'],
		['copy', 'Always copy'],
		['move', 'Move (after seeding is done; for no seeding or little space)'],
		['symlink', 'Symlink (advanced; some media servers dislike it)']
	];
	const setLinkMode = (mode: string) =>
		act('link', async () => {
			await patch(`/destinations/${dest!.id}`, { link_mode: mode }, dest!.etag);
			onChanged();
		}, 'Saved.');

	const saveFolder = (e: SubmitEvent) => {
		e.preventDefault();
		act('folder', async () => {
			await patch(`/destinations/${dest!.id}`, { path: folder.trim() }, dest!.etag);
			editingFolder = false;
			onChanged();
			await load();
		}, 'Folder saved. New files go there; files already in the old folder stay.');
	};

	// the preview as a folder tree, under the library folder
	type Node = { name: string; children: Node[]; note?: string };
	const tree = $derived.by(() => {
		const root: Node = { name: '', children: [] };
		for (const s of preview) {
			let at = root;
			const parts = s.path.split('/');
			parts.forEach((p, i) => {
				let n = at.children.find((c) => c.name === p);
				if (!n) {
					n = { name: p, children: [] };
					at.children.push(n);
				}
				if (i === parts.length - 1) n.note = s.label;
				at = n;
			});
		}
		return root.children;
	});
	const seasonChoice = $derived(options.season_folders.find((x) => x.pattern === draft.season_folder));
</script>

<div class="card stack lib">
	<div class="spread">
		<h3>{tv ? 'TV shows' : 'Movies'}</h3>
		{#if dest}<span class="badge {dest.status === 'online' ? 'ok' : 'warn'}" title={dest.status_message ?? ''}>{dest.status}</span>{/if}
	</div>

	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}

	<div class="field">
		<div class="label">Folder</div>
		{#if dest && !editingFolder}
			<div class="row">
				<Path path={dest.path} />
				<button class="small" onclick={() => ((folder = dest.path), (editingFolder = true))}>Change</button>
				<a class="button small" href="/library/import?dest={dest.id}">Import what's already here</a>
			</div>
			{#if dest.status_message && dest.status_message !== 'ok'}<div class="muted small">{dest.status_message}</div>{/if}
		{:else if dest}
			<form class="row" onsubmit={saveFolder}>
				<input class="grow" bind:value={folder} required aria-label="{tv ? 'TV' : 'Movies'} folder" />
				<button class="primary" disabled={!!busy}>Save</button>
				<button type="button" onclick={() => (editingFolder = false)}>Cancel</button>
			</form>
			<div class="muted small">Checked when you save: it must exist or be creatable, be writable, and have free space.</div>
		{/if}
	</div>

	{#if dest}
		<div class="field">
			<label class="label" for="linkmode-{kind}">How finished downloads get here</label>
			<select id="linkmode-{kind}" value={dest.link_mode} onchange={(e) => setLinkMode(e.currentTarget.value)} disabled={!!busy}>
				{#each LINK_MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
			</select>
		</div>
	{/if}

	{#if saved}
		<div class="field">
			<div class="label">Naming style</div>
			<div class="presets" role="radiogroup">
				{#each options.presets as p (p.id)}
					<button class="preset" class:on={presetId === p.id} role="radio" aria-checked={presetId === p.id} onclick={() => pickPreset(p.id)}>
						<strong>{p.label}</strong>
						<span class="muted small">{p.description}</span>
					</button>
				{/each}
				<button class="preset" class:on={presetId === 'custom'} role="radio" aria-checked={presetId === 'custom'} onclick={() => pickPreset('custom')}>
					<strong>Custom</strong>
					<span class="muted small">Build your own from tokens</span>
				</button>
			</div>
		</div>

		{#if tv}
			<div class="field">
				<div class="label">Season folders</div>
				<div class="row seg" role="radiogroup">
					{#each options.season_folders as sf (sf.pattern)}
						<button class:on={draft.season_folder === sf.pattern} role="radio" aria-checked={draft.season_folder === sf.pattern}
							onclick={() => (draft = { ...draft, season_folder: sf.pattern })}>{sf.label}</button>
					{/each}
					{#if !seasonChoice}<span class="muted small">custom: <code>{draft.season_folder}</code></span>{/if}
				</div>
			</div>
		{/if}

		{#if presetId === 'custom'}
			<div class="field stack">
				<label>
					{tv ? 'Show folder' : 'Movie folder'}
					<input data-key={FOLDER} bind:value={draft[FOLDER]} onfocus={(e) => (focused = e.currentTarget)} spellcheck="false" />
				</label>
				{#if tv}
					<label>
						Season folder <span class="muted small">(empty: none)</span>
						<input data-key="season_folder" bind:value={draft.season_folder} onfocus={(e) => (focused = e.currentTarget)} spellcheck="false" />
					</label>
				{/if}
				<label>
					{tv ? 'Episode file' : 'Movie file'} <span class="muted small">(the extension is added)</span>
					<input data-key={FILE} bind:value={draft[FILE]} onfocus={(e) => (focused = e.currentTarget)} spellcheck="false" />
				</label>
				<div class="tokens">
					<span class="muted small">Click to insert at the cursor:</span>
					{#each tokens as [t, help] (t)}
						<button class="token small" title={help} onmousedown={(e) => e.preventDefault()} onclick={() => insert(t)}>{t}</button>
					{/each}
				</div>
				<p class="muted small hint">
					<code>[ … ]</code> is optional: it disappears when a token inside it is empty, e.g.
					<code>[ - {'{'}Quality{'}'}]</code>. <code>{'{'}Season:00{'}'}</code> pads to two digits. Characters
					Windows can't use are replaced automatically.
				</p>
			</div>
		{/if}

		<div class="field">
			<div class="label">Preview</div>
			{#if previewError}
				<p class="notice error">{previewError}</p>
			{:else}
				<div class="tree">
					<div class="dir root">{dest?.path ?? ''}</div>
					{#snippet branch(nodes: Node[], depth: number)}
						{#each nodes as n (n.name)}
							<div class="line" style="padding-left: {depth * 1.1}rem">
								<span class={n.children.length ? 'dir' : 'file'}>{n.children.length ? '📁' : '🎞'} {n.name}</span>
								{#if n.note}<span class="note">← {n.note}</span>{/if}
							</div>
							{@render branch(n.children, depth + 1)}
						{/each}
					{/snippet}
					{@render branch(tree, 1)}
				</div>
			{/if}
		</div>

		<div class="row">
			<button class="primary" onclick={save} disabled={!dirty || !!busy || !!previewError}>Save naming</button>
			{#if dirty}<button onclick={() => ((draft = { ...saved!.patterns }), (custom = saved!.preset === 'custom'))} disabled={!!busy}>Undo changes</button>{/if}
			{#if dirty}<span class="muted small">Not saved yet.</span>{/if}
		</div>

		{#if renames && renames.length > 0}
			<div class="notice warn stack">
				<div class="spread">
					<span>{renames.length} file{renames.length === 1 ? '' : 's'} already in the library {renames.length === 1 ? 'has' : 'have'} different names than this naming gives.</span>
					<button class="small" onclick={() => (showRenames = !showRenames)}>{showRenames ? 'Hide' : 'Review renames'}</button>
				</div>
				{#if showRenames}
					<div class="renames">
						{#each renames as r (r.file_id)}
							<div class="rename">
								<div class="small old">{r.current}</div>
								<div class="small new">→ {r.new}</div>
							</div>
						{/each}
					</div>
					<div class="row">
						<button class="primary" onclick={rename} disabled={!!busy || dirty}>
							{busy === 'rename' ? 'Renaming…' : `Rename ${renames.length} file${renames.length === 1 ? '' : 's'}`}
						</button>
						<span class="muted small">Files are renamed in place (same disk, nothing copied); seeding is not affected.</span>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.lib {
		gap: 1rem;
		min-width: 0;
	}
	.lib :global(.row) {
		flex-wrap: wrap;
		min-width: 0;
	}
	.field {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	h3 {
		margin: 0;
	}
	.label {
		font-weight: 600;
		margin-bottom: 0.35rem;
	}
	.grow {
		flex: 1;
		min-width: 14rem;
	}
	.presets {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr));
		gap: 0.5rem;
	}
	.preset {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.2rem;
		text-align: left;
		padding: 0.6rem 0.75rem;
		height: 100%;
		white-space: normal;
	}
	.preset.on,
	.seg button.on {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.seg {
		gap: 0.35rem;
	}
	.tokens {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		align-items: center;
	}
	.token {
		font-family: var(--mono, ui-monospace, monospace);
		padding: 0.1rem 0.45rem;
	}
	input[data-key] {
		width: 100%;
		font-family: var(--mono, ui-monospace, monospace);
	}
	.hint {
		margin: 0;
	}
	.tree {
		font-family: var(--mono, ui-monospace, monospace);
		font-size: 0.82rem;
		background: var(--surface-2);
		border-radius: var(--radius);
		padding: 0.6rem 0.75rem;
		overflow-x: auto;
	}
	.line {
		white-space: nowrap;
		line-height: 1.6;
	}
	.root {
		color: var(--muted);
	}
	.note {
		color: var(--muted);
		font-family: var(--font, system-ui, sans-serif);
		margin-left: 0.6rem;
		font-size: 0.78rem;
	}
	.renames {
		max-height: 16rem;
		overflow: auto;
		background: var(--surface);
		border-radius: var(--radius);
		padding: 0.5rem;
	}
	.rename {
		padding: 0.25rem 0;
		border-bottom: 1px solid var(--border);
		word-break: break-all;
	}
	.old {
		color: var(--muted);
		text-decoration: line-through;
	}
</style>
