// Who is signed in, shared by the layout and pages.
import type { S } from './api.ts';

export const session = $state<{
	me: S['Me'] | null;
	checked: boolean;
	health: S['HealthReport'] | null;
	host: S['HostInfo'] | null;
	version: string | null;
}>({
	me: null,
	checked: false,
	health: null,
	host: null,
	version: null
});

/** How Windows sees a path when Fetchbox runs in WSL (null otherwise). */
export function windowsPath(path: string | null | undefined): string | null {
	const distro = session.host?.wsl_distro;
	if (!path || !distro || !path.startsWith('/')) return null;
	const drive = path.match(/^\/mnt\/([a-z])(\/.*)?$/i);
	if (drive) return `${drive[1].toUpperCase()}:${(drive[2] ?? '/').replaceAll('/', '\\')}`;
	return `\\\\wsl.localhost\\${distro}${path.replaceAll('/', '\\')}`;
}
