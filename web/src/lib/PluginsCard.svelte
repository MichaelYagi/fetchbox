<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import PluginSettingsForm from './PluginSettingsForm.svelte';
	import { ApiError, del, get, patch, post, upload, type S } from './api.ts';
	import { session } from './session.svelte.ts';
	// where .whl files copied by hand are installed from (a NAS share, a Docker volume)
	const dropFolder = $derived.by(() => {
		const d = session.host?.data_dir;
		if (!d) return null;
		const sep = d.includes('\\') && !d.includes('/') ? '\\' : '/'; // as this system writes paths
		return [d.replace(/[\\/]+$/, ''), 'plugins', 'drop'].join(sep);
	});

	// Plugins: what's installed, on or off, what each uses, its settings; install from a
	// wheel file. Plugins run with Fetchbox's full access, so the install and the first time
	// one is turned on say so plainly.
	type P = S['PluginOut'];
	let plugins = $state<P[] | null>(null);
	let error = $state('');
	let busy = $state('');
	let open = $state<Record<string, boolean>>({});

	async function load() {
		try {
			plugins = await get<P[]>('/plugins');
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
	});

	const TRUST = 'Plugins run inside Fetchbox with its full access: your settings, your files, the network. Only install plugins from people you trust.';
	const uses = (p: P) => (p.permissions.length ? p.permissions.map((x) => x.words).join(' · ') : 'Nothing beyond Fetchbox itself');

	function status(p: P): [string, string] {
		if (p.running) return ['Running', 'ok'];
		if (!p.approved) return ['Not approved', 'warn'];
		if (!p.enabled) return ['Off', 'muted'];
		if (p.settings.some((s) => s.missing.length)) return ['Needs settings', 'warn'];
		return ['Not running', 'error'];
	}

	async function act(name: string, fn: () => Promise<unknown>) {
		busy = name;
		error = '';
		try {
			await fn();
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	// A plugin's actions for the whole library: run as a job; its result shown when done
	let running = $state<Record<string, boolean>>({});
	let results = $state<Record<string, string>>({});
	async function runLibrary(p: P, a: P['actions'][number]) {
		const key = `${p.id}/${a.addon}/${a.method}`;
		running[key] = true;
		results[key] = 'Working…';
		try {
			const r = await post<S['PluginJobOut']>(`/plugins/${p.id}/actions/${a.addon}/${a.method}`, {});
			if (!r.job_id) {
				results[key] = 'Already running';
				return;
			}
			for (;;) {
				await new Promise((ok) => setTimeout(ok, 1500));
				const j = await get<S['JobOut']>(`/jobs/${r.job_id}`);
				if (j.status === 'succeeded') {
					const res = (j.result ?? {}) as Record<string, unknown>;
					results[key] = typeof res.message === 'string' ? res.message : typeof res.skipped === 'string' ? res.skipped : 'Done';
					break;
				}
				if (j.status === 'failed' || j.status === 'cancelled') {
					results[key] = j.error ?? 'It failed';
					break;
				}
			}
		} catch (e) {
			results[key] = e instanceof ApiError ? e.message : String(e);
		} finally {
			running[key] = false;
		}
	}

	async function toggle(p: P) {
		if (!p.enabled && !p.approved) {
			const ok = await ask(`Turn on ${p.name}?\n\nWhat it can do: ${uses(p)}.\n\n${TRUST}`, 'Turn on');
			if (!ok) return;
		}
		await act(p.id, () => patch(`/plugins/${p.id}`, { enabled: !p.enabled }));
	}

	async function remove(p: P) {
		const ok = await ask(`Remove ${p.name}? Its settings and what it stored are deleted too. Files it filed stay where they are.`, 'Remove', true);
		if (ok) await act(p.id, () => del(`/plugins/${p.id}`));
	}

	let url = $state('');
	async function fromUrl(e: SubmitEvent) {
		e.preventDefault();
		const u = url.trim();
		if (!u) return;
		const ok = await ask(`Install the plugin at ${u}?\n\n${TRUST}`, 'Install');
		if (ok) {
			await act('install', () => post('/plugins/install-url', { url: u }));
			if (!error) url = '';
		}
	}

	async function onFile(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		const ok = await ask(`Install ${file.name}?\n\n${TRUST}`, 'Install');
		if (ok) await act('install', () => upload('/plugins/install', file));
	}
</script>

<div class="card stack">
	<p class="muted small">
		Plugins add to Fetchbox: subtitles, a new kind of thing to collect, one more service. {TRUST}
		<a href="https://github.com/MichaelYagi/fetchbox/blob/main/docs/plugins.md" target="_blank" rel="noreferrer">Writing plugins</a>
	</p>
	<label class="small">Install from a file (.whl): <input type="file" accept=".whl" onchange={onFile} disabled={!!busy} /></label>
	<form class="row small" onsubmit={fromUrl}>
		<input class="grow" type="url" bind:value={url} placeholder="…or its address: https://…/fetchbox_comics-1.0.0-py3-none-any.whl" aria-label="Plugin address" />
		<button disabled={!url.trim() || !!busy}>Install</button>
	</form>
	{#if dropFolder}
		<p class="muted small">
			…or copy the .whl into <code>{dropFolder}</code>: it's installed within a minute and listed here to be turned on. A newer version of one you have replaces it.
		</p>
	{/if}
	{#if error}<p class="notice error small">{error}</p>{/if}
	{#if plugins === null}
		<p class="muted small">Loading…</p>
	{:else if plugins.length === 0}
		<p class="muted small">No plugins installed.</p>
	{:else}
		{#each plugins as p (p.id)}
			{@const [label, tone] = status(p)}
			<div class="plugin stack">
				<div class="spread">
					<div>
						<strong>{p.name}</strong>
						{#if p.version}<span class="muted small">{p.version}</span>{/if}
						<span class="pill {tone} small">{label}</span>
					</div>
					<div class="row">
						<button class="small" onclick={() => toggle(p)} disabled={!!busy || !p.api_supported}>{p.enabled ? 'Turn off' : 'Turn on'}</button>
						<button class="small danger" onclick={() => remove(p)} disabled={!!busy}>Remove</button>
					</div>
				</div>
				{#if p.description}<p class="small">{p.description}</p>{/if}
				<p class="muted small">
					{uses(p)}{#if p.contributes.length}{' · '}Adds: {p.contributes.map((c) => c.name).join(', ')}{/if}
					{#if p.homepage} · <a href={p.homepage} target="_blank" rel="noreferrer">Homepage</a>{/if}
				</p>
				{#if p.running}
					{#each p.actions.filter((a) => a.on === 'library') as a (`${a.addon}/${a.method}`)}
						{@const key = `${p.id}/${a.addon}/${a.method}`}
						<div class="row small">
							<button class="small" onclick={() => runLibrary(p, a)} disabled={!!running[key]}>{a.label}</button>
							{#if results[key]}<span class="muted">{results[key]}</span>{/if}
						</div>
					{/each}
				{/if}
				{#if !p.api_supported}
					<p class="notice warn small">Written for plugin API {p.api}; this Fetchbox supports API 1. A newer Fetchbox may run it.</p>
				{:else if p.error && p.enabled}
					<p class="notice warn small">{p.error}</p>
				{/if}
				{#each p.settings.filter((s) => s.schema) as part (part.id)}
					<details bind:open={open[`${p.id}/${part.id}`]}>
						<summary>{p.settings.length > 1 ? `${part.name} settings` : 'Settings'}{#if part.missing.length}<span class="dot" title="Settings needed"></span>{/if}</summary>
						<PluginSettingsForm plugin={p.id} {part} onsaved={load} />
					</details>
				{/each}
			</div>
		{/each}
	{/if}
</div>

<style>
	.plugin {
		border-top: 1px solid var(--border);
		padding-top: 0.75rem;
	}
	/* its description and what it uses read as one block, not three spaced-out lines */
	.plugin p {
		margin: 0;
	}
	.pill {
		margin-left: 0.4rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		border: 1px solid var(--border);
	}
	.pill.ok {
		color: var(--ok, #3fb37f);
	}
	.pill.warn {
		color: var(--warn, #d9a441);
	}
	.pill.error {
		color: var(--danger, #e5534b);
	}
	.dot {
		display: inline-block;
		width: 0.5rem;
		height: 0.5rem;
		margin-left: 0.4rem;
		border-radius: 50%;
		background: var(--warn, #d9a441);
	}
	summary {
		cursor: pointer;
	}
</style>
