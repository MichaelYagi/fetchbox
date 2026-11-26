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
		$ref?: string;
	};
	const schema = $derived((part.schema ?? {}) as { properties?: Record<string, Prop>; required?: string[]; $defs?: Record<string, Prop> });
	const fields = $derived(Object.entries(schema.properties ?? {}));
	// enums arrive as a $ref to $defs
	const resolve = (p: Prop): Prop => (p.$ref ? { ...p, ...(schema.$defs?.[p.$ref.split('/').pop() ?? ''] ?? {}) } : p);

	let values = $state<Record<string, unknown>>({});
	let error = $state('');
	let message = $state('');
	let busy = $state(false);
	$effect(() => {
		const v: Record<string, unknown> = {};
		for (const [k, p] of Object.entries(schema.properties ?? {})) {
			const stored = part.values?.[k];
			v[k] = stored ?? (resolve(p).writeOnly ? '' : (resolve(p).default ?? ''));
			if (resolve(p).type === 'array' && Array.isArray(v[k])) v[k] = (v[k] as unknown[]).join('\n');
		}
		values = v;
	});

	function body(): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const [k, raw] of fields) {
			const p = resolve(raw);
			let v = values[k];
			if (p.type === 'array') v = String(v ?? '').split('\n').map((x) => x.trim()).filter(Boolean);
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
			error = e instanceof ApiError ? [e.message, ...(e.errors ?? []).map((x) => x.message)].join(' · ') : String(e);
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
		{#if p.type === 'boolean'}
			<label class="check"><input type="checkbox" bind:checked={values[key] as boolean} /> {label}</label>
		{:else}
			<label class="field">
				<span>{label}{#if needed}<span class="needed"> · needed</span>{/if}</span>
				{#if p.enum}
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
