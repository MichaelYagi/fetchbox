// Typed access to the Fetchbox API. Types come from the OpenAPI document
// (`npm run gen` regenerates src/lib/schema.d.ts).
import type { components } from './schema.d.ts';

export type S = components['schemas'];
export type Title = S['TitleOut'];
export type Wanted = S['WantedOut'];
export type Download = S['DownloadOut'];

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string,
		public slug: string | null = null,
		public fields: { field: string; message: string }[] = []
	) {
		super(message);
	}
}

/** Raised for 401 so the layout can send the user to sign in. */
export class SignedOut extends ApiError {}

function csrf(): string {
	const m = document.cookie.match(/(?:^|;\s*)fb_csrf=([^;]+)/);
	return m ? decodeURIComponent(m[1]) : '';
}

type Query = Record<string, string | number | boolean | null | undefined>;

export async function api<T>(
	method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
	path: string,
	opts: { body?: unknown; query?: Query; etag?: string } = {}
): Promise<T> {
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
	if (method !== 'GET') headers['X-CSRF-Token'] = csrf();
	if (opts.etag) headers['If-Match'] = opts.etag;
	const qs = new URLSearchParams();
	for (const [k, v] of Object.entries(opts.query ?? {})) {
		if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
	}
	const url = `/api/v1${path}${qs.size ? `?${qs}` : ''}`;
	const r = await fetch(url, {
		method,
		headers,
		credentials: 'same-origin',
		body: opts.body === undefined ? undefined : JSON.stringify(opts.body)
	});
	if (r.status === 204) return undefined as T;
	const text = await r.text();
	const data = text ? JSON.parse(text) : null;
	if (!r.ok) {
		const p = (data ?? {}) as Partial<S['Problem']> & { type?: string };
		const slug = p.type ? (p.type.split('/').pop() ?? null) : null;
		const fields = (p.errors ?? []).map((e) => ({ field: e.field, message: e.message }));
		const message =
			fields.length > 0
				? fields.map((f) => f.message).join('; ')
				: (p.detail ?? p.title ?? `HTTP ${r.status}`);
		const E = r.status === 401 ? SignedOut : ApiError;
		throw new E(r.status, message, slug, fields);
	}
	return data as T;
}

export const get = <T>(path: string, query?: Query) => api<T>('GET', path, { query });
export const post = <T>(path: string, body?: unknown, query?: Query) =>
	api<T>('POST', path, { body, query });
export const put = <T>(path: string, body: unknown) => api<T>('PUT', path, { body });
export const patch = <T>(path: string, body: unknown, etag?: string) =>
	api<T>('PATCH', path, { body, etag });
export const del = (path: string, query?: Query, etag?: string) =>
	api<void>('DELETE', path, { query, etag });

// ---- formatting ---------------------------------------------------------------------------

export function bytes(n: number | null | undefined): string {
	if (!n) return '—';
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let i = 0;
	let v = n;
	while (v >= 1000 && i < units.length - 1) {
		v /= 1000;
		i++;
	}
	return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function rate(n: number | null | undefined): string {
	return n ? `${bytes(n)}/s` : '—';
}

export function duration(s: number | null | undefined): string {
	if (s === null || s === undefined) return '—';
	if (s < 60) return `${Math.round(s)} s`;
	if (s < 3600) return `${Math.round(s / 60)} min`;
	if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
	return `${(s / 86400).toFixed(1)} d`;
}

export function ago(iso: string | null | undefined): string {
	if (!iso) return 'never';
	const s = (Date.now() - new Date(iso).getTime()) / 1000;
	if (s < 0) return `in ${duration(-s)}`;
	if (s < 45) return 'just now';
	return `${duration(s)} ago`;
}

export const STATE_LABELS: Record<string, string> = {
	missing: 'Missing',
	searching: 'Searching',
	grabbed: 'Grabbed',
	downloading: 'Downloading',
	importing: 'Importing',
	acquired: 'In library'
};
