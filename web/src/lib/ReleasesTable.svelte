<script lang="ts">
	import { bytes, type S } from './api.ts';

	// Results of an interactive search (movie, episode or season), with a Grab button.
	let {
		result,
		busy = false,
		onGrab
	}: {
		result: S['InteractiveOut'];
		busy?: boolean;
		onGrab: (r: S['ReleaseOut']) => void;
	} = $props();

	function covers(r: S['ReleaseOut']): string {
		const eps = (r.episodes ?? []) as number[][];
		if (eps.length === 0) return '';
		const pad = (n: number) => String(n).padStart(2, '0');
		const [s0, e0] = eps[0];
		const last = eps[eps.length - 1];
		const range = eps.length === 1 ? `S${pad(s0)}E${pad(e0)}` : `S${pad(s0)}E${pad(e0)}–E${pad(last[1])}`;
		return r.season_pack ? `${range} · season pack` : range;
	}
</script>

<p class="muted small">
	{result.releases.length} result{result.releases.length === 1 ? '' : 's'} from {result.indexers_searched}
	indexer{result.indexers_searched === 1 ? '' : 's'}; the best are first. Rejected ones say why.
</p>
{#each result.failures as f}<p class="notice warn">{f.indexer_name}: {f.error}</p>{/each}
{#if result.releases.length === 0}
	<p class="empty card">
		No releases found. {result.indexers_searched === 0 ? 'Add an indexer in Settings.' : ''}
	</p>
{:else}
	<div class="card table-wrap">
		<table>
			<thead><tr><th>Release</th><th>Quality</th><th>Size</th><th title="Seeders; for Usenet, how often it was downloaded">Seeds</th><th>Indexer</th><th></th></tr></thead>
			<tbody>
				{#each result.releases as r (r.release_id)}
					<tr class:rejected={!r.accepted}>
						<td>
							<div class="name">{#if r.candidate.protocol === 'usenet'}<span class="badge usenet" title="An NZB, for SABnzbd or NZBGet">Usenet</span> {/if}{r.candidate.title}</div>
							{#if covers(r)}<div class="small muted">{covers(r)}</div>{/if}
							{#each r.rejections as rej}<div class="small reason">{rej.message}</div>{/each}
						</td>
						<td>{r.quality}{r.format_score ? ` (${r.format_score > 0 ? '+' : ''}${r.format_score})` : ''}</td>
						<td>{bytes(r.candidate.size)}</td>
						<td>{r.candidate.protocol === 'usenet' ? (r.candidate.grabs != null ? `${r.candidate.grabs} grabs` : '—') : (r.candidate.seeders ?? '—')}</td>
						<td class="muted">{r.candidate.source_name}</td>
						<td>
							<button
								class={r.accepted ? 'primary' : ''}
								onclick={() => onGrab(r)}
								disabled={busy}
								title={r.accepted ? '' : 'Rejected: grab anyway'}>Grab</button
							>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	.name {
		word-break: break-all;
	}
	.reason {
		color: var(--warn);
	}
	tr.rejected .name {
		color: var(--muted);
	}
	p {
		margin: 0.25rem 0 0.5rem;
	}
	.usenet {
		margin-right: 0.25rem;
		vertical-align: 1px;
	}
</style>
