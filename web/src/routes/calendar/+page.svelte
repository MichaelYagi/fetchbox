<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, get, type S } from '#lib/api.ts';

	type Entry = S['CalendarEntry'];
	const DAY = 86_400_000;
	const iso = (d: Date) =>
		`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
	const today = iso(new Date());

	// ?month=2026-11 opens that month (and the arrows keep the URL in step)
	const m = /^(\d{4})-(\d{2})$/.exec(page.url.searchParams.get('month') ?? '');
	let month = $state(m ? new Date(+m[1], +m[2] - 1, 1) : new Date(new Date().getFullYear(), new Date().getMonth(), 1));
	let entries = $state<Entry[]>([]);
	let error = $state('');
	let loading = $state(false);

	// Monday before the 1st to the Sunday after the last day: whole weeks
	const days = $derived.by(() => {
		const first = new Date(month);
		const start = new Date(first.getTime() - ((first.getDay() + 6) % 7) * DAY);
		const last = new Date(first.getFullYear(), first.getMonth() + 1, 0);
		const end = new Date(last.getTime() + ((7 - last.getDay()) % 7) * DAY);
		const out: Date[] = [];
		for (let d = new Date(start); d <= end; d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)) out.push(d);
		return out;
	});
	const byDay = $derived.by(() => {
		const m = new Map<string, Entry[]>();
		for (const e of entries) m.set(e.date, [...(m.get(e.date) ?? []), e]);
		return m;
	});
	const label = $derived(month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }));

	$effect(() => {
		const from = iso(days[0]);
		const to = iso(days[days.length - 1]);
		loading = true;
		get<Entry[]>('/calendar', { start: from, end: to })
			.then((r) => {
				entries = r;
				error = '';
			})
			.catch((e) => (error = e instanceof ApiError ? e.message : String(e)))
			.finally(() => (loading = false));
	});

	function go(d: Date) {
		month = d;
		const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
		goto(`/calendar?month=${key}`, { replace: true, shallow: true });
	}
	const shift = (n: number) => go(new Date(month.getFullYear(), month.getMonth() + n, 1));
	const pad = (n: number | null | undefined) => String(n ?? 0).padStart(2, '0');
	const RELEASE: Record<string, string> = { cinema: 'in cinemas', digital: 'digital release', disc: 'on disc' };

	function what(e: Entry): string {
		return e.kind === 'episode' ? `S${pad(e.season)}E${pad(e.episode)}` : RELEASE[e.release ?? ''] ?? '';
	}
	function tone(e: Entry): string {
		if (!e.monitored) return 'off';
		if (e.state === 'acquired') return 'ok';
		if (['grabbed', 'downloading', 'importing', 'searching'].includes(e.state)) return 'busy';
		return e.date <= today ? 'missing' : 'upcoming';
	}
	const TONES: [string, string][] = [
		['ok', 'Downloaded'],
		['busy', 'Downloading'],
		['missing', 'Out, not downloaded yet'],
		['upcoming', 'Coming up'],
		['off', 'Not monitored']
	];
</script>

<main>
	<div class="spread head">
		<h1>Calendar</h1>
		<div class="row">
			<button onclick={() => shift(-1)} aria-label="Previous month">‹</button>
			<strong class="month">{label}</strong>
			<button onclick={() => shift(1)} aria-label="Next month">›</button>
			<button onclick={() => go(new Date(new Date().getFullYear(), new Date().getMonth(), 1))}>Today</button>
		</div>
	</div>
	{#if error}<p class="notice error">{error}</p>{/if}

	<div class="grid" class:loading aria-label="Month">
		{#each ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as d (d)}<div class="dow">{d}</div>{/each}
		{#each days as d (iso(d))}
			{@const key = iso(d)}
			{@const list = byDay.get(key) ?? []}
			<div class="day" class:other={d.getMonth() !== month.getMonth()} class:today={key === today} class:blank={list.length === 0}>
				<div class="num">
					<span class="wd">{d.toLocaleDateString(undefined, { weekday: 'short' })}</span>
					{d.getDate()}
					<span class="mo">{d.toLocaleDateString(undefined, { month: 'short' })}</span>
				</div>
				{#each list as e, i (i)}
					<a class="entry {tone(e)}" href="/title/{e.title_id}" title="{e.name} · {what(e)}{e.episode_name ? ` · ${e.episode_name}` : ''}">
						<span class="name">{e.name}</span>
						<span class="what">{what(e)}{#if e.episode_name}<span class="epname"> · {e.episode_name}</span>{/if}</span>
					</a>
				{/each}
			</div>
		{/each}
	</div>
	{#if !loading && entries.length === 0 && !error}
		<p class="empty">Nothing airs or comes out this month among your shows and movies.</p>
	{/if}

	<div class="legend row small muted">
		{#each TONES as [t, l] (t)}<span><span class="dot {t}"></span>{l}</span>{/each}
	</div>
</main>

<style>
	.head {
		flex-wrap: wrap;
		gap: 0.5rem;
	}
	.month {
		min-width: 9.5rem;
		text-align: center;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(7, minmax(0, 1fr));
		gap: 1px;
		background: var(--border);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
		margin-top: 1rem;
	}
	.grid.loading {
		opacity: 0.6;
	}
	.dow {
		background: var(--surface-2);
		padding: 0.35rem 0.5rem;
		font-size: 0.78rem;
		color: var(--muted);
		font-weight: 600;
	}
	.day {
		background: var(--surface);
		min-height: 6.5rem;
		padding: 0.3rem;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		min-width: 0;
	}
	.day.other {
		background: var(--surface-2);
	}
	.day.other .num {
		color: var(--muted);
	}
	.num {
		font-size: 0.8rem;
		font-weight: 600;
		padding: 0 0.2rem;
	}
	.today .num {
		color: var(--accent);
	}
	.wd,
	.mo,
	.epname {
		display: none;
	}
	.entry {
		display: flex;
		flex-direction: column;
		font-size: 0.74rem;
		line-height: 1.25;
		padding: 0.2rem 0.35rem;
		border-radius: 4px;
		border-left: 3px solid var(--muted);
		background: var(--surface-2);
		color: var(--text);
		min-width: 0;
	}
	.entry:hover {
		text-decoration: none;
		filter: brightness(0.96);
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
	}
	.what {
		color: var(--muted);
	}
	.ok {
		border-left-color: var(--ok, #16a34a);
	}
	.busy {
		border-left-color: var(--accent);
	}
	.missing {
		border-left-color: var(--warn, #d97706);
	}
	.upcoming {
		border-left-color: var(--muted);
	}
	.entry.off {
		opacity: 0.5;
	}
	.legend {
		margin-top: 0.75rem;
		gap: 1rem;
		flex-wrap: wrap;
	}
	.dot {
		display: inline-block;
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 2px;
		margin-right: 0.35rem;
		background: var(--muted);
	}
	.dot.ok {
		background: var(--ok, #16a34a);
	}
	.dot.busy {
		background: var(--accent);
	}
	.dot.missing {
		background: var(--warn, #d97706);
	}
	.dot.off {
		opacity: 0.4;
	}

	/* phones: a list of the days that have something */
	@media (max-width: 700px) {
		.grid {
			grid-template-columns: minmax(0, 1fr);
			background: none;
			border: none;
			gap: 0.5rem;
		}
		.dow,
		.day.blank {
			display: none;
		}
		.day {
			min-height: 0;
			border: 1px solid var(--border);
			border-radius: var(--radius);
			padding: 0.5rem;
		}
		.wd,
		.mo,
		.epname {
			display: inline;
		}
		.num {
			font-size: 0.9rem;
		}
		.entry {
			font-size: 0.85rem;
		}
	}
</style>
