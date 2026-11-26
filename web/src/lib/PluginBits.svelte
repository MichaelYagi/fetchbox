<script lang="ts" module>
	import { get, type S } from './api.ts';

	// The running plugins' actions, fetched once per page load and shared by every place
	// on the title page that shows them (none when no plugin is running).
	export type Act = S['ActionOut'] & { plugin: string };
	let cache: Promise<Act[]> | null = null;
	export function pluginActions(): Promise<Act[]> {
		cache ??= get<S['PluginOut'][]>('/plugins')
			.then((ps) => ps.filter((p) => p.running).flatMap((p) => p.actions.map((a) => ({ ...a, plugin: p.id }))))
			.catch(() => []);
		return cache;
	}
</script>

<script lang="ts">
	import { ApiError, post } from './api.ts';

	// Plugins on a title page: their panels (drawn by Fetchbox: a status line, a table,
	// buttons) and their actions. Nothing shows when no plugin has anything for it.
	let { titleId, itemId = null }: { titleId: string; itemId?: string | null } = $props();

	type Panel = S['PanelOut'];
	let actions = $state<Act[]>([]);
	let panels = $state<Panel[]>([]);
	let note = $state('');
	let busy = $state('');

	$effect(() => {
		const t = titleId;
		pluginActions().then((a) => (actions = a.filter((x) => x.on === 'title' || (x.on === 'item' && itemId))));
		get<Panel[]>('/plugins/panels', { on: 'title', id: t })
			.then((p) => (panels = p))
			.catch(() => (panels = []));
	});

	async function run(a: Act) {
		busy = `${a.plugin}/${a.method}`;
		note = '';
		try {
			await post(`/plugins/${a.plugin}/actions/${a.addon}/${a.method}`, { item: a.on === 'title' ? titleId : itemId });
			note = `${a.label}: started (see Activity)`;
		} catch (e) {
			note = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}
	const byLabel = (p: Panel, label: string) => actions.find((a) => a.plugin === p.plugin && a.label === label);
</script>

{#if actions.length || panels.length}
	<h2>Plugins</h2>
	{#if actions.length}
		<div class="row">
			{#each actions as a (`${a.plugin}/${a.addon}/${a.method}`)}
				<button onclick={() => run(a)} disabled={!!busy}>{a.label}</button>
			{/each}
		</div>
	{/if}
	{#each panels as p (`${p.plugin}/${p.addon}/${p.method}`)}
		<div class="card stack">
			<div class="spread"><strong>{p.title}</strong>{#if p.status}<span class="muted small">{p.status}</span>{/if}</div>
			{#if p.error}<p class="notice warn small">{p.error}</p>{/if}
			{#if p.table.length}
				{@const cols = Object.keys(p.table[0])}
				<div class="table-wrap">
					<table>
						<thead><tr>{#each cols as c (c)}<th>{c}</th>{/each}</tr></thead>
						<tbody>
							{#each p.table as row, i (i)}<tr>{#each cols as c (c)}<td>{row[c] ?? ''}</td>{/each}</tr>{/each}
						</tbody>
					</table>
				</div>
			{/if}
			{#if p.actions.length}
				<div class="row">
					{#each p.actions as label (label)}
						{@const a = byLabel(p, label)}
						{#if a}<button class="small" onclick={() => run(a)} disabled={!!busy}>{label}</button>{/if}
					{/each}
				</div>
			{/if}
		</div>
	{/each}
	{#if note}<p class="muted small">{note}</p>{/if}
{/if}
