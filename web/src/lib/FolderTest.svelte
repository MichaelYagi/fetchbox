<script lang="ts">
	// "Test": can this folder be used? Nothing is created; the answer clears when the path changes.
	import { ApiError, post, type S } from './api.ts';

	let { path }: { path: string } = $props();
	let result = $state<{ for: string; ok: boolean; message: string } | null>(null);
	let busy = $state(false);
	const shown = $derived(result && result.for === path.trim() ? result : null);

	async function test() {
		const p = path.trim();
		busy = true;
		try {
			const r = await post<S['FolderTestOut']>('/storage/test-folder', { path: p });
			result = { for: p, ok: r.ok, message: r.message };
		} catch (e) {
			result = { for: p, ok: false, message: e instanceof ApiError ? e.message : String(e) };
		} finally {
			busy = false;
		}
	}
</script>

<div class="foldertest">
	<button type="button" class="small" onclick={test} disabled={busy || !path.trim()}>{busy ? 'Testing…' : 'Test'}</button>
	{#if shown}
		<span class="small" class:good={shown.ok} class:bad={!shown.ok} role="status">{shown.ok ? '✓' : '✗'} {shown.message}</span>
	{/if}
</div>

<style>
	.foldertest {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.foldertest span {
		overflow-wrap: anywhere;
	}
	.good {
		color: var(--ok);
	}
	.bad {
		color: var(--error);
	}
</style>
