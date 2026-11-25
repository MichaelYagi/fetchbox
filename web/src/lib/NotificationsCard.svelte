<script lang="ts">
	import { ask } from '#lib/dialogs.svelte.ts';
	import { ApiError, del, get, patch, post, type S } from './api.ts';

	// Where Fetchbox sends a short message when something happens (Discord, email, a webhook).
	type Target = S['TargetOut'];
	let targets = $state<Target[]>([]);
	let events = $state<S['EventKindOut'][]>([]);
	let results = $state<Record<string, S['TestResult']>>({});
	let editing = $state<string | null>(null); // a target id, or 'new'
	const blank = () => ({
		kind: 'discord',
		name: 'Discord',
		url: '',
		secret: '',
		// email
		host: '',
		port: '587',
		username: '',
		password: '',
		to: '',
		sender: '',
		events: [] as string[]
	});
	let draft = $state(blank());
	let busy = $state('');
	let error = $state('');

	const KINDS: Record<string, { label: string; field: string; placeholder: string; help: string }> = {
		discord: {
			label: 'Discord',
			field: 'webhook_url',
			placeholder: 'https://discord.com/api/webhooks/…',
			help: 'In Discord: channel settings › Integrations › Webhooks › New Webhook › Copy Webhook URL.'
		},
		email: {
			label: 'Email',
			field: 'host',
			placeholder: 'smtp.gmail.com',
			help: 'Gmail: server smtp.gmail.com, port 587, your Gmail address as the user name, and an app password (Google Account › Security › 2-Step Verification › App passwords), not your normal password.'
		},
		webhook: {
			label: 'Webhook',
			field: 'url',
			placeholder: 'http://homeassistant.local:8123/api/webhook/fetchbox',
			help: 'Fetchbox POSTs JSON (event, title, body, level, data) to this address: Home Assistant, n8n, ntfy bridges…'
		}
	};

	async function load() {
		try {
			[targets, events] = await Promise.all([
				get<Target[]>('/notifications/targets'),
				get<S['EventKindOut'][]>('/notifications/events')
			]);
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

	function startAdd() {
		draft = { ...blank(), events: events.filter((e) => e.default).map((e) => e.event) };
		editing = 'new';
	}
	function startEdit(t: Target) {
		const v = (k: string) => String(t.settings[k] ?? '');
		draft = {
			...blank(),
			kind: t.kind,
			name: t.name,
			url: v(KINDS[t.kind]?.field ?? 'url'),
			secret: v('secret'),
			host: v('host'),
			port: v('port') || '587',
			username: v('username'),
			password: v('password'), // the mask: sent back, it keeps the password
			to: v('to'),
			sender: v('sender'),
			events: [...t.events]
		};
		editing = t.id;
	}
	function toggleEvent(ev: string, on: boolean) {
		draft.events = on ? [...draft.events, ev] : draft.events.filter((e) => e !== ev);
	}

	const save = (e: SubmitEvent) => {
		e.preventDefault();
		act('save', async () => {
			const orNull = (x: string) => x.trim() || null;
			const settings: Record<string, string | number | null> =
				draft.kind === 'email'
					? {
							host: draft.host.trim(),
							port: Number(draft.port) || 587,
							username: orNull(draft.username),
							password: orNull(draft.password),
							to: draft.to.trim(),
							sender: orNull(draft.sender)
						}
					: { [KINDS[draft.kind].field]: draft.url.trim() };
			if (draft.kind === 'webhook') settings.secret = orNull(draft.secret);
			const body = { name: draft.name.trim(), settings, events: draft.events };
			const row =
				editing === 'new'
					? await post<Target>('/notifications/targets', { kind: draft.kind, ...body })
					: await patch<Target>(`/notifications/targets/${editing}`, body, targets.find((t) => t.id === editing)?.etag);
			editing = null;
			results[row.id] = await post<S['TestResult']>(`/notifications/targets/${row.id}/test`);
			await load();
		});
	};
	const test = (t: Target) =>
		act(`test-${t.id}`, async () => {
			results[t.id] = await post<S['TestResult']>(`/notifications/targets/${t.id}/test`);
			await load();
		});
	const toggle = (t: Target) =>
		act(`en-${t.id}`, async () => {
			await patch(`/notifications/targets/${t.id}`, { enabled: !t.enabled }, t.etag);
			await load();
		});
	const remove = async (t: Target) => {
		if (!(await ask(`Stop sending notifications to ${t.name}?`, 'Remove', true))) return;
		act(`rm-${t.id}`, async () => {
			await del(`/notifications/targets/${t.id}`, undefined, t.etag);
			await load();
		});
	};
	const label = (ev: string) => events.find((e) => e.event === ev)?.label ?? ev;
</script>

<div class="stack">
	<p class="muted small">Get a message when something finishes or goes wrong: by email, in a Discord channel, or anywhere that takes a webhook.</p>
	{#if error}<p class="notice error">{error}</p>{/if}

	{#each targets as t (t.id)}
		<div class="card stack target">
			<div class="spread">
				<div>
					<strong>{t.name}</strong> <span class="badge">{KINDS[t.kind]?.label ?? t.kind}</span>
					{#if !t.enabled}<span class="badge">Off</span>{/if}
					<div class="muted small">{t.events.map(label).join(' · ') || 'no events chosen'}</div>
				</div>
				<div class="row">
					<button class="small" onclick={() => test(t)} disabled={!!busy}>{busy === `test-${t.id}` ? 'Sending…' : 'Send a test'}</button>
					<button class="small" onclick={() => (editing === t.id ? (editing = null) : startEdit(t))} disabled={!!busy}>
						{editing === t.id ? 'Cancel' : 'Edit'}
					</button>
					<button class="small" onclick={() => toggle(t)} disabled={!!busy}>{t.enabled ? 'Turn off' : 'Turn on'}</button>
					<button class="small danger" onclick={() => remove(t)} disabled={!!busy}>Remove</button>
				</div>
			</div>
			{#if results[t.id]}<p class="notice {results[t.id].ok ? '' : 'error'} small">{results[t.id].message}</p>{/if}
			{#if t.last_error && !results[t.id]}<p class="notice warn small">Last message failed: {t.last_error}</p>{/if}
			{#if editing === t.id}{@render form()}{/if}
		</div>
	{/each}

	{#if editing === 'new'}
		<div class="card stack">{@render form()}</div>
	{:else}
		<div><button onclick={startAdd} disabled={!!busy || events.length === 0}>Add a notification</button></div>
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
		{#if draft.kind === 'email'}
			<div class="row">
				<label class="grow">Name <input bind:value={draft.name} required /></label>
				<label class="grow2">Send to
					<input bind:value={draft.to} required type="text" inputmode="email" placeholder="you@example.com (several: separate with commas)" autocomplete="off" spellcheck="false" />
				</label>
			</div>
			<div class="row">
				<label class="grow2">Mail server
					<input bind:value={draft.host} required placeholder="smtp.gmail.com" autocomplete="off" spellcheck="false" />
				</label>
				<label class="port">Port <input bind:value={draft.port} required inputmode="numeric" placeholder="587" /></label>
			</div>
			<div class="row">
				<label class="grow">User name <input bind:value={draft.username} placeholder="you@gmail.com" autocomplete="off" spellcheck="false" /></label>
				<label class="grow">Password <input bind:value={draft.password} type="password" placeholder="app password" autocomplete="new-password" /></label>
			</div>
			<label>From <span class="muted small">(optional: the user name when empty)</span>
				<input bind:value={draft.sender} placeholder="fetchbox@example.com" autocomplete="off" spellcheck="false" />
			</label>
		{:else}
			<div class="row">
				<label class="grow">Name <input bind:value={draft.name} required /></label>
				<label class="grow2">
					{draft.kind === 'discord' ? 'Webhook URL' : 'URL'}
					<input bind:value={draft.url} required placeholder={KINDS[draft.kind].placeholder} autocomplete="off" spellcheck="false" />
				</label>
			</div>
		{/if}
		<p class="muted small hint">{KINDS[draft.kind].help}</p>
		{#if draft.kind === 'webhook'}
			<label>Secret <span class="muted small">(optional, sent as X-Fetchbox-Secret)</span>
				<input bind:value={draft.secret} autocomplete="off" />
			</label>
		{/if}
		<fieldset class="events">
			<legend>Send a message when…</legend>
			{#each events as ev (ev.event)}
				<label class="inline">
					<input type="checkbox" checked={draft.events.includes(ev.event)} onchange={(e) => toggleEvent(ev.event, e.currentTarget.checked)} />
					{ev.label}
				</label>
			{/each}
		</fieldset>
		<div class="row">
			<button class="primary" disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save and send a test'}</button>
			{#if editing === 'new'}<button type="button" onclick={() => (editing = null)}>Cancel</button>{/if}
		</div>
	</form>
{/snippet}

<style>
	.target {
		gap: 0.5rem;
	}
	.target :global(.row) {
		flex-wrap: wrap;
	}
	.grow {
		flex: 1;
		min-width: 10rem;
	}
	.grow2 {
		flex: 2;
		min-width: 14rem;
	}
	.port {
		flex: 0 1 6rem;
		min-width: 5rem;
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
	.events {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.5rem 0.75rem;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
		gap: 0.3rem 1rem;
	}
	.inline {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
</style>
