<script lang="ts">
	import { ApiError, post, type S } from './api.ts';

	// Settings → Delete all data: the data goes, the settings stay, a backup comes first.
	let { ondone }: { ondone?: () => void } = $props();
	const PHRASE = 'delete all data';
	let deleteFiles = $state(false);
	let typed = $state('');
	let busy = $state(false);
	let error = $state('');
	let result = $state<S['ResetOut'] | null>(null);
	const ready = $derived(typed.trim().toLowerCase() === PHRASE);

	async function run(e: SubmitEvent) {
		e.preventDefault();
		if (!ready) return;
		const what = deleteFiles
			? 'all data AND the media files in your library folders'
			: 'all data (your media files stay)';
		if (!confirm(`Delete ${what}?\n\nSettings are kept, and a backup is made first.`)) return;
		busy = true;
		error = '';
		result = null;
		try {
			result = await post<S['ResetOut']>('/system/reset-data', { confirm: PHRASE, delete_files: deleteFiles });
			typed = '';
			deleteFiles = false;
			ondone?.();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<div class="card zone">
	<p>
		Deletes your movies, shows, artists and albums, the wanted list, history, the queue, the blocklist and the
		activity log. Downloads in progress are stopped and their unfinished data deleted.
	</p>
	<p>
		<strong>Kept:</strong> everything on this page: indexers, download clients, library folders, quality profiles,
		naming, notifications, media servers, your login and API keys. A backup is made first, so you can undo this from
		<a href="#backups">Backups</a>.
	</p>
	<form onsubmit={run}>
		<label class="check">
			<input type="checkbox" bind:checked={deleteFiles} />
			<span>
				Also delete the media files in my library folders
				<span class="muted small">(into each folder's recycle folder, if it has one turned on)</span>
			</span>
		</label>
		{#if !deleteFiles}
			<p class="muted small">
				Your media files stay where they are. To bring them back in, use <em>Import what's already here</em> on each
				folder under <a href="#library">Library</a>.
			</p>
		{/if}
		<div class="row">
			<label class="grow">
				Type <code>{PHRASE}</code> to confirm
				<input bind:value={typed} autocomplete="off" spellcheck="false" placeholder={PHRASE} />
			</label>
			<button class="danger solid" disabled={!ready || busy}>{busy ? 'Deleting…' : 'Delete all data'}</button>
		</div>
	</form>
	{#if error}<p class="err">{error}</p>{/if}
	{#if result}
		<p class="ok">
			All data deleted. {result.downloads_stopped} download{result.downloads_stopped === 1 ? '' : 's'} stopped{#if result.files_deleted},
				{result.files_deleted} media file{result.files_deleted === 1 ? '' : 's'} deleted{/if}. Backup:
			<code>{result.backup}</code>.
		</p>
		{#if result.not_stopped.length}
			<p class="err small">Couldn't stop (is the client running?): {result.not_stopped.join(', ')}</p>
		{/if}
		{#if result.not_deleted.length}
			<p class="err small">Couldn't delete: {result.not_deleted.join(', ')}</p>
		{/if}
	{/if}
</div>

<style>
	.zone {
		border: 1px solid var(--error);
		background: var(--error-bg);
	}
	.zone p {
		margin: 0 0 0.6rem;
	}
	.check {
		display: flex;
		gap: 0.5rem;
		align-items: flex-start;
		margin: 0.4rem 0;
	}
	.grow {
		flex: 1;
		min-width: 14rem;
	}
	.row {
		align-items: end;
	}
	button.solid {
		background: var(--error);
		border-color: var(--error);
		color: #fff;
	}
	button.solid:disabled {
		opacity: 0.5;
	}
	.ok {
		color: var(--ok);
		margin-top: 0.6rem;
	}
	.err {
		color: var(--error);
	}
</style>
