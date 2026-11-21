<script lang="ts">
	import { windowsPath } from './session.svelte.ts';

	// A path on the machine Fetchbox runs on; under WSL also how Windows reaches it.
	let { path }: { path: string | null | undefined } = $props();
	const win = $derived(windowsPath(path));
</script>

{#if path}
	<code>{path}</code>
	{#if win}<span class="win" title="The same folder, as Windows sees it">Windows: <code>{win}</code></span>{/if}
{/if}

<style>
	code {
		word-break: break-all;
	}
	.win {
		display: block;
		font-size: 0.8rem;
		color: var(--muted);
	}
</style>
