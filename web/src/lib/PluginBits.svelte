<script lang="ts" module>
	import { get, post, type S } from './api.ts';

	// The running plugins' actions, fetched once per page load and shared by every place
	// on the title page that shows them (none when no plugin is running).
	export type Act = S['ActionOut'] & { plugin: string; pluginName: string };
	let cache: Promise<Act[]> | null = null;
	// Run a plugin's button and wait for its job: what it said (its message, or what it
	// returned), for showing where it was pressed. Also in Activity.
	export async function runAction(a: Act, item: string | null): Promise<string> {
		const r = await post<S['PluginJobOut']>(`/plugins/${a.plugin}/actions/${a.addon}/${a.method}`, { item });
		if (!r.job_id) return `${a.label}: already running`;
		for (let i = 0; i < 400; i++) {
			await new Promise((ok) => setTimeout(ok, 1000));
			const j = await get<S['JobOut']>(`/jobs/${r.job_id}`);
			if (j.status === 'succeeded') {
				const res = (j.result ?? {}) as Record<string, unknown>;
				const said = typeof res.message === 'string' ? res.message : typeof res.skipped === 'string' ? res.skipped : 'done';
				return `${a.label}: ${said}`;
			}
			if (j.status === 'failed' || j.status === 'cancelled') return `${a.label} failed: ${j.error ?? 'see Activity'}`;
		}
		return `${a.label}: still working (see Activity)`;
	}
	export const NO_FILE = "No file yet: this works once it's downloaded";

	export function pluginActions(): Promise<Act[]> {
		cache ??= get<S['PluginOut'][]>('/plugins')
			.then((ps) => ps.filter((p) => p.running).flatMap((p) => p.actions.map((a) => ({ ...a, plugin: p.id, pluginName: p.name }))))
			.catch(() => []);
		return cache;
	}
</script>

<script lang="ts">
	import { ApiError } from './api.ts';

	// Plugins on a title page: their panels (drawn by Fetchbox: a status line, a table,
	// buttons) and their actions. Nothing shows when no plugin has anything for it.
	// hasFile: the title has a downloaded file (an item: its own); buttons that need one are off without
	// onchanged: after a button's job, so the page shows what it did (files, history)
	let {
		titleId,
		kind,
		itemId = null,
		hasFile = true,
		onchanged = () => {}
	}: { titleId: string; kind: string; itemId?: string | null; hasFile?: boolean; onchanged?: () => unknown } = $props();
	async function loadPanels() {
		panels = await get<Panel[]>('/plugins/panels', { on: 'title', id: titleId }).catch(() => []);
	}

	type Panel = S['PanelOut'];
	let actions = $state<Act[]>([]);
	let panels = $state<Panel[]>([]);
	let notes = $state<Record<string, string>>({}); // plugin id -> its last button's result
	let busy = $state('');

	// One card per plugin: its panel's title and status, its buttons, its table, and what
	// its last button did. A plugin with a button for the whole title doesn't repeat its
	// per-item one here (on a movie they'd do the same).
	type Card = { plugin: string; name: string; panels: Panel[]; acts: Act[] };
	const cards = $derived.by(() => {
		const out = new Map<string, Card>();
		const card = (plugin: string, name: string) => {
			if (!out.has(plugin)) out.set(plugin, { plugin, name, panels: [], acts: [] });
			return out.get(plugin)!;
		};
		for (const p of panels) card(p.plugin, p.title).panels.push(p);
		for (const a of actions) {
			const c = card(a.plugin, a.pluginName);
			const whole = actions.some((x) => x.plugin === a.plugin && x.on === 'title');
			if (a.on === 'item' && whole) continue;
			if (!c.acts.some((x) => x.label === a.label)) c.acts.push(a);
		}
		return [...out.values()];
	});

	$effect(() => {
		const t = titleId;
		const k = kind;
		pluginActions().then((a) => (actions = a.filter((x) => (x.on === 'title' || (x.on === 'item' && itemId)) && (!x.kinds || x.kinds.includes(k)))));
		get<Panel[]>('/plugins/panels', { on: 'title', id: t })
			.then((p) => (panels = p))
			.catch(() => (panels = []));
	});

	async function run(a: Act) {
		busy = `${a.plugin}/${a.method}`;
		notes[a.plugin] = 'Working…';
		try {
			notes[a.plugin] = await runAction(a, a.on === 'title' ? titleId : itemId);
			await loadPanels(); // its status, as it is now
			onchanged();
		} catch (e) {
			notes[a.plugin] = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}
	const off = (a: Act) => a.needs_file && !hasFile;
</script>

{#if cards.length}
	<h2>Plugins</h2>
	{#each cards as c (c.plugin)}
		<div class="card stack plugin">
			{#each c.panels.length ? c.panels : [null] as p, i (i)}
				<div class="spread head">
					<strong>{p?.title ?? c.name}</strong>
					{#if p?.status}<span class="muted small">{p.status}</span>{/if}
				</div>
				{#if p?.error}<p class="notice warn small">{p.error}</p>{/if}
			{/each}
			{#if c.acts.length}
				<div class="row">
					{#each c.acts as a (`${a.addon}/${a.method}`)}
						<button class="small" onclick={() => run(a)} disabled={!!busy || off(a)} title={off(a) ? NO_FILE : undefined}>{a.label}</button>
					{/each}
					{#if notes[c.plugin]}<span class="muted small">{notes[c.plugin]}</span>{/if}
				</div>
			{:else if notes[c.plugin]}
				<p class="muted small">{notes[c.plugin]}</p>
			{/if}
			{#each c.panels as p (`${p.addon}/${p.method}`)}
				{#if p.table.length}
					{@const cols = Object.keys(p.table[0])}
					<div class="table-wrap">
						<table>
							<thead><tr>{#each cols as col (col)}<th>{col}</th>{/each}</tr></thead>
							<tbody>
								{#each p.table as row, r (r)}<tr>{#each cols as col (col)}<td>{row[col] ?? ''}</td>{/each}</tr>{/each}
							</tbody>
						</table>
					</div>
				{/if}
			{/each}
		</div>
	{/each}
{/if}

<style>
	.plugin {
		margin-bottom: 0.75rem;
		gap: 0.75rem;
	}
	.plugin .head {
		align-items: baseline;
		gap: 0.5rem 1rem;
		flex-wrap: wrap;
	}
</style>
