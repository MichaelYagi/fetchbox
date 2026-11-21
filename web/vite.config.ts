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
			})
		})
	],
	server: {
		proxy: {
			'/api': { target: 'http://127.0.0.1:8787', ws: true }
		}
	}
});
