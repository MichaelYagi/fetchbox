<script lang="ts">
	import { ApiError, get, post, type S } from './api.ts';

	// Move settings between installs: export to a JSON file, import with a preview first.
	let password = $state('');
	let importPassword = $state('');
	let doc = $state<Record<string, unknown> | null>(null);
	let fileName = $state('');
	let plan = $state<S['ConfigImportOut'] | null>(null);
	let busy = $state('');
	let error = $state('');
	let message = $state('');

	const AREAS: Record<string, string> = {
		quality_profile: 'Quality profiles',
		custom_format: 'Custom formats',
		naming_template: 'Naming',
		indexer_definition: 'Indexer definitions',
		indexer: 'Indexers',
		download_client: 'Download clients',
		storage_node: 'Storage',
		destination: 'Library folders',
		media_server: 'Media servers',
		notification_target: 'Notifications',
		schedule: 'Schedules',
		system: 'System settings'
	};

	async function act(name: string, fn: () => Promise<unknown>) {
		busy = name;
		error = message = '';
		try {
			await fn();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const exportNow = () =>
		act('export', async () => {
			const data = password ? await post('/config/export', { password }) : await get('/config/export');
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const a = document.createElement('a');
			a.href = URL.createObjectURL(blob);
			a.download = `fetchbox-settings-${new Date().toISOString().slice(0, 10)}.json`;
			a.click();
			URL.revokeObjectURL(a.href);
			message = password ? 'Exported, with secrets encrypted by your password.' : 'Exported (without passwords and API keys).';
			password = '';
		});

	async function onFile(e: Event) {
		const f = (e.currentTarget as HTMLInputElement).files?.[0];
		if (!f) return;
		plan = null;
		error = '';
		try {
			doc = JSON.parse(await f.text());
			fileName = f.name;
		} catch {
			error = `${f.name} isn't a settings export`;
			doc = null;
		}
	}
	const preview = () =>
		act('preview', async () => {
			plan = await post<S['ConfigImportOut']>('/config/import', { document: doc, password: importPassword || null });
		});
	const apply = () =>
		act('apply', async () => {
			const r = await post<S['ConfigImportOut']>('/config/import', { document: doc, password: importPassword || null, apply: true });
			plan = null;
			doc = null;
			message = r.restarting ? 'Imported: Fetchbox is restarting. Reload this page in a moment.' : 'Imported: restart Fetchbox to use everything.';
		});
	const changes = (p: S['ConfigImportOut']) =>
		Object.values(p.plan).reduce((n, x) => n + x.create.length + x.update.length, 0);
</script>

<div class="card stack">
	<p class="muted small">
		Copy every setting (profiles, indexers, clients, folders, naming, connections) to another Fetchbox, not your library
		itself. Passwords and API keys are only included with a password, and then encrypted.
	</p>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}
	<div class="row">
		<button onclick={exportNow} disabled={!!busy || (password.length > 0 && password.length < 8)}>Export settings</button>
		<input class="grow" type="password" placeholder="Password to include secrets (optional)" bind:value={password} autocomplete="new-password" />
	</div>
	<div class="row">
		<label class="small muted">Import: <input type="file" accept=".json,application/json" onchange={onFile} /></label>
		{#if doc}
			<input type="password" placeholder="Its password, if it has secrets" bind:value={importPassword} autocomplete="off" />
			<button onclick={preview} disabled={!!busy}>{busy === 'preview' ? 'Comparing…' : 'Show what changes'}</button>
		{/if}
	</div>
	{#if plan}
		<div class="plan">
			<strong>{fileName}: {changes(plan)} change{changes(plan) === 1 ? '' : 's'}</strong>
			{#each Object.entries(plan.plan) as [area, p] (area)}
				{#if p.create.length || p.update.length}
					<div class="small">
						<span class="area">{AREAS[area] ?? area}</span>
						{#if p.create.length}<span class="badge ok">+ {p.create.join(', ')}</span>{/if}
						{#if p.update.length}<span class="badge">changed: {p.update.join(', ')}</span>{/if}
					</div>
				{/if}
			{/each}
			<p class="muted small">Nothing is deleted. Applying restarts Fetchbox; a backup first is a good idea.</p>
			<div><button class="primary" onclick={apply} disabled={!!busy || changes(plan) === 0}>{busy === 'apply' ? 'Importing…' : 'Import these changes'}</button></div>
		</div>
	{/if}
</div>

<style>
	.plan {
		display: grid;
		gap: 0.35rem;
		border-top: 1px solid var(--border);
		padding-top: 0.5rem;
	}
	.area {
		display: inline-block;
		min-width: 9rem;
		color: var(--muted);
	}
	.badge {
		margin-right: 0.3rem;
	}
</style>
