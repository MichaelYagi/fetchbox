<script lang="ts">
	import Path from './Path.svelte';
	import { ApiError, get, patch, post, type S } from './api.ts';

	// The music library folder: where albums go (Artist/Album (Year)/01 - Title.flac),
	// how they get there, and importing what's already in it.
	let { dest, onChanged }: { dest: S['DestinationOut'] | undefined; onChanged: () => void } = $props();
	let editing = $state(false);
	let folder = $state('');
	let busy = $state('');
	let error = $state('');
	let message = $state('');
	let moving = $state<number | null>(null);

	const MODES: [string, string][] = [
		['hardlink_or_copy', 'Hardlink when possible, else copy (recommended)'],
		['hardlink_only', 'Hardlink only (fail rather than use double space)'],
		['copy', 'Always copy (tags written right away)'],
		['move', 'Move (after seeding is done)']
	];

	async function act(name: string, fn: () => Promise<unknown>, done = '') {
		busy = name;
		error = message = '';
		try {
			await fn();
			if (done) message = done;
			onChanged();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}
	const save = (e: SubmitEvent) => {
		e.preventDefault();
		act('folder', async () => {
			await patch(`/destinations/${dest!.id}`, { path: folder.trim() }, dest!.etag);
			editing = false;
		}, "Folder saved. New albums go there; albums already in the old folder stay (they aren't moved).");
	};
	const setMode = (mode: string) =>
		act('mode', () => patch(`/destinations/${dest!.id}`, { link_mode: mode }, dest!.etag), 'Saved.');
	const moveTo = (path: string) =>
		act('move', async () => {
			const job = await post<S['JobOut']>(`/destinations/${dest!.id}/move`, { path });
			moving = 0;
			for (;;) {
				const j = await get<S['JobOut']>(`/jobs/${job.id}`);
				moving = j.progress;
				if (j.status === 'succeeded') break;
				if (j.status === 'failed') throw new Error(j.error ?? 'the move failed');
				await new Promise((r) => setTimeout(r, 700));
			}
			moving = null;
		}, 'Moved. New albums are now hardlinked: one copy of each file.');
</script>

<div class="card stack">
	<div class="spread">
		<h3>Music</h3>
		{#if dest}<span class="badge {dest.status === 'online' ? 'ok' : 'warn'}" title={dest.status_message ?? ''}>{dest.status}</span>{/if}
	</div>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}
	{#if !dest}
		<p class="muted small">No music folder yet: it's created when Fetchbox starts.</p>
	{:else}
		<div class="field">
			<div class="label">Folder</div>
			{#if !editing}
				<div class="row">
					<Path path={dest.path} />
					<button class="small" onclick={() => ((folder = dest.path), (editing = true))}>Change</button>
					<a class="button small" href="/library/import?dest={dest.id}">Import what's already here</a>
				</div>
				{#if dest.suggested_path}
					<div class="notice warn small stack">
						<div>Every album is stored twice: once where it downloads, and again here, because this folder is on a different drive.</div>
						<div>Fetchbox can move this folder, with the albums already in it, to:</div>
						<Path path={dest.suggested_path} />
						<div>
							<button class="primary small" disabled={!!busy} onclick={() => moveTo(dest!.suggested_path!)}>
								{moving !== null ? `Moving… ${Math.round(moving * 100)}%` : 'Move the library next to the downloads'}
							</button>
						</div>
					</div>
				{:else if dest.status_message && dest.status_message !== 'ok'}<div class="muted small">{dest.status_message}</div>{/if}
			{:else}
				<form class="row" onsubmit={save}>
					<input class="grow" bind:value={folder} required aria-label="Music folder" />
					<button class="primary" disabled={!!busy}>Save</button>
					<button type="button" onclick={() => (editing = false)}>Cancel</button>
				</form>
			{/if}
		</div>
		<label class="field">
			<span class="label">How finished downloads get here</span>
			<select value={dest.link_mode} onchange={(e) => setMode(e.currentTarget.value)} disabled={!!busy}>
				{#each MODES as [v, l] (v)}<option value={v}>{l}</option>{/each}
			</select>
		</label>
		<p class="muted small">
			Albums are named <span class="mono">Artist/Album (Year)/01 - Title.flac</span> (a CD folder per disc) and tagged
			from MusicBrainz. A hardlinked album is the seeding torrent's own file, so its tags are written once seeding ends.
		</p>
	{/if}
</div>

<style>
	.grow {
		flex: 1;
		min-width: 12rem;
	}
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.label {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.mono {
		font-family: var(--mono, ui-monospace, monospace);
	}
</style>
