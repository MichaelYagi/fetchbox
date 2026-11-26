<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, get, post, type S } from './api.ts';

	// Adding a title of a plugin's media kind (a comic series): the kind's own search, the
	// same clickable results and an Add dialog asking which of its items to want.
	let { kind }: { kind: S['MediaKindOut'] } = $props();

	type Hit = S['HitOut'];
	let q = $state('');
	let hits = $state<Hit[] | null>(null);
	let searched = $state('');
	let busy = $state(false);
	let error = $state('');
	let pending = $state<Hit | null>(null);
	let monitor = $state<'all' | 'future' | 'none'>('all');
	let adding = $state(false);
	let dialog = $state<HTMLDialogElement | null>(null);
	let seq = 0;
	const item = $derived(kind.levels[kind.levels.length - 1] ?? 'item');

	$effect(() => {
		void kind.id;
		hits = null;
		searched = '';
		error = '';
	});
	$effect(() => {
		if (pending && dialog && !dialog.open) dialog.showModal();
	});

	async function search(e?: SubmitEvent) {
		e?.preventDefault();
		const text = q.trim();
		if (!text) return;
		const mine = ++seq;
		busy = true;
		error = '';
		try {
			const got = await get<Hit[]>(`/plugins/kinds/${kind.id}/search`, { q: text });
			if (mine !== seq) return;
			hits = got;
			searched = text;
		} catch (err) {
			if (mine === seq) error = err instanceof ApiError ? err.message : String(err);
		} finally {
			if (mine === seq) busy = false;
		}
	}

	async function add(e: SubmitEvent) {
		e.preventDefault();
		if (!pending) return;
		adding = true;
		error = '';
		try {
			const t = await post<S['PluginTitleOut']>(`/plugins/kinds/${kind.id}/titles`, { ref: pending.ref, monitor });
			dialog?.close();
			goto(`/title/${t.id}`);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			adding = false;
		}
	}

	function closed() {
		pending = null;
		setTimeout(() => {
			const el = document.activeElement;
			if (el instanceof HTMLElement && el.classList.contains('pick')) el.blur();
		}, 0);
	}
	const pickKey = (e: KeyboardEvent, h: Hit) => {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			pending = h;
		}
	};
</script>

<form class="card stack" onsubmit={search}>
	<div class="row">
		<input class="grow" bind:value={q} placeholder="{kind.name}: search by name" aria-label="Search" />
		<button class="primary" disabled={!q.trim()}>{busy ? 'Searching…' : 'Search'}</button>
	</div>
	{#if error && !pending}<p class="notice error small">{error}</p>{/if}
</form>

{#if hits !== null}
	{#if hits.length === 0}
		<p class="empty">Nothing found for “{searched}”.</p>
	{:else}
		<div class="results">
			{#each hits as h (h.ref)}
				{#if h.title_id}
					<a class="card result pick" href="/title/{h.title_id}" aria-label="{h.name}: in your library">
						{#if h.poster_url}<img src={h.poster_url} alt="" loading="lazy" />{:else}<div class="noposter"></div>{/if}
						<div class="body">
							<div class="spread"><strong>{h.name} {h.year ? `(${h.year})` : ''}</strong><span class="owned small">In library</span></div>
							<p class="muted small overview">{h.overview}</p>
						</div>
					</a>
				{:else}
					<div class="card result pick" class:chosen={pending?.ref === h.ref} role="button" tabindex="0"
						aria-label="Add {h.name}" onclick={() => (pending = h)} onkeydown={(e) => pickKey(e, h)}>
						{#if h.poster_url}<img src={h.poster_url} alt="" loading="lazy" />{:else}<div class="noposter"></div>{/if}
						<div class="body">
							<strong>{h.name} {h.year ? `(${h.year})` : ''}</strong>
							<p class="muted small overview">{h.overview}</p>
						</div>
					</div>
				{/if}
			{/each}
		</div>
	{/if}
{/if}

<dialog bind:this={dialog} onclose={closed} oncancel={(e) => { e.preventDefault(); if (!adding) dialog?.close(); }} aria-labelledby="plugin-add-title">
	{#if pending}
		<form class="stack" onsubmit={add}>
			<h2 id="plugin-add-title">Add {pending.name}{pending.year ? ` (${pending.year})` : ''}</h2>
			<p class="muted small">{kind.name} · nothing is added or downloaded until you confirm</p>
			<label class="field">
				Download
				<select bind:value={monitor}>
					<option value="all">Every {item}</option>
					<option value="future">Only new {item}s</option>
					<option value="none">Nothing yet (choose later)</option>
				</select>
			</label>
			{#if error}<p class="notice error small">{error}</p>{/if}
			<div class="row end">
				<button type="button" onclick={() => dialog?.close()} disabled={adding}>Cancel</button>
				<button class="primary" disabled={adding}>{adding ? 'Adding…' : 'Add'}</button>
			</div>
		</form>
	{/if}
</dialog>

<style>
	.results {
		display: grid;
		gap: 0.75rem;
		margin-top: 1rem;
	}
	.result {
		display: flex;
		gap: 1rem;
		padding: 0.75rem;
		color: inherit;
		text-decoration: none;
		cursor: pointer;
	}
	.pick:hover,
	.pick:focus-visible,
	.pick.chosen {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 9%, var(--surface));
		box-shadow: 0 0 0 1px var(--accent);
		outline: none;
	}
	img,
	.noposter {
		width: 72px;
		aspect-ratio: 2 / 3;
		object-fit: cover;
		border-radius: 6px;
		background: var(--surface-2);
		flex: none;
	}
	.body {
		flex: 1;
		min-width: 0;
	}
	.owned {
		padding: 0.15rem 0.55rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--muted);
	}
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.end {
		justify-content: flex-end;
	}
</style>
