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
