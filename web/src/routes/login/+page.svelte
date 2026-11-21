<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, get, post, type S } from '#lib/api.ts';
	import { session } from '#lib/session.svelte.ts';

	let setup = $state<boolean | null>(null);
	let username = $state('admin');
	let password = $state('');
	let repeat = $state('');
	let error = $state('');
	let busy = $state(false);

	$effect(() => {
		get<{ setup_required: boolean }>('/auth/status').then((s) => (setup = s.setup_required));
	});

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		error = '';
		if (setup && password !== repeat) {
			error = 'The passwords do not match.';
			return;
		}
		busy = true;
		try {
			await post(setup ? '/auth/setup' : '/auth/login', { username, password });
			session.me = await get<S['Me']>('/auth/me');
			const next = page.url.searchParams.get('next');
			await goto(next && next.startsWith('/') && !next.startsWith('//') ? next : '/');
		} catch (err) {
			error = err instanceof ApiError ? err.message : 'Could not reach Fetchbox.';
		} finally {
			busy = false;
		}
	}
</script>

<main class="center">
	<form class="card stack" onsubmit={submit}>
		<div class="row">
			<img src="/favicon.svg" alt="" width="32" height="32" />
			<h1>Fetchbox</h1>
		</div>
		{#if setup === null}
			<p class="muted">Loading…</p>
		{:else}
			<p class="muted">
				{setup
					? 'Welcome. Choose the admin username and password for this Fetchbox.'
					: 'Sign in to continue.'}
			</p>
			<label>Username <input bind:value={username} autocomplete="username" required /></label>
			<label>
				Password
				<input
					type="password"
					bind:value={password}
					autocomplete={setup ? 'new-password' : 'current-password'}
					required
				/>
			</label>
			{#if setup}
				<label>
					Repeat password
					<input type="password" bind:value={repeat} autocomplete="new-password" required />
				</label>
			{/if}
			{#if error}<p class="notice error">{error}</p>{/if}
			<button class="primary" disabled={busy}>{setup ? 'Create and sign in' : 'Sign in'}</button>
		{/if}
	</form>
</main>

<style>
	.center {
		display: grid;
		place-items: center;
		min-height: 80vh;
	}
	form {
		width: min(380px, 100%);
	}
	h1 {
		margin: 0;
	}
	p {
		margin: 0;
	}
</style>
