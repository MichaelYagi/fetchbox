<script lang="ts">
	import '#lib/app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { get, post, SignedOut, type S } from '#lib/api.ts';
	import { session } from '#lib/session.svelte.ts';

	let { children } = $props();

	const nav = [
		{ href: '/', label: 'Library' },
		{ href: '/add', label: 'Add movie' },
		{ href: '/queue', label: 'Queue' },
		{ href: '/settings', label: 'Settings' }
	];
	const open = ['/login'];

	async function check() {
		try {
			session.me = await get<S['Me']>('/auth/me');
		} catch (e) {
			session.me = null;
			if (e instanceof SignedOut && !open.includes(page.url.pathname)) {
				await goto(`/login?next=${encodeURIComponent(page.url.pathname)}`);
			}
		} finally {
			session.checked = true;
		}
	}

	async function loadHealth() {
		if (!session.me) return;
		try {
			session.health = await get<S['HealthReport']>('/system/health');
		} catch {
			session.health = null;
		}
	}

	async function signOut() {
		await post('/auth/logout');
		session.me = null;
		await goto('/login');
	}

	$effect(() => {
		void page.url.pathname;
		if (!session.me) check();
	});

	$effect(() => {
		void page.url.pathname; // fresh on every page change, and once a minute
		if (!session.me) return;
		loadHealth();
		const t = setInterval(loadHealth, 60_000);
		return () => clearInterval(t);
	});

	const active = (href: string) =>
		href === '/' ? page.url.pathname === '/' || page.url.pathname.startsWith('/title') : page.url.pathname.startsWith(href);
	const health = $derived(session.health);
	const problems = $derived(health?.checks.filter((c) => c.status !== 'ok') ?? []);
</script>

{#if session.me && !open.includes(page.url.pathname)}
	<header>
		<div class="bar">
			<a class="brand" href="/">
				<img src="/favicon.svg" alt="" width="24" height="24" />
				Fetchbox
			</a>
			<nav>
				{#each nav as n (n.href)}
					<a href={n.href} class:active={active(n.href)}>{n.label}</a>
				{/each}
			</nav>
			<div class="row right">
				{#if health}
					<a
						class="badge {health.status === 'ok' ? 'ok' : health.status === 'warning' ? 'warn' : 'error'}"
						href="/settings#health"
						title={problems.map((p) => `${p.name}: ${p.message}`).join('\n') || 'All checks pass'}
					>
						{health.status === 'ok' ? 'Healthy' : `${problems.length} issue${problems.length === 1 ? '' : 's'}`}
					</a>
				{/if}
				<button class="small" onclick={signOut}>Sign out</button>
			</div>
		</div>
	</header>
	{@render children()}
{:else if open.includes(page.url.pathname)}
	{@render children()}
{:else if session.checked}
	<main><p class="muted">Signing in…</p></main>
{/if}

<style>
	header {
		background: var(--surface);
		border-bottom: 1px solid var(--border);
		position: sticky;
		top: 0;
		z-index: 10;
	}
	.bar {
		max-width: 1200px;
		margin: 0 auto;
		padding: 0.6rem 1rem;
		display: flex;
		align-items: center;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.brand {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-weight: 700;
		color: var(--text);
		font-size: 1.05rem;
	}
	.brand:hover {
		text-decoration: none;
	}
	nav {
		display: flex;
		gap: 0.25rem;
		flex-wrap: wrap;
	}
	nav a {
		color: var(--muted);
		padding: 0.35rem 0.7rem;
		border-radius: 8px;
	}
	nav a:hover {
		background: var(--surface-2);
		text-decoration: none;
	}
	nav a.active {
		color: var(--text);
		background: var(--surface-2);
		font-weight: 600;
	}
	.right {
		margin-left: auto;
	}
</style>
