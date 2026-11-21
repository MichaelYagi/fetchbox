<script lang="ts">
	import { STATE_LABELS } from './api.ts';

	let { state }: { state: string } = $props();
	const tone: Record<string, string> = {
		acquired: 'ok',
		downloading: 'info',
		grabbed: 'info',
		importing: 'info',
		searching: 'info',
		missing: 'warn'
	};
</script>

<span class="badge {tone[state] ?? ''}">{STATE_LABELS[state] ?? state}</span>
