// What /view serves: a form for the view token. Only once the server accepts the token does it fetch the viewer and
// put it in place of this page, so the viewer itself is never shown to anyone without the token.
(function () {
  const KEY = 'swarmchat.view.token';
  const theme = localStorage.getItem('swarmchat.view.theme');  // the theme picked in the viewer, if any
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  const form = document.querySelector('form'), err = document.querySelector('.err');

  async function open(token) {
    const r = await fetch('/v1/view_page', {method: 'POST', body: '{}',
      headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}});
    if (!r.ok) throw new Error(r.status === 401 ? 'That token was refused.' : `The server answered ${r.status}.`);
    const p = await r.json();
    localStorage.setItem(KEY, token);  // where the viewer looks for it
    const doc = new DOMParser().parseFromString(p.html, 'text/html');
    document.replaceChild(document.adoptNode(doc.documentElement), document.documentElement);
    const script = document.createElement('script');  // runs: the page's policy allows this exact script
    script.textContent = p.js;
    document.body.appendChild(script);
  }

  form.onsubmit = e => {
    e.preventDefault();
    open(form.token.value.trim()).catch(x => { err.textContent = x.message; });
  };
  const linked = () => (location.hash.match(/(^#|&)token=([0-9a-f]{16,})/) || [])[2];
  addEventListener('hashchange', function retry() {  // a link with another token, pasted into this tab
    if (linked()) open(linked()).then(() => removeEventListener('hashchange', retry), x => { err.textContent = x.message; });
  });
  const token = linked() || localStorage.getItem(KEY);
  if (token) open(token).catch(x => { err.textContent = x.message; });
  form.token.focus();
})();
