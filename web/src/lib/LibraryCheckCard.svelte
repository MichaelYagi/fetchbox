<script lang="ts">
	import { ApiError, bytes, get, post, type S } from './api.ts';

	// Check every library folder: files gone from disk, duplicates, files Fetchbox
	// doesn't know about. Only reads; "Search again" forgets missing files.
	type Report = {
		files: number;
		not_checked: number;
		missing: { file_id: string; path: string; title: string | null; title_id: string | null }[];
		duplicates: { file_id: string; path: string; title: string | null; size: number }[][];
		unknown: { path: string; folder: string }[];
		unknown_count: number;
	};
	let report = $state<Report | null>(null);
	let progress = $state<number | null>(null);
	let busy = $state(false);
	let error = $state('');
	let message = $state('');

	async function run() {
		busy = true;
		error = message = '';
		try {
			const job = await post<S['JobOut']>('/library/check');
			for (;;) {
				const j = await get<S['JobOut']>(`/jobs/${job.id}`);
				progress = j.progress;
				if (j.status === 'succeeded') {
					report = j.result as unknown as Report;
					break;
				}
				if (j.status === 'failed') throw new Error(j.error ?? 'the check failed');
				await new Promise((r) => setTimeout(r, 700));
			}
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			busy = false;
			progress = null;
		}
	}

	async function forget() {
		if (!report || !confirm(`Forget ${report.missing.length} missing file(s)? Fetchbox will search for them again.`)) return;
		try {
			const r = await post<{ forgotten: number }>('/library/check/forget', { file_ids: report.missing.map((m) => m.file_id) });
			message = `${r.forgotten} forgotten; they'll be searched for again.`;
			await run();
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		}
	}
</script>

<div class="card stack check">
	<div class="spread">
		<div>
			<h3>Check library</h3>
			<div class="muted small">Finds files gone from disk, duplicates, and video files Fetchbox doesn't know about. Changes nothing.</div>
		</div>
		<button onclick={run} disabled={busy}>{busy ? `Checking…${progress != null ? ` ${Math.round(progress * 100)}%` : ''}` : 'Check now'}</button>
	</div>
	{#if error}<p class="notice error">{error}</p>{/if}
	{#if message}<p class="notice">{message}</p>{/if}
	{#if report}
		<p class="small">
			{report.files} file{report.files === 1 ? '' : 's'} checked{report.not_checked ? `, ${report.not_checked} in offline folders not checked` : ''}.
			{#if !report.missing.length && !report.duplicates.length && !report.unknown_count}<strong>All good.</strong>{/if}
		</p>
		{#if report.missing.length}
			<div class="block">
				<div class="spread"><strong>Missing from disk ({report.missing.length})</strong>
					<button class="small" onclick={forget}>Search for them again</button></div>
				{#each report.missing as m (m.file_id)}<div class="small line">{#if m.title_id}<a href="/title/{m.title_id}">{m.title}</a>: {/if}<span class="mono">{m.path}</span></div>{/each}
			</div>
		{/if}
		{#if report.duplicates.length}
			<div class="block">
				<strong>Duplicates ({report.duplicates.length})</strong>
				<div class="muted small">The same file more than once. Nothing is deleted; remove the copy you don't want yourself.</div>
				{#each report.duplicates as g, i (i)}
					<div class="group">
						{#each g as f (f.file_id)}<div class="small line">{f.title ?? ''} · {bytes(f.size)} · <span class="mono">{f.path}</span></div>{/each}
					</div>
				{/each}
			</div>
		{/if}
		{#if report.unknown_count}
			<div class="block">
				<strong>Not in Fetchbox ({report.unknown_count})</strong>
				<div class="muted small">Video files in your library folders that Fetchbox has no record of. Use “Import what's already here” on the folder to add them.</div>
				{#each report.unknown.slice(0, 30) as u, i (i)}<div class="small line mono">{u.path}</div>{/each}
				{#if report.unknown_count > 30}<div class="muted small">…and {report.unknown_count - 30} more</div>{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	h3 {
		margin: 0;
	}
	.block {
		display: grid;
		gap: 0.25rem;
		border-top: 1px solid var(--border);
		padding-top: 0.5rem;
	}
	.group {
		border-left: 3px solid var(--warn, #d97706);
		padding-left: 0.5rem;
		margin-top: 0.25rem;
	}
	.line {
		overflow-wrap: anywhere;
	}
	.mono {
		font-family: var(--mono, ui-monospace, monospace);
	}
	.check :global(.spread) {
		flex-wrap: wrap;
		gap: 0.5rem;
	}
</style>
