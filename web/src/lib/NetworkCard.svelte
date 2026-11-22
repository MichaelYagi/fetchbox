<script lang="ts">
	import { ApiError, get, patch, type S } from '#lib/api.ts';

	// Reverse proxy (nginx, Caddy, Traefik; Authelia / Authentik) and webhook limits.
	type Net = { trusted_proxies: string[]; auth_header: string | null; block_private_webhooks: boolean };
	let me = $state<S['Me'] | null>(null);
	let proxies = $state('');
	let headerOn = $state(false);
	let header = $state('Remote-User');
	let blockPrivate = $state(false);
	let error = $state('');
	let saved = $state('');
	let busy = $state(false);

	async function load() {
		const cfg = await get<{ config: { network?: Net } }>('/system/config');
		const n = cfg.config.network;
		proxies = (n?.trusted_proxies ?? []).join('\n');
		headerOn = !!n?.auth_header;
		header = n?.auth_header ?? 'Remote-User';
		blockPrivate = n?.block_private_webhooks ?? false;
		me = await get<S['Me']>('/auth/me');
	}
	$effect(() => {
		load().catch(() => {});
	});

	async function save(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = saved = '';
		try {
			await patch('/system/config', {
				network: {
					trusted_proxies: proxies.split(/[\s,]+/).filter(Boolean),
					auth_header: headerOn ? header.trim() || 'Remote-User' : null,
					block_private_webhooks: blockPrivate
				}
			});
			await load();
			saved = 'Saved.';
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const behindProxy = $derived(me && me.connected_from && me.ip !== me.connected_from);
</script>

<form class="card stack" onsubmit={save}>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if me}
		<p class="small">
			Fetchbox sees this browser at <strong class="mono">{me.ip}</strong>{#if behindProxy}, through the proxy at
				<span class="mono">{me.connected_from}</span>{/if}.
		</p>
	{/if}

	<h3>Reverse proxy</h3>
	<p class="muted small">
		Only if Fetchbox sits behind nginx, Caddy, Traefik or similar. List the proxy's address (or its network, like
		<span class="mono">172.18.0.0/16</span> for Docker) so Fetchbox shows and limits by the real visitor's address.
		{#if me?.connected_from && !behindProxy}The address connecting right now is
			<span class="mono">{me.connected_from}</span>.{/if}
	</p>
	<label>Trusted proxies <span class="muted small">(one per line)</span>
		<textarea rows="2" bind:value={proxies} spellcheck="false" placeholder="127.0.0.1"></textarea>
	</label>
	<label class="check"
		><input type="checkbox" bind:checked={headerOn} /> Sign in whoever the proxy says (Authelia, Authentik, …)</label
	>
	{#if headerOn}
		<label class="small">Header
			<input bind:value={header} spellcheck="false" autocomplete="off" />
		</label>
		<p class="notice warn small">
			Anyone who can reach Fetchbox through a trusted proxy and send this header is signed in, so the proxy must
			check the login on every request and remove this header when it comes from the visitor. Don't list
			proxies that don't.
		</p>
	{/if}

	<h3>Webhooks</h3>
	<label class="check"
		><input type="checkbox" bind:checked={blockPrivate} /> Only send webhooks and Discord messages to public internet
		addresses</label
	>
	<p class="muted small">
		For a Fetchbox reachable from the internet: a webhook then can't be pointed at your router or other machines on
		your network. Leave it off to send to Home Assistant or similar at home.
	</p>

	<div class="row">
		<button class="primary" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
		{#if saved}<span class="small">{saved}</span>{/if}
	</div>
</form>

<style>
	.mono {
		font-family: var(--mono, ui-monospace, monospace);
	}
	.check {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}
	h3 {
		margin: 0.4rem 0 0;
	}
</style>
