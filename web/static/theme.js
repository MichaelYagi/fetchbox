// The saved colour theme, set before the page draws (no flash of the default theme).
// A file, not an inline script: the page's security policy only runs its own scripts.
try {
	var theme = localStorage.getItem('fetchbox-theme');
	if (theme) document.documentElement.setAttribute('data-theme', theme);
} catch (e) {
	/* storage blocked: the default theme */
}
