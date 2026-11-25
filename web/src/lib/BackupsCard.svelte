<script lang="ts">
	import { ask, askText } from '#lib/dialogs.svelte.ts';
	import { ApiError, bytes, del, get, post, upload, type S } from './api.ts';

	// Backups of the database and settings: made weekly and before updates, or now.
	type Backup = S['BackupOut'];
	let backups = $state<Backup[]>([]);
	let password = $state('');
	let withKey = $state(false);
	let busy = $state('');
	let error = $state('');
	let message = $state('');

	const REASONS: Record<string, string> = {
		manual: 'Made by hand',
		scheduled: 'Weekly',
		'pre-migration': 'Before an update',
		'before-restore': 'Before a restore',
		'before-reset': 'Before deleting all data',
		uploaded: 'Uploaded'
	};

	async function load() {
		try {
			backups = await get<Backup[]>('/backups');
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
	$effect(() => {
		load();
	});

	async function act(name: string, fn: () => Promise<unknown>, done = '') {
		busy = name;
		error = message = '';
		try {
			await fn();
			if (done) message = done;
			await load();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = '';
		}
	}

	const make = () =>
		act('make', () => post('/backups', withKey ? { password } : {}), 'Backup made.').then(() => {
			password = '';
			withKey = false;
		});
	const remove = async (b: Backup) => (await ask(`Delete the backup from ${when(b.created_at)}?`, 'Delete', true)) && act(`rm-${b.id}`, () => del(`/backups/${b.id}`));
	async function restore(b: Backup) {
		if (!(await ask(`Restore the backup from ${when(b.created_at)}?\n\nFetchbox restarts, and everything since then is replaced (your current data is kept as a "Before a restore" backup).`, 'Restore', true))) return;
		const pw = b.includes_secrets_key ? await askText("The backup's password (it holds the secrets key):", { ok: 'Restore', password: true }) : null;
		if (b.includes_secrets_key && pw === null) return;
		act(`restore-${b.id}`, async () => {
			const r = await post<S['RestoreOut']>(`/backups/${b.id}/restore`, pw ? { password: pw } : {});
			message = r.restarting
				? 'Restoring: Fetchbox is restarting. Reload this page in a moment.'
				: 'Staged: restart Fetchbox to finish restoring.';
		});
	}
	const onFile = (e: Event) => {
		const f = (e.currentTarget as HTMLInputElement).files?.[0];
		if (f) act('upload', () => upload('/backups/upload', f), `${f.name} added.`);
		(e.currentTarget as HTMLInputElement).value = '';
	};
	const when = (iso: string) => new Date(iso).toLocaleString();
</script>

<div class="card stack">
	<p class="muted small">
		The database and settings, backed up weekly and before every update (downloads and your library files aren't included).
		Backups are kept in the data folder's <code>backups</code>; download one to keep it elsewhere.
	</p>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}
	<div class="row">
		<button class="primary" onclick={make} disabled={!!busy || (withKey && password.length < 8)}>{busy === 'make' ? 'Backing up…' : 'Back up now'}</button>
		<label class="inline"><input type="checkbox" bind:checked={withKey} /> Include the secrets key (to restore on another computer)</label>
		{#if withKey}<input type="password" placeholder="Password for it (8+ characters)" bind:value={password} autocomplete="new-password" />{/if}
	</div>
	{#if backups.length}
		<div class="table-wrap">
			<table>
				<tbody>
					{#each backups as b (b.id)}
						<tr>
							<td>{when(b.created_at)}</td>
							<td class="muted small">{REASONS[b.reason] ?? b.reason}{b.includes_secrets_key ? ' · with key' : ''}</td>
							<td class="muted small">{bytes(b.size)} · v{b.app_version}</td>
							<td>
								<div class="row end">
									<a class="button small" href="/api/v1/backups/{b.id}" download>Download</a>
									<button class="small" onclick={() => restore(b)} disabled={!!busy}>Restore</button>
									<button class="small danger" onclick={() => remove(b)} disabled={!!busy}>Delete</button>
								</div>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="muted small">No backups yet.</p>
	{/if}
	<label class="small muted">Restore from a file: <input type="file" accept=".zip" onchange={onFile} disabled={!!busy} /></label>
</div>

<style>
	.inline {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}
	.end {
		justify-content: flex-end;
		flex-wrap: wrap;
	}
</style>
