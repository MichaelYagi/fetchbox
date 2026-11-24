<script lang="ts">
	import { applyTheme, currentTheme, THEMES, type ThemeId } from './themes.ts';

	// The colour theme, as swatches in the top bar.
	let theme = $state<ThemeId>(currentTheme());
	let open = $state(false);
	let root = $state<HTMLElement>();
	const now = $derived(THEMES.find((t) => t.id === theme) ?? THEMES[0]);

	function pick(id: ThemeId) {
		theme = id;
		applyTheme(id);
		open = false;
	}
	$effect(() => {
		if (!open) return;
		const away = (e: PointerEvent) => {
			if (root && !root.contains(e.target as Node)) open = false;
		};
		const esc = (e: KeyboardEvent) => {
			if (e.key === 'Escape') open = false;
		};
		document.addEventListener('pointerdown', away);
		document.addEventListener('keydown', esc);
		return () => {
			document.removeEventListener('pointerdown', away);
			document.removeEventListener('keydown', esc);
		};
	});
</script>

{#snippet swatch(colors: readonly string[])}
	<span class="swatch" aria-hidden="true">{#each colors as c (c)}<span style="background: {c}"></span>{/each}</span>
{/snippet}

<div class="picker" bind:this={root}>
	<button
		type="button"
		class="small trigger"
		aria-haspopup="menu"
		aria-expanded={open}
		title="Theme: {now.name}"
		aria-label="Theme: {now.name}"
		onclick={() => (open = !open)}>{@render swatch(now.swatch)}</button
	>
	{#if open}
		<div class="menu" role="menu" aria-label="Theme">
			{#each THEMES as t (t.id)}
				<button type="button" role="menuitemradio" aria-checked={t.id === theme} class:on={t.id === theme} onclick={() => pick(t.id)}>
					{@render swatch(t.swatch)}
					<span>{t.name}</span>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.picker {
		position: relative;
	}
	.trigger {
		display: inline-flex;
		align-items: center;
		padding: 0.35rem 0.45rem;
	}
	.swatch {
		display: inline-flex;
		width: 24px;
		height: 16px;
		border-radius: 4px;
		overflow: hidden;
		border: 1px solid var(--border);
		flex-shrink: 0;
	}
	.swatch span {
		flex: 1;
	}
	.menu {
		position: absolute;
		top: calc(100% + 6px);
		right: 0;
		z-index: 20;
		min-width: 10rem;
		padding: 0.3rem;
		display: grid;
		gap: 0.1rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		box-shadow: 0 6px 20px rgb(0 0 0 / 0.25);
	}
	.menu button {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		width: 100%;
		border: 0;
		background: none;
		text-align: left;
		padding: 0.4rem 0.55rem;
		font-size: 0.9rem;
	}
	.menu button:hover {
		background: var(--surface-2);
	}
	.menu button.on {
		color: var(--accent);
		font-weight: 600;
	}
</style>
