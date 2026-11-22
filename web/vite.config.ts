import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// The build goes straight into the Python package, which serves it (fetchbox/ui.py).
// `npm run dev` proxies the API to a local `fetchbox serve` on its default port.
export default defineConfig({
	plugins: [
		sveltekit({
			adapter: adapter({
				pages: '../src/fetchbox/static',
				assets: '../src/fetchbox/static',
				fallback: 'index.html',
				strict: true
			}),
			// Strict CSP as a <meta> tag, with hashes of SvelteKit's own inline bootstrap
			// script: no 'unsafe-inline' for scripts. Inline style attributes are allowed
			// (Svelte uses them); posters come from TMDB over https.
			csp: {
				mode: 'hash',
				directives: {
					'default-src': ['self'],
					'script-src': ['self'],
					'style-src': ['self', 'unsafe-inline'],
					'img-src': ['self', 'data:', 'https:'],
					'connect-src': ['self'],
					'font-src': ['self'],
					'object-src': ['none'],
					'base-uri': ['self'],
					'form-action': ['self']
				}
			}
		})
	],
	server: {
		proxy: {
			'/api': { target: 'http://127.0.0.1:8787', ws: true }
		}
	}
});
