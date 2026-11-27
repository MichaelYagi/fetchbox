<script lang="ts">
	// A path as the system Fetchbox runs on sees it (under WSL, the Linux path): only that.
	let { path }: { path: string | null | undefined } = $props();
</script>

{#if path}<code>{path}</code>{/if}

<style>
	code {
		word-break: break-all;
	}
</style>
