<script lang="ts">
	import { ApiError, post, put, type S } from './api.ts';

	// A plugin's settings form, drawn from its settings model (JSON Schema): plugins write no
	// form code. Secrets are password fields; one already set shows as •••••••• and is kept
	// unless something new is typed.
	let { plugin, part, onsaved }: { plugin: string; part: S['SettingsOut']; onsaved: () => void } = $props();

	const MASK = '••••••••';
	type Prop = {
		title?: string;
		description?: string;
		type?: string;
		format?: string;
		writeOnly?: boolean;
		enum?: unknown[];
		items?: { type?: string };
		minimum?: number;
		maximum?: number;
		default?: unknown;
		anyOf?: Prop[];
		'x-fetchbox'?: string;
		'x-fetchbox-choices'?: Record<string, string>; // value -> name: pick by name
		$ref?: string;
	};
	const schema = $derived((part.schema ?? {}) as { properties?: Record<string, Prop>; required?: string[]; $defs?: Record<string, Prop> });
	const fields = $derived(Object.entries(schema.properties ?? {}));
	// enums arrive as a $ref to $defs
	const resolve = (p: Prop): Prop => (p.$ref ? { ...p, ...(schema.$defs?.[p.$ref.split('/').pop() ?? ''] ?? {}) } : p);
	const choicesOf = (p: Prop) => p['x-fetchbox-choices'];
	// a list with choices: picked by name, in order (the first comes first)
	const picked = (k: string) => (values[k] as string[] | undefined) ?? [];
	const add = (k: string, v: string) => v && !picked(k).includes(v) && (values[k] = [...picked(k), v]);
	const drop = (k: string, v: string) => (values[k] = picked(k).filter((x) => x !== v));
	const earlier = (k: string, i: number) => {
		const xs = [...picked(k)];
		[xs[i - 1], xs[i]] = [xs[i], xs[i - 1]];
		values[k] = xs;
	};

	let values = $state<Record<string, unknown>>({});
	let error = $state('');
	let message = $state('');
	let busy = $state(false);
	$effect(() => {
		const v: Record<string, unknown> = {};
		for (const [k, p] of Object.entries(schema.properties ?? {})) {
			const stored = part.values?.[k];
			v[k] = stored ?? (resolve(p).writeOnly ? '' : (resolve(p).default ?? ''));
			if (resolve(p).type === 'array' && choicesOf(resolve(p))) v[k] = Array.isArray(v[k]) ? [...(v[k] as string[])] : [];
			else if (resolve(p).type === 'array' && Array.isArray(v[k])) v[k] = (v[k] as unknown[]).join('\n');
		}
		values = v;
	});

	function body(): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const [k, raw] of fields) {
			const p = resolve(raw);
			let v = values[k];
			if (p.type === 'array' && choicesOf(p)) v = [...picked(k)];
			else if (p.type === 'array') v = String(v ?? '').split('\n').map((x) => x.trim()).filter(Boolean);
			else if (p.type === 'integer' || p.type === 'number') v = v === '' || v === null ? null : Number(v);
			else if (p.writeOnly && v === '') continue; // not typed: unchanged
			out[k] = v;
		}
		return out;
	}

	async function save() {
		busy = true;
		error = message = '';
		try {
			await put(`/plugins/${plugin}/settings/${part.id}`, { values: body() });
			message = 'Saved';
			onsaved();
		} catch (e) {
			error = e instanceof ApiError ? [e.message, ...e.fields.map((x) => x.message)].join(' · ') : String(e);
		} finally {
			busy = false;
		}
	}

	async function test() {
		busy = true;
		error = message = '';
		try {
			const r = await post<S['TestOut']>(`/plugins/${plugin}/settings/${part.id}/test`, { values: body() });
			if (r.ok) message = r.message;
			else error = r.message;
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<form class="stack" onsubmit={(e) => (e.preventDefault(), save())}>
	{#each fields as [key, raw] (key)}
		{@const p = resolve(raw)}
		{@const label = p.title ?? key}
		{@const needed = part.missing.includes(key)}
		{@const choices = choicesOf(p)}
		{#if p.type === 'boolean'}
			<label class="check"><input type="checkbox" bind:checked={values[key] as boolean} /> {label}</label>
		{:else if p.type === 'array' && choices}
			<div class="field">
				<span>{label}{#if needed}<span class="needed"> · needed</span>{/if}</span>
				<div class="chosen">
					{#each picked(key) as v, i (v)}
						<span class="chip">
							{choices[v] ?? v}
							{#if i > 0}<button type="button" class="x" title="Move earlier" aria-label="Move {choices[v] ?? v} earlier" onclick={() => earlier(key, i)}>↑</button>{/if}
							<button type="button" class="x" title="Remove" aria-label="Remove {choices[v] ?? v}" onclick={() => drop(key, v)}>×</button>
						</span>
					{:else}
						<span class="muted small">None yet</span>
					{/each}
				</div>
				<select aria-label="Add to {label}" onchange={(e) => (add(key, e.currentTarget.value), (e.currentTarget.value = ''))}>
					<option value="">Add…</option>
					{#each Object.entries(choices).filter(([v]) => !picked(key).includes(v)).sort((a, b) => a[1].localeCompare(b[1])) as [v, name] (v)}<option value={v}>{name}</option>{/each}
				</select>
				{#if p.description}<span class="muted small">{p.description}</span>{/if}
			</div>
		{:else}
			<label class="field">
				<span>{label}{#if needed}<span class="needed"> · needed</span>{/if}</span>
				{#if choices}
					<select bind:value={values[key]}>
						{#each Object.entries(choices) as [v, name] (v)}<option value={v}>{name}</option>{/each}
					</select>
				{:else if p.enum}
					<select bind:value={values[key]}>
						{#each p.enum as opt (String(opt))}<option value={opt}>{String(opt)}</option>{/each}
					</select>
				{:else if p.type === 'array'}
					<textarea rows="3" bind:value={values[key]} placeholder="One per line"></textarea>
				{:else if p.type === 'integer' || p.type === 'number'}
					<input type="number" bind:value={values[key]} min={p.minimum} max={p.maximum} step={p.type === 'integer' ? 1 : 'any'} />
				{:else if p.writeOnly || p.format === 'password'}
					<input type="password" autocomplete="off" bind:value={values[key]} placeholder={part.values?.[key] === MASK ? 'Set: type to replace it' : ''} />
				{:else}
					<input type="text" bind:value={values[key]} placeholder={p['x-fetchbox'] === 'folder' ? '/path/to/a/folder' : p['x-fetchbox'] === 'url' ? 'https://' : ''} />
				{/if}
				{#if p.description}<span class="muted small">{p.description}</span>{/if}
			</label>
		{/if}
	{/each}
	<div class="row">
		<button class="primary" disabled={busy}>Save</button>
		{#if part.has_test}<button type="button" onclick={test} disabled={busy}>Test</button>{/if}
		{#if message}<span class="ok small">{message}</span>{/if}
	</div>
	{#if error}<p class="notice error small">{error}</p>{/if}
</form>

<style>
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.chosen {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		padding: 0.15rem 0.3rem 0.15rem 0.6rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface-2);
		font-size: 0.9rem;
	}
	.chip .x {
		background: none;
		border: 0;
		padding: 0 0.25rem;
		color: var(--muted);
		cursor: pointer;
	}
	.chip .x:hover {
		color: var(--text);
	}
	.needed {
		color: var(--warn, #d9a441);
	}
	.check {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	.ok {
		color: var(--ok, #3fb37f);
	}
</style>
