<script lang="ts">
	import { answer, dialogs } from '#lib/dialogs.svelte.ts';

	let el = $state<HTMLDialogElement | null>(null);
	let value = $state('');
	let field = $state<HTMLInputElement | null>(null);
	let okButton = $state<HTMLButtonElement | null>(null);
	const current = $derived(dialogs.queue[0] ?? null);

	$effect(() => {
		const a = current;
		if (!el) return;
		if (a && !el.open) {
			value = '';
			el.showModal();
			// the answer you'd most likely give is focused, except for a destructive one
			queueMicrotask(() => (a.input ? field : a.danger ? null : okButton)?.focus());
		} else if (!a && el.open) el.close();
	});

	function done(v: string | null) {
		el?.close();
		answer(v);
	}
</script>

<dialog
	bind:this={el}
	class="ask"
	aria-describedby="ask-message"
	oncancel={(e) => {
		e.preventDefault();
		done(null);
	}}
>
	{#if current}
		<form
			method="dialog"
			class="stack"
			onsubmit={(e) => {
				e.preventDefault();
				done(current.input ? value : '');
			}}
		>
			<p id="ask-message" class="message">{current.message}</p>
			{#if current.input}
				<input
					bind:this={field}
					bind:value
					type={current.input}
					autocomplete={current.input === 'password' ? 'off' : undefined}
					aria-labelledby="ask-message"
				/>
			{/if}
			<div class="actions">
				<button type="button" onclick={() => done(null)}>Cancel</button>
				<button bind:this={okButton} class={current.danger ? 'danger' : 'primary'}>{current.ok}</button>
			</div>
		</form>
	{/if}
</dialog>

<style>
	.message {
		margin: 0;
		white-space: pre-line;
		overflow-wrap: anywhere;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
</style>
