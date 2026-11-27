<script lang="ts">
	import '#lib/app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { get, post, rate, SignedOut, type Download, type S } from '#lib/api.ts';
	import { session } from '#lib/session.svelte.ts';
	import ThemePicker from '#lib/ThemePicker.svelte';
	import Dialogs from '#lib/Dialogs.svelte';

	let { children } = $props();

	const nav = [
		{ href: '/', label: 'Library' },
		{ href: '/add', label: 'Add' },
		{ href: '/calendar', label: 'Calendar' },
		{ href: '/queue', label: 'Queue' },
		{ href: '/activity', label: 'Activity' },
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

	async function loadHost() {
		try {
			const status = await get<S['StatusInfo']>('/system/status');
			session.host = status.host ?? null;
			session.version = status.version;
		} catch {
			session.host = null;
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

	// Downloads at a glance: running / queued counts, overall progress, speed.
	let downloads = $state<Download[]>([]);
	let attention = $state(0);
	async function loadDownloads() {
		if (!session.me) return;
		try {
			const [d, imports] = await Promise.all([
				get<Download[]>('/downloads'),
				get<S['ImportOut'][]>('/imports')
			]);
			downloads = d;
			attention = imports.filter((i) => i.status === 'needs_attention').length;
		} catch {
			/* the queue page shows errors; the bar just keeps its last state */
		}
	}
	$effect(() => {
		if (!session.me) return;
		loadDownloads();
		const t = setInterval(loadDownloads, 3000);
		return () => clearInterval(t);
	});
	const inFlight = $derived(downloads.filter((d) => ['queued', 'downloading', 'paused'].includes(d.status)));
	const running = $derived(inFlight.filter((d) => d.status === 'downloading'));
	const speed = $derived(inFlight.reduce((n, d) => n + (d.live?.download_rate ?? 0), 0));
	const overall = $derived.by(() => {
		const sized = inFlight.filter((d) => d.size);
		const total = sized.reduce((n, d) => n + (d.size ?? 0), 0);
		return total ? sized.reduce((n, d) => n + (d.size ?? 0) * d.progress, 0) / total : 0;
	});
	const summary = $derived(
		inFlight.length === 0
			? 'No downloads'
			: [
					running.length ? `${running.length} downloading` : null,
					inFlight.length - running.length ? `${inFlight.length - running.length} waiting` : null
				]
					.filter(Boolean)
					.join(', ')
	);

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
		if (!session.host) loadHost();
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
			<a class="brand" href="/" aria-label="Fetchbox">
				<img src="/favicon.svg" alt="" width="24" height="24" />
				<span class="brand-name">Fetchbox</span>
			</a>
			<nav>
				{#each nav as n (n.href)}
					<a href={n.href} class:active={active(n.href)}>{n.label}</a>
				{/each}
			</nav>
			<div class="row right">
				<a
					class="dl"
					class:idle={inFlight.length === 0}
					href="/queue"
					title={inFlight.map((d) => `${d.name}: ${Math.floor(d.progress * 100)}%`).join('\n') || 'Nothing downloading'}
				>
					<span class="arrow" aria-hidden="true">↓</span>
					<span class="label">{summary}</span>
					{#if inFlight.length}<span class="count">{inFlight.length}</span>{/if}
					{#if inFlight.length}
						<span class="dlbar" aria-label="Overall progress"><span style="width: {Math.round(overall * 100)}%"></span></span>
						<span class="muted">{Math.floor(overall * 100)}%{speed ? ` · ${rate(speed)}` : ''}</span>
					{/if}
					{#if attention}<span class="badge warn">{attention} to import</span>{/if}
				</a>
				{#if health}
					<a
						class="badge {health.status === 'ok' ? 'ok' : health.status === 'warning' ? 'warn' : 'error'}"
						href="/settings/general#health"
						title={problems.map((p) => `${p.name}: ${p.message}`).join('\n') || 'All checks pass'}
					>
						{health.status === 'ok' ? 'Healthy' : `${problems.length} issue${problems.length === 1 ? '' : 's'}`}
					</a>
				{/if}
				<ThemePicker />
				<button class="small" onclick={signOut}>Sign out</button>
			</div>
		</div>
	</header>
	{@render children()}
	{#if session.host}
		<footer>
			Fetchbox{session.version ? ` ${session.version}` : ''} is running on <strong>{session.host.name}</strong>
			({session.host.wsl_distro ? `Linux, ${session.host.wsl_distro} in WSL` : session.host.os}): every
			folder shown here is on that machine.
			<a href="/settings/general#computer">Details</a>
		</footer>
	{/if}
{:else if open.includes(page.url.pathname)}
	{@render children()}
{:else if session.checked}
	<main><p class="muted">Signing in…</p></main>
{/if}
<Dialogs />

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
	footer {
		max-width: 1200px;
		margin: 0 auto;
		padding: 1rem 1rem 2rem;
		font-size: 0.8rem;
		color: var(--muted);
		border-top: 1px solid var(--border);
	}
	.dl {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		font-size: 0.85rem;
		color: var(--text);
		padding: 0.25rem 0.6rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		white-space: nowrap;
	}
	.dl:hover {
		text-decoration: none;
		border-color: var(--accent);
	}
	.dl.idle {
		color: var(--muted);
	}
	.arrow {
		color: var(--accent);
		font-weight: 700;
	}
	.dl.idle .arrow {
		color: var(--muted);
	}
	.dlbar {
		width: 56px;
		height: 5px;
		border-radius: 999px;
		background: var(--surface-2);
		overflow: hidden;
		display: inline-block;
	}
	.dlbar > span {
		display: block;
		height: 100%;
		background: var(--accent);
		transition: width 0.4s;
	}
	@media (max-width: 640px) {
		.dlbar,
		.dl .muted {
			display: none;
		}
	}
	.right {
		margin-left: auto;
	}
	.dl .count {
		display: none;
	}
	/* Phones: the status beside "Fetchbox" on the first line, the menu under them; the
	   downloads link is just its arrow (and how many, while any are going) */
	@media (max-width: 640px) {
		.bar {
			gap: 0.5rem 1rem;
		}
		.brand {
			order: 0;
		}
		.right {
			order: 1;
			gap: 0.4rem;
		}
		nav {
			order: 2;
			flex-basis: 100%;
			flex-wrap: nowrap; /* one line, scrolled sideways if it doesn't fit */
			overflow-x: auto;
			scrollbar-width: none;
			gap: 0;
		}
		nav a {
			padding: 0.35rem 0.45rem;
			font-size: 0.95rem;
			white-space: nowrap;
		}
		.dl .label {
			display: none;
		}
		.dl .count {
			display: inline;
		}
	}
	/* Narrow phones: the icon without the name, and a tighter menu, so each is one line */
	@media (max-width: 400px) {
		.brand-name {
			display: none;
		}
		nav {
			gap: 0;
		}
		nav a {
			padding: 0.35rem 0.4rem;
			font-size: 0.95rem;
		}
	}
</style>
