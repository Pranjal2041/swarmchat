'use strict';
// swarmchat view: watch every chat on a server, live. Plain JavaScript with no build step, served as one page.
// Every bit of text an agent wrote goes through esc() or html``, and the page's Content-Security-Policy lets only
// this script run, so a message can never become code.

// ---------------------------------------------------------------------- helpers

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
class Safe { constructor(s) { this.s = s; } toString() { return this.s; } }
const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = v => String(v).replace(/[&<>"']/g, c => ESC[c]);
const part = v => v == null || v === false ? '' : v instanceof Safe ? v.s : Array.isArray(v) ? v.map(part).join('') : esc(v);
// A template that escapes everything put into it, except what is already Safe.
const html = (strings, ...vals) => new Safe(strings.reduce((out, s, i) => out + part(vals[i - 1]) + s));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const num = n => (n || 0).toLocaleString('en-US');
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many || one + 's'}`;
const ms = at => typeof at === 'number' ? at * 1000 : Date.parse(at);
const pad = n => String(n).padStart(2, '0');
const clock = at => { const d = new Date(ms(at)); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const clockS = at => { const d = new Date(ms(at)); return `${clock(at)}:${pad(d.getSeconds())}`; };
const full = at => new Date(ms(at)).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'medium'});
const dayOf = at => new Date(ms(at)).toDateString();
const dayName = at => {
  const d = new Date(ms(at)), now = new Date();
  if (d.toDateString() === now.toDateString()) return 'Today';
  return d.toLocaleDateString(undefined, {weekday: 'long', month: 'long', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric'});
};
function ago(at, ref = Date.now()) {
  const s = Math.max(0, (ref - ms(at)) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)}d ago`;
  return new Date(ms(at)).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
}
const bytes = n => n < 1000 ? `${n} B` : n < 1e6 ? `${(n / 1e3).toFixed(1)} KB` : n < 1e9 ? `${(n / 1e6).toFixed(1)} MB` : `${(n / 1e9).toFixed(1)} GB`;
function hashHue(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619); return (h >>> 0) % 360; }
const userColor = id => `hsl(${hashHue(id)} 62% 68%)`;
function initials(name) {
  const w = String(name).replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  return ((w[0] || '?')[0] + (w.length > 1 ? w[w.length - 1][0] : (w[0] || '')[1] || '')).toUpperCase();
}
function throttle(fn, wait) {
  let last = 0, t = null;
  return () => {
    const left = wait - (Date.now() - last);
    if (left <= 0) { last = Date.now(); fn(); } else if (!t) t = setTimeout(() => { t = null; last = Date.now(); fn(); }, left);
  };
}

// ---------------------------------------------------------------------- talking to the server

let TOKEN = '';
async function api(name, params = {}) {
  const r = await fetch('/v1/' + name, {method: 'POST', body: JSON.stringify(params),
    headers: {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}});
  let data;
  try { data = await r.json(); } catch (_) { const e = new Error(`The server sent no JSON (HTTP ${r.status}).`); e.status = r.status; throw e; }
  if (!r.ok) { const e = new Error(data.error || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return data;
}

async function download(id, name) {
  const r = await fetch('/v1/view_download', {method: 'POST', body: JSON.stringify({id}),
    headers: {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}});
  if (!r.ok) return toast((await r.json().catch(() => ({}))).error || 'Download failed.');
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement('a'), {href: url, download: name || 'file'});
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

// Pictures are fetched with the token as they come near the screen, and the newest are kept as object URLs. Each
// counts what it takes drawn, 4 bytes a pixel, or its file's size if that is more.
const PICS = new Map();  // attachment id -> {p: a promise of its object URL, size}
let picBytes = 0;
function picture(id, type, size) {
  let pic = PICS.get(id);
  if (pic) { PICS.delete(id); PICS.set(id, pic); return pic.p; }  // the most recently used stay at the end
  const p = fetch('/v1/view_download', {method: 'POST', body: JSON.stringify({id}),
    headers: {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}})
    .then(async r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const b = await r.blob();
      return URL.createObjectURL(b.slice(0, b.size, type));  // the type the server read from the bytes
    });
  pic = {p, size};
  p.catch(() => { if (PICS.get(id) === pic) { PICS.delete(id); picBytes -= size; } });
  PICS.set(id, pic); picBytes += size;
  for (const [old, o] of PICS) {
    if (PICS.size <= 400 && picBytes <= 400e6) break;
    PICS.delete(old); picBytes -= o.size;
    o.p.then(url => URL.revokeObjectURL(url), () => {});  // a picture already drawn stays drawn
  }
  return p;
}
const near = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const img = e.target, d = img.dataset;
    near.unobserve(img); watched.delete(img);
    const broken = () => img.parentElement.classList.add('broken');
    img.onerror = broken;
    picture(d.pic, d.type, Math.max(+d.size, 4 * d.w * d.h)).then(url => { img.src = url; }, broken);
  }
}, {rootMargin: '800px 0px'});
const watched = new Set();
function watchPictures(changes) {  // only in what was just added: a long file in the viewer adds many lines
  for (const img of watched) if (!img.isConnected) { near.unobserve(img); watched.delete(img); }
  for (const c of changes) for (const n of c.addedNodes) {
    if (n.nodeType !== 1) continue;
    for (const img of n.matches('img[data-pic]') ? [n] : n.querySelectorAll('img[data-pic]')) {
      if (!img.classList.contains('seen')) { img.classList.add('seen'); watched.add(img); near.observe(img); }
    }
  }
}

// ---------------------------------------------------------------------- what the page knows

const S = {
  server: {}, users: new Map(), chats: new Map(), feed: 0, total: 0, sent: new Map(), last: new Map(),
  route: {}, tab: 'chats', filter: '', as: null, rate: [], view: null, pane: null, liveState: '',
};
const user = id => S.users.get(id) || {id, name: id, fields: {}, status: '', removed: true};
const name = id => user(id).name;
const first = id => name(id).split(/\s+/)[0];
const KIND = {channel: '#', group: '◆', direct: '●'};

function title(c, as = S.route.as) {
  if (!c) return 'unknown chat';
  if (c.name) return '#' + c.name;
  const people = c.members.filter(u => !as || u !== as || c.members.length === 1);
  const names = people.map(name);
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} +${names.length - 3}` : names.join(c.kind === 'direct' && !as ? ' & ' : ', ');
}
const chatTitle = id => title(S.chats.get(id));

function addChat(c) {
  const old = S.chats.get(c.id);
  S.chats.set(c.id, c);
  if (!old) S.total += c.messages;
  else S.total += c.messages - old.messages;
}

async function loadSnapshot() {
  const t = performance.now();
  const r = await api('view_snapshot');
  S.server = r.server; S.feed = r.feed;
  S.users = new Map(r.users.map(u => [u.id, u]));
  S.chats = new Map(); S.total = 0; S.sent = new Map(); S.last = new Map();
  for (const c of r.chats) {
    addChat(c);
    for (const [u, n] of Object.entries(c.sent)) S.sent.set(u, (S.sent.get(u) || 0) + n);
    for (const [u, at] of Object.entries(c.last_by)) if (!S.last.has(u) || S.last.get(u) < at) S.last.set(u, at);
  }
  S.loadedIn = performance.now() - t;
}

// ---------------------------------------------------------------------- the address bar holds where you are

const KEYS = ['layout', 'chat', 'm', 'had', 'thread', 'knew', 'as', 'person', 'q', 'unread', 'post', 'sort'];
function readRoute() {
  const p = new URLSearchParams(location.hash.slice(1));
  const r = {};
  for (const k of KEYS) if (p.get(k)) r[k] = p.get(k);
  r.layout = ['chat', 'board', 'map'].includes(r.layout) ? r.layout : 'chat';
  return r;
}
// Goes somewhere. Keys set to null are dropped. Without keep, only the layout and the agent seen as come along.
function go(changes, keep = false) {
  const base = keep ? {...S.route} : {layout: S.route.layout, as: S.route.as};
  const r = {...base, ...changes};
  const p = new URLSearchParams();
  for (const k of KEYS) if (r[k] != null && r[k] !== '') p.set(k, r[k]);
  const hash = '#' + p.toString();
  if (location.hash !== hash) location.hash = hash; else route();
}

function viewKey(r) {
  if (r.layout === 'map') return 'map';
  if (r.q) return `q|${r.q}|${r.as}`;
  if (r.person) return `person|${r.person}`;
  if (r.unread && r.as) return `unread|${r.as}`;
  if (r.layout === 'board') return r.post ? `post|${r.post}|${r.as}` : `board|${r.chat}|${r.sort}|${r.as}`;
  return r.chat ? `chat|${r.chat}|${r.m}|${r.had}|${r.knew}|${r.as}` : `all|${r.as}`;
}

async function route() {
  const prev = S.route;
  S.route = readRoute();
  const r = S.route;
  document.body.className = 'layout-' + r.layout;
  if (r.as !== prev.as) await loadAs();
  const key = viewKey(r);
  if (r.as !== prev.as && key === S.viewKey && S.view && S.view.onAs) S.view.onAs();
  if (key !== S.viewKey) {
    if (S.view && S.view.destroy) S.view.destroy();
    S.viewKey = key;
    const main = $('#main');
    main.replaceChildren();
    S.view = r.layout === 'map' ? new MapView(main)
      : r.q ? new SearchView(main, r.q)
      : r.person ? new PersonView(main, r.person)
      : r.unread && r.as ? new UnreadView(main)
      : r.layout === 'board' ? (r.post ? new PostView(main, r.post) : new BoardView(main, r.chat, r.sort || 'hot'))
      : r.chat ? new ChatView(main, r.chat, {jump: r.m ? +r.m : null, had: r.had ? +r.had : null, knew: r.knew})
      : new ActivityView(main);
  }
  const paneKey = r.layout === 'map' ? 'map' : r.thread ? 'thread|' + r.thread : r.knew ? 'knew|' + r.knew : '';
  if (paneKey !== S.paneKey) {
    S.paneKey = paneKey;
    if (S.pane && S.pane.destroy) S.pane.destroy();
    S.pane = r.thread && r.layout !== 'map' ? new ThreadPane(r.thread) : r.knew && r.layout !== 'map' ? new KnewPane(r.knew) : null;
    $('#pane').classList.toggle('open', !!S.pane || (r.layout === 'map' && !!S.mapPane));
    if (!S.pane && r.layout !== 'map') $('#pane').replaceChildren();
  }
  renderTop();
  side.build();
}

// ---------------------------------------------------------------------- seeing as one agent

async function loadAs() {
  const id = S.route.as;
  if (!id) { S.as = null; return; }
  try {
    const r = await api('view_as', {user: id, unread: true, limit: 200});
    if (S.route.as !== id) return;
    S.as = {user: id, chats: new Map(r.chats.map(c => [c.id, c])), order: r.chats.map(c => c.id), positions: r.positions,
      unread: r.unread, unreadLeft: r.unread_left};
  } catch (e) {
    toast(e.message);
    S.as = null;
  }
}
// At most once a second, so steady traffic in the agent's chats still shows (a debounce would wait forever).
const refreshAs = throttle(async () => {
  if (!S.route.as) return;
  await loadAs();
  side.build();
  if (S.view && S.view.onAs) S.view.onAs();
}, 1000);

// ---------------------------------------------------------------------- top bar

function renderTop() {
  const r = S.route;
  for (const b of $$('#layouts button')) b.classList.toggle('on', b.dataset.layout === r.layout);
  const agents = [...S.users.values()].filter(u => !u.removed).length;
  const now = Date.now();
  S.rate = S.rate.filter(t => now - t < 10000);
  const rate = S.rate.length / Math.max(1, Math.min(10, (now - S.since) / 1000));
  $('#stats').innerHTML = html`<b>${num(agents)}</b> agents · <b>${num(S.chats.size)}</b> chats · <b>${num(S.total)}</b> messages${rate ? html` · <b>${rate < 10 ? rate.toFixed(1) : Math.round(rate)}</b>/s` : ''}`;
  const as = $('#as');
  as.classList.toggle('on', !!r.as);
  as.innerHTML = r.as ? html`${avatar(r.as, 'sm')}<span>As <b>${name(r.as)}</b></span><span class="x" data-clear-as title="See everything again">×</span>`
    : html`<span>As everyone</span><span>▾</span>`;
}
setInterval(() => renderTop(), 2000);

function setLive(state) {
  if (S.liveState === state) return;
  S.liveState = state;
  const el = $('#live');
  el.className = 'live ' + state;
  el.textContent = {on: 'Live', ro: 'Read-only', off: 'Offline'}[state] || '';
  el.title = {on: 'Following every chat as it happens', ro: `No server is running on ${S.server.data}: this is the folder as it was on disk`,
    off: 'Lost the server. Trying again.'}[state] || '';
}

function avatar(id, size = '') {
  const u = user(id);
  return html`<span class="av ${size}" style="--c:${userColor(id)}" title="${u.name}">${initials(u.name)}</span>`;
}

// ---------------------------------------------------------------------- sidebar: a list that draws only the rows on screen

const side = {
  rows: [], h: 30,
  list: null, space: null, box: null,
  init() {
    this.list = $('#list'); this.space = $('.vspace', this.list); this.box = $('.vrows', this.list);
    this.list.addEventListener('scroll', () => this.draw(), {passive: true});
    new ResizeObserver(() => this.draw()).observe(this.list);
    this.list.addEventListener('click', e => {
      const row = e.target.closest('[data-i]');
      if (row) this.rows[+row.dataset.i].open();
    });
    $('#filter').addEventListener('input', e => { S.filter = e.target.value.trim().toLowerCase(); this.build(); this.list.scrollTop = 0; });
    $('#tabs').addEventListener('click', e => {
      const b = e.target.closest('[data-tab]');
      if (!b) return;
      S.tab = b.dataset.tab; this.build(); this.list.scrollTop = 0;
    });
  },
  build() {
    for (const b of $$('#tabs button')) b.classList.toggle('on', b.dataset.tab === S.tab);
    $('#filter').placeholder = S.tab === 'people' ? 'Filter people' : 'Filter chats';
    this.rows = S.tab === 'people' ? peopleRows() : chatRows();
    this.space.style.height = this.rows.length * this.h + 'px';
    this.draw();
  },
  draw() {
    const top = this.list.scrollTop, height = this.list.clientHeight || 800;
    const start = Math.max(0, Math.floor(top / this.h) - 8), end = Math.min(this.rows.length, Math.ceil((top + height) / this.h) + 8);
    this.box.style.transform = `translateY(${start * this.h}px)`;
    let out = '';
    for (let i = start; i < end; i++) out += this.rows[i].html(i);
    this.box.innerHTML = out;
  },
};
const buildSide = throttle(() => side.build(), 500);

function chatRows() {
  const r = S.route, f = S.filter, rows = [];
  const special = (label, icon, on, open, n) => ({open, html: i => html`<div class="row special${on ? ' on' : ''}" data-i="${i}"><span class="ico">${icon}</span><span class="t">${label}</span>${n ? html`<span class="badge">${num(n)}</span>` : ''}</div>`.s});
  if (!f) {
    if (r.layout === 'board') rows.push(special('Front page', '★', !r.chat && !r.post && !r.q && !r.person && !r.unread, () => go({chat: null})));
    else rows.push(special('All activity', '≋', !r.chat && !r.q && !r.person && !r.unread, () => go({chat: null})));
    if (r.as && S.as) rows.push(special(`${first(r.as)}'s unread`, '✉', !!r.unread, () => go({unread: 1}),
      S.as.order.reduce((n, id) => n + (S.as.chats.get(id).unread || 0), 0)));
  }
  let chats;
  if (r.as && S.as) chats = S.as.order.map(id => S.chats.get(id)).filter(Boolean);
  else chats = [...S.chats.values()].sort((a, b) => (b.last_at > a.last_at) - (b.last_at < a.last_at));
  if (f) chats = chats.filter(c => title(c).toLowerCase().includes(f) || c.id.includes(f));
  S.busiest = chats.reduce((most, c) => Math.max(most, c.recent || 0), 1);
  const groups = [['channel', 'Channels'], ['group', 'Group chats'], ['direct', 'Direct chats']];
  for (const [kind, label] of groups) {
    const these = chats.filter(c => c.kind === kind);
    if (!these.length) continue;
    rows.push({open() {}, html: () => html`<div class="row head">${label} · ${num(these.length)}</div>`.s});
    for (const c of these) rows.push(chatRow(c));
  }
  if (rows.length <= 2 && f) rows.push({open() {}, html: () => '<div class="row head">Nothing matches</div>'});
  return rows;
}

function chatRow(c) {
  const r = S.route;
  const mine = r.as && S.as ? S.as.chats.get(c.id) : null;
  return {
    open: () => go({chat: c.id, layout: r.layout === 'map' ? 'chat' : r.layout}),
    html: i => {
      const on = r.chat === c.id && !r.q && !r.person && !r.unread;
      const unread = mine ? mine.unread : 0;
      const badge = mine ? (mine.mentioned ? html`<span class="badge at" title="Mentioned">@${unread}</span>` : unread ? html`<span class="badge${mine.notify !== 'all' ? ' mute' : ''}" title="Unread for ${name(r.as)}${mine.notify !== 'all' ? `, notify ${mine.notify}` : ''}">${num(unread)}</span>` : '')
        : html`<span class="n" title="Messages">${num(c.messages)}</span>`;
      const busy = c.recent ? Math.max(8, Math.round(100 * Math.sqrt(c.recent / S.busiest))) : 0;
      return html`<div class="row${on ? ' on' : ''}${unread ? ' unread' : ''}" data-i="${i}" title="${title(c)}${c.public ? ' (public)' : ''}${c.archived ? ' (archived)' : ''}${c.description ? '\n' + c.description : ''}
${num(c.recent || 0)} messages in the last 15 minutes"><span class="ico ${c.kind}">${KIND[c.kind]}</span><span class="t">${c.name || title(c)}</span>${busy ? html`<span class="busy"><i style="width:${busy}%"></i></span>` : ''}${badge}</div>`.s;
    },
  };
}

function peopleRows() {
  const f = S.filter, r = S.route;
  let people = [...S.users.values()];
  if (f) people = people.filter(u => `${u.id} ${u.name} ${u.status} ${Object.values(u.fields || {}).join(' ')}`.toLowerCase().includes(f));
  people.sort((a, b) => (S.last.get(b.id) || 0) - (S.last.get(a.id) || 0) || a.name.localeCompare(b.name));
  const rows = [{open() {}, html: () => html`<div class="row head">People · ${num(people.length)}</div>`.s}];
  for (const u of people) {
    rows.push({
      open: () => go({person: u.id, layout: r.layout === 'map' ? 'chat' : r.layout}),
      html: i => {
        const role = Object.values(u.fields || {})[0];
        const last = S.last.get(u.id);
        return html`<div class="row${r.person === u.id ? ' on' : ''}" data-i="${i}" title="${u.name} (${u.id})${u.status ? '\n' + u.status : ''}\n${num(S.sent.get(u.id) || 0)} messages sent">${avatar(u.id, 'sm')}<span class="t">${u.name}${u.removed ? ' (removed)' : ''}${role ? html` <span class="n">${role}</span>` : ''}</span><span class="n" title="Last message">${last ? ago(last).replace(' ago', '') : ''}</span></div>`.s;
      },
    });
  }
  return rows;
}

// ---------------------------------------------------------------------- how a message looks

const EMOJI = {'+1': '👍', '-1': '👎', 'thumbsup': '👍', 'thumbsdown': '👎', 'heart': '❤️', 'eyes': '👀', 'check': '✅', 'x': '❌', 'tada': '🎉', 'fire': '🔥', 'rocket': '🚀', 'smile': '🙂'};
function inline(text) {
  let s = esc(text);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/(^|[\s(>])@([\p{L}\p{N}._-]+)/gu, '$1<span class="at">@$2</span>');
  s = s.replace(/\bhttps?:\/\/(?:[^\s<&]|&amp;)*[^\s<.,;:!?)\]'"&]/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
  return s;
}
function rich(text, mark) {
  if (mark) return highlight(text, mark);
  return new Safe(String(text).split(/(```[\s\S]*?```)/g).map(p =>
    p.length >= 6 && p.startsWith('```') && p.endsWith('```') ? fence(p.slice(3, -3)) : inline(p)).join(''));
}
function fence(body) {  // a code block in a message, highlighted when it names its language, as ```python
  const lang = /^([\w+#.-]*)\n/.exec(body), kind = lang && langKind(lang[1]);
  return `<pre>${kind ? blockHtml(body.slice(lang[0].length), kind) : esc(lang ? body.slice(lang[0].length) : body)}</pre>`;
}
function highlight(text, term) {
  const low = text.toLowerCase(), t = term.toLowerCase();
  let out = '', i = 0, j;
  while (t && (j = low.indexOf(t, i)) >= 0) { out += esc(text.slice(i, j)) + '<mark>' + esc(text.slice(j, j + t.length)) + '</mark>'; i = j + t.length; }
  return new Safe(out + esc(text.slice(i)));
}

// Files under a message or post. Pictures are drawn, in a box of their own shape that is there before their bytes
// arrive, so nothing moves when they do. A text file shows its first lines, and opens whole on a click. Anything else
// is a button that downloads it.
function filesHtml(list, big) {
  if (!list || !list.length) return '';
  const pics = list.filter(a => a.image), texts = list.filter(a => a.text), rest = list.filter(a => !a.image && !a.text);
  return html`${pics.length ? html`<div class="pics">${pics.map(a => picHtml(a, big))}</div>` : ''}${texts.map(a => snipHtml(a, big))}${rest.length ? html`<div class="files">${rest.map(a => html`<button class="file" data-file="${a.id}" data-name="${a.name}" title="Download">${a.folder ? '🗂' : '📄'} ${a.name}<span class="k">${a.folder ? 'folder · ' : ''}${bytes(a.size)}</span></button>`)}</div>` : ''}`;
}
function picHtml(a, big) {
  const {type, width: w, height: h} = a.image;
  const k = Math.min(1, (big ? 640 : 400) / w, (big ? 480 : 300) / h);
  return html`<button class="pic" data-image="${a.id}" data-name="${a.name}" data-type="${type}" data-w="${w}" data-h="${h}" data-size="${a.size}" title="${a.name} · ${w} × ${h} · ${bytes(a.size)}" style="width:${Math.max(1, Math.round(w * k))}px;aspect-ratio:${w} / ${h}"><img data-pic="${a.id}" data-type="${type}" data-size="${a.size}" data-w="${w}" data-h="${h}" alt="${a.name}"></button>`;
}
function openPicture(d) {
  const w = +d.w, h = +d.h, k = Math.min(1, innerWidth * 0.92 / w, (innerHeight - 90) * 0.92 / h);
  const box = document.createElement('div');
  box.className = 'lightbox';
  box.innerHTML = html`<img alt="${d.name}" width="${Math.max(1, Math.round(w * k))}" height="${Math.max(1, Math.round(h * k))}"><div class="cap"><b>${d.name}</b><span>${w} × ${h} · ${bytes(+d.size)}</span><button class="btn" data-file="${d.image}" data-name="${d.name}">Download</button></div>`.s;
  overlay.open(box);
  $('.shade', overlay.el).classList.add('dark');
  const img = $('img', box);
  img.onclick = () => overlay.close();
  picture(d.image, d.type, Math.max(+d.size, 4 * w * h)).then(url => { img.src = url; }, () => toast('The picture could not be loaded.'));
}

// ---------------------------------------------------------------------- text files

// A text file shows under its message as a card with its first lines, and opens whole in a viewer, formatted for its
// kind: code is highlighted, Markdown rendered, a diff shows its added and removed lines, a log its levels and
// terminal colours, a CSV file a table. The kind comes from the file's name, and for some names from its first lines.
// Every piece of a file goes through esc() before markup is put around it, so a file can never become code.

// extension: kind of formatting and the name shown
const EXT = Object.fromEntries(('py python Python|pyi python Python|js js JavaScript|mjs js JavaScript|cjs js JavaScript|' +
  'jsx js JavaScript|ts js TypeScript|tsx js TypeScript|json json JSON|ipynb json Notebook|geojson json GeoJSON|' +
  'jsonl jsonl JSON lines|ndjson jsonl JSON lines|sh shell Shell|bash shell Shell|zsh shell Shell|fish shell Shell|' +
  'html html HTML|htm html HTML|xhtml html HTML|xml html XML|svg html SVG|vue html Vue|css css CSS|scss css SCSS|' +
  'less css Less|c c C|h c C|cc c C++|cpp c C++|cxx c C++|hpp c C++|cu c CUDA|java c Java|kt c Kotlin|kts c Kotlin|' +
  'go c Go|rs c Rust|cs c C#|swift c Swift|scala c Scala|php c PHP|dart c Dart|zig c Zig|proto c Protobuf|' +
  'rb hash Ruby|pl hash Perl|r hash R|jl hash Julia|mk hash Makefile|cmake hash CMake|sql sql SQL|yaml yaml YAML|' +
  'yml yaml YAML|toml ini TOML|ini ini INI|cfg ini Config|conf ini Config|env ini Environment|properties ini Properties|' +
  'md markdown Markdown|markdown markdown Markdown|mdx markdown MDX|diff diff Diff|patch diff Patch|log log Log|' +
  'out log Output|err log Errors|csv csv CSV|tsv tsv TSV|txt text Text|text text Text|rst text reStructuredText|tex text TeX')
  .split('|').map(s => { const [ext, kind, ...label] = s.split(' '); return [ext, [kind, label.join(' ')]]; }));
const BARE = {dockerfile: ['hash', 'Dockerfile'], makefile: ['hash', 'Makefile'], gnumakefile: ['hash', 'Makefile'],
  'cmakelists.txt': ['hash', 'CMake'], '.gitignore': ['hash', 'Git ignore'], '.dockerignore': ['hash', 'Docker ignore'],
  '.env': ['ini', 'Environment'], '.bashrc': ['shell', 'Shell'], '.zshrc': ['shell', 'Shell'], '.profile': ['shell', 'Shell']};
// a file's badge, when its extension is long or missing, and its colour
const BADGE = {markdown: 'md', diff: 'diff', python: 'py', js: 'js', shell: 'sh', html: 'html', css: 'css', c: 'code', hash: 'code',
  json: 'json', jsonl: 'json', yaml: 'yaml', ini: 'conf', sql: 'sql', log: 'log', csv: 'csv', tsv: 'tsv'};
const FAMILY = {markdown: 'doc', text: 'plain', log: 'plain', diff: 'diff', csv: 'data', tsv: 'data', json: 'data', jsonl: 'data',
  html: 'web', css: 'web'};

const extOf = name => { const base = name.split('/').pop().toLowerCase(), dot = base.lastIndexOf('.'); return dot > 0 ? base.slice(dot + 1) : ''; };
function fileKind(name, sample = '') {
  const base = name.split('/').pop().toLowerCase();
  let [kind, label] = BARE[base] || EXT[extOf(name)] || ['text', 'Text'];
  if (kind === 'text') {  // a file named for no kind, or plain text, may still be a diff, a script or a log
    if (/^(diff |--- \S|Index: |From [0-9a-f]{40} )/.test(sample) && /^\+\+\+ /m.test(sample)) [kind, label] = ['diff', 'Diff'];
    else if (/^#!.*\b(?:ba|z|da|k)?sh\b/.test(sample)) [kind, label] = ['shell', 'Shell'];
    else if (/^#!.*python/.test(sample)) [kind, label] = ['python', 'Python'];
    else if (/^#!.*\b(node|deno|bun)\b/.test(sample)) [kind, label] = ['js', 'JavaScript'];
    else if (sample.split('\n').filter(l => LOG_TIME.test(l)).length >= 3) [kind, label] = ['log', 'Log'];
  }
  const ext = extOf(name);
  return {kind, label, badge: (ext && ext.length <= 4 ? ext : BADGE[kind] || 'txt').toUpperCase()};
}

// ---- highlighting: each kind of code is a list of [class, pattern], and at each place the first that matches wins.
// Patterns use no capturing groups. "(?<![^\n])" means at the start of a line, "(?![\s\S])" at the end of the text.
const words = list => new RegExp(`\\b(?:${list.trim().split(/\s+/).join('|')})\\b`);
const NUM = /\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[jJlLuUfFnN]?\b/;
const DQ = /"(?:[^"\\\n]|\\.)*"?/, SQ = /'(?:[^'\\\n]|\\.)*'?/;
const BLOCK = /\/\*[\s\S]*?(?:\*\/|(?![\s\S]))/, SLASHES = /\/\/[^\n]*/, HASHES = /(?<![\w$])#[^\n]*/;
const WORD = /[A-Za-z_$][\w$]*/, TYPE = /\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*\b/, CALL = /\b[A-Za-z_$][\w$]*(?=\s*\()/;
const LANG = {
  python: [['cc', /#[^\n]*/], ['cs', /(?<!\w)[rRbBuUfF]{0,2}(?:"""[\s\S]*?(?:"""|(?![\s\S]))|'''[\s\S]*?(?:'''|(?![\s\S])))/],
    ['cs', /(?<!\w)[rRbBuUfF]{0,2}(?:"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?)/], ['cf', /(?<![\w)\]])@[A-Za-z_][\w.]*/],
    ['ck', words('and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield')],
    ['cb', words('True False None self cls')], ['cf', /(?<=\b(?:def|class)\s+)[A-Za-z_]\w*/], ['cf', CALL], ['cn', NUM], ['', WORD]],
  js: [['cc', SLASHES], ['cc', BLOCK], ['cs', /`(?:[^`\\]|\\[\s\S])*`?/], ['cs', DQ], ['cs', SQ],
    ['ck', words('break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch throw try typeof var void while with yield async await as interface type enum implements private public protected readonly declare namespace abstract')],
    ['cb', words('true false null undefined this NaN Infinity')], ['ct', TYPE], ['cf', CALL], ['cn', NUM], ['', WORD]],
  c: [['cc', SLASHES], ['cc', BLOCK], ['cf', /(?<![^\n])[ \t]*#[ \t]*[a-z]+\b[^\n]*/], ['cs', /`[^`]*`?/], ['cs', DQ],
    ['cs', /'(?:[^'\\\n]|\\.){1,8}'/],
    ['ck', words('auto break case catch char class const constexpr continue default delete do double else enum explicit extern final float for friend goto if inline int long namespace new noexcept operator override private protected public register return short signed sizeof static struct switch template throw try typedef typename union unsigned using virtual void volatile while bool boolean byte extends implements import instanceof interface package super synchronized throws abstract func go chan defer fallthrough range select var fn let mut impl trait pub use mod crate match async await fun companion sealed internal suspend guard extension protocol')],
    ['cb', words('true false null nullptr NULL nil None Some Ok Err this self Self')], ['ct', TYPE], ['cf', CALL], ['cn', NUM], ['', WORD]],
  shell: [['cc', HASHES], ['cs', /"(?:[^"\\]|\\[\s\S])*"?/], ['cs', /'[^']*'?/], ['cv', /\$(?:\{[^}\n]*\}|[A-Za-z_]\w*|[0-9@#?*!$-])/],
    ['ck', words('if then else elif fi for while until do done case esac in function return local export readonly declare unset shift break continue exit source alias set trap eval exec select')],
    ['cn', /\b\d+\b/], ['', /[A-Za-z_][\w-]*/]],
  hash: [['cc', HASHES], ['cs', DQ], ['cs', SQ],
    ['ck', /(?<![^\n])[ \t]*(?:FROM|RUN|COPY|ADD|CMD|ENTRYPOINT|ENV|ARG|WORKDIR|EXPOSE|USER|LABEL|VOLUME|HEALTHCHECK|SHELL|ONBUILD|STOPSIGNAL)\b/],
    ['cf', /(?<![^\n])[\w./%$()-]+(?=[ \t]*::?(?!=))/], ['cv', /\$[({][^)}\n]*[)}]|\$\w+/],
    ['ck', words('def end do if elsif else unless while until class module return yield begin rescue ensure require function local then for in sub my use')],
    ['cb', words('true false nil TRUE FALSE NULL self')], ['cn', NUM], ['', WORD]],
  json: [['ca', /"(?:[^"\\\n]|\\.)*"(?=\s*:)/], ['cs', DQ], ['cb', /\b(?:true|false|null)\b/], ['cn', /-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/]],
  yaml: [['cc', /(?<![^\s])#[^\n]*/], ['cc', /(?<![^\n])(?:---|\.\.\.)(?=\s|(?![\s\S]))/], ['ca', /[\w.\/-]+(?=:(?:[ \t\n]|(?![\s\S])))/],
    ['ca', /"[^"\n]*"(?=:\s)/], ['cs', DQ], ['cs', /'(?:[^'\n]|'')*'?/], ['cf', /[&*][\w-]+/],
    ['cb', /\b(?:true|false|yes|no|on|off|null|True|False|Yes|No|NULL|Null)\b|(?<=:\s*)~/], ['cn', NUM], ['', WORD]],
  ini: [['cc', /(?<![^\n])[ \t]*[#;][^\n]*/], ['ct', /(?<![^\n])[ \t]*\[[^\]\n]*\]+/], ['ca', /(?<![^\n])[ \t]*[\w.\-"' ]*?[\w"'](?=[ \t]*[=:])/],
    ['cs', /"""[\s\S]*?(?:"""|(?![\s\S]))/], ['cs', DQ], ['cs', /'[^'\n]*'?/], ['cb', words('true false yes no on off null none')], ['cn', NUM], ['', WORD]],
  css: [['cc', BLOCK], ['cs', DQ], ['cs', SQ], ['ck', /@[\w-]+/], ['ca', /[\w-]+(?=\s*:(?![^{};\n]*\{))/], ['cn', /#[\da-fA-F]{3,8}\b/],
    ['', /[A-Za-z_-][\w-]*/], ['cn', /(?:\d+\.?\d*|\.\d+)(?:%|[a-zA-Z]+)?/]],
  sql: [['cc', /--[^\n]*/], ['cc', BLOCK], ['cs', /'(?:[^']|'')*'?/], ['ca', /"[^"\n]*"?|`[^`\n]*`?/],
    ['ck', /\b(?:select|from|where|and|or|not|insert|into|values|update|set|delete|create|table|index|view|drop|alter|add|column|primary|key|foreign|references|join|left|right|inner|outer|full|cross|on|as|group|by|order|having|limit|offset|union|all|distinct|case|when|then|else|end|is|null|like|ilike|in|between|exists|with|returning|default|unique|check|constraint|asc|desc|if|begin|commit|rollback|transaction|over|partition)\b/i],
    ['cf', CALL], ['cn', NUM], ['', WORD]],
  markdown: [['ch', /(?<![^\n])#{1,6}[ \t][^\n]*/], ['cs', /(?<![^\n])[ \t]*(?:```|~~~)[\s\S]*?(?:\n[ \t]*(?:```|~~~)[^\n]*|(?![\s\S]))/],
    ['cc', /(?<![^\n])[ \t]*>[^\n]*/], ['ck', /(?<![^\n])[ \t]*(?:[-*+]|\d+[.)])(?=[ \t])/], ['cs', /`[^`\n]+`/],
    ['cf', /!?\[[^\]\n]*\]\([^)\n]*\)/], ['cb', /\*\*[^*\n]+\*\*|__[^_\n]+__/]],
  html: [['cc', /<!--[\s\S]*?(?:-->|(?![\s\S]))/], ['cc', /<![A-Za-z][^>]*>|<\?[\s\S]*?(?:\?>|(?![\s\S]))/],
    [s => embedded(s, 'js'), /<script\b[^>]*>[\s\S]*?(?:<\/script\s*>|(?![\s\S]))/i],
    [s => embedded(s, 'css'), /<style\b[^>]*>[\s\S]*?(?:<\/style\s*>|(?![\s\S]))/i],
    [tagPieces, /<\/?[A-Za-z][\w:.-]*(?:\s[^<>]*?)?\/?>/], ['cv', /&#?\w+;/]],
};
LANG.jsonl = LANG.json;
const REGEX = {};

// [class, text] pieces that cover the whole text, in order. A class can be a function that splits its match further.
// A generator, so a large file is formatted a part at a time: the scan goes on from where it stopped, and a comment
// or string that runs over many lines is still one piece.
function* pieces(text, kind) {
  const rules = LANG[kind];
  if (!rules) return yield ['', text];
  const re = new RegExp(REGEX[kind] ||= rules.map(([, r]) => `(${r.source})`).join('|'), 'g' + (kind === 'sql' ? 'i' : ''));
  let at = 0, m;
  while ((m = re.exec(text))) {
    if (!m[0]) { re.lastIndex++; continue; }
    if (m.index > at) yield ['', text.slice(at, m.index)];
    let g = 1;
    while (m[g] === undefined) g++;
    const cls = rules[g - 1][0];
    if (typeof cls === 'function') yield* cls(m[0]); else yield [cls, m[0]];
    at = m.index + m[0].length;
  }
  if (at < text.length) yield ['', text.slice(at)];
}
const tokens = (text, kind) => [...pieces(text, kind)];
function tagPieces(s) {
  const name = /^<\/?([\w:.-]+)/.exec(s), out = [['', s.slice(0, name.index + name[0].length - name[1].length)], ['ct', name[1]]];
  const re = /("[^"]*"?|'[^']*'?)|([^\s=>\/"']+)/g, rest = s.slice(name[0].length);
  let at = 0, m;
  while ((m = re.exec(rest))) {
    if (m.index > at) out.push(['', rest.slice(at, m.index)]);
    out.push([m[1] ? 'cs' : 'ca', m[0]]);
    at = m.index + m[0].length;
  }
  out.push(['', rest.slice(at)]);
  return out;
}
function embedded(s, kind) {  // a <script> or <style> element: its tags, and what is inside as JavaScript or CSS
  const open = /^<[^>]*>/.exec(s)[0], close = /<\/\w+\s*>$/.exec(s), inner = s.slice(open.length, close ? close.index : s.length);
  return [...tagPieces(open), ...tokens(inner, kind), ...(close ? tagPieces(close[0]) : [])];
}
// Pieces as lines of HTML. A piece that runs over several lines is closed at the end of each and opened again.
function* htmlLines(list) {
  let cur = '';
  for (const [cls, s] of list) {
    const parts = s.split('\n');
    for (let i = 0; i < parts.length; i++) {
      if (i) { yield cur; cur = ''; }
      if (parts[i]) cur += cls ? `<span class="${cls}">${esc(parts[i])}</span>` : esc(parts[i]);
    }
  }
  yield cur;
}
const linesOf = list => [...htmlLines(list)];
const codeHtml = (text, kind) => linesOf(tokens(text, kind)).join('\n');
// A code block that names its language, in a message or in Markdown: ```python, ```ts, ```diff.
const ALIAS = {javascript: 'js', typescript: 'js', node: 'js', bash: 'shell', console: 'shell', shell: 'shell', shellsession: 'shell',
  python3: 'python', golang: 'c', rust: 'c', java: 'c', cpp: 'c', 'c++': 'c', csharp: 'c', kotlin: 'c', swift: 'c', ruby: 'hash',
  dockerfile: 'hash', makefile: 'hash', toml: 'ini', jsonc: 'json', xml: 'html', svg: 'html', patch: 'diff'};
const langKind = name => { const n = (name || '').toLowerCase(); return ALIAS[n] || (EXT[n] || [])[0] || (LANG[n] || n === 'diff' || n === 'log' ? n : null); };
const blockHtml = (text, kind) => kind === 'diff' || kind === 'log' ? textLines(text, kind).map(lineHtml).join('') : codeHtml(text, kind);

// ---- lines: {h: html, c: a class for the line, n: its number}, or for a diff a: and b:, its old and new numbers.
// lineBatches gives them size at a time, so the viewer shows the first while it formats the rest.
function* lineBatches(text, kind, size) {
  if (text.endsWith('\n')) text = text.slice(0, -1);
  if (kind === 'diff' || kind === 'log' || kind === 'text') {  // a line at a time, each knowing the lines before it
    const all = text.split('\n'), line = kind === 'diff' ? diffLine() : logLine(kind === 'log');
    for (let i = 0; i < all.length; i += size) yield all.slice(i, i + size).map(line);
    return;
  }
  let batch = [], n = 0;
  for (const h of htmlLines(pieces(text, kind))) {
    batch.push({h, n: ++n});
    if (batch.length >= size) { yield batch; batch = []; }
  }
  yield batch;
}
const textLines = (text, kind) => [...lineBatches(text, kind, Infinity)].flat();
const lineHtml = l => `<div class="cl${l.c ? ' ' + l.c : ''}"${l.n ? ` data-n="${l.n}"` : ''}${l.a ? ` data-a="${l.a}"` : ''}${l.b ? ` data-b="${l.b}"` : ''}>${l.h}</div>`;

// A diff: each file's header, each hunk's header, and its lines added and removed, numbered as in the old and the
// new file, with the code highlighted as the file's kind. Counting each hunk's lines keeps a removed line that
// starts with "--" from being taken for a header.
function diffLine() {
  let a = 0, b = 0, left = 0, right = 0, kind = 'text', m;
  const code = s => linesOf(pieces(s, kind))[0];
  return line => {
    if (left > 0 || right > 0) {
      const c = line[0];
      if (c === '+') { right--; return {c: 'da', b: b++, h: '+' + code(line.slice(1))}; }
      if (c === '-') { left--; return {c: 'dd', a: a++, h: '-' + code(line.slice(1))}; }
      if (c === '\\') return {c: 'dm', h: esc(line)};
      if (c === ' ' || line === '') { left--; right--; return {a: a++, b: b++, h: ' ' + code(line.slice(1))}; }
      left = right = 0;
    }
    if ((m = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line))) {
      a = +m[1]; left = m[2] == null ? 1 : +m[2]; b = +m[3]; right = m[4] == null ? 1 : +m[4];
      return {c: 'dh', h: `<span class="cf">${esc(m[0])}</span>${esc(line.slice(m[0].length))}`};
    }
    if ((m = /^diff --git a\/.* b\/(.*)$/.exec(line))) kind = fileKind(m[1]).kind;
    else if ((m = /^(?:\+\+\+|---) (?:[ab]\/)?(.+?)(?:\t.*)?$/.exec(line)) && m[1] !== '/dev/null') kind = fileKind(m[1]).kind;
    return {c: /^diff /.test(line) ? 'df' : 'dm', h: esc(line)};
  };
}

// A log, or plain text: terminal colours shown, other escapes dropped, and what a terminal would show after a
// carriage return. In a log, the time at the start of a line is dimmed, its level stands out, an error's line and a
// Python traceback are tinted red and a warning's yellow.
const LOG_TIME = /^\[?(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:[.,]\d+)?)?(?:Z|[+-]\d\d:?\d\d)?|\d\d:\d\d:\d\d(?:[.,]\d+)?|[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d)\]?/;
const LOG_LEVEL = /\b(?:(FATAL|CRITICAL|ERROR|SEVERE|PANIC|EMERG)|(WARN(?:ING)?)|(INFO|NOTICE)|(DEBUG|TRACE))\b/;
function logLine(levels) {
  const st = {};
  let trace = false, n = 0;
  return raw => {
    raw = raw.replace(/\r+$/, '');
    if (raw.includes('\r')) raw = raw.slice(raw.lastIndexOf('\r') + 1);
    raw = raw.replace(/[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]/g, '');
    const pieces = raw.includes('\x1b') ? ansi(raw, st) : null, plain = pieces ? pieces.map(p => p[1]).join('') : raw;
    const line = {n: ++n, h: ''};
    if (levels) {
      if (/^Traceback \(most recent call last\)/.test(plain)) trace = true;
      else if (trace && !/^\s/.test(plain)) { trace = false; line.c = 'le'; }
      if (trace) line.c = 'le';
    }
    if (pieces) line.h = pieces.map(([c, s]) => c ? `<span class="${c}">${esc(s)}</span>` : esc(s)).join('');
    else if (levels) {
      const t = LOG_TIME.exec(plain), rest = t ? plain.slice(t[0].length) : plain, l = LOG_LEVEL.exec(rest.slice(0, 80));
      if (t) line.h = `<span class="cc">${esc(t[0])}</span>`;
      if (l) {
        const lv = l[1] ? 'e' : l[2] ? 'w' : l[3] ? 'i' : 'd';
        line.c ||= {e: 'le', w: 'lw', d: 'lg'}[lv];
        line.h += esc(rest.slice(0, l.index)) + `<span class="lv${lv}">${l[0]}</span>` + esc(rest.slice(l.index + l[0].length));
      } else line.h += esc(rest);
    } else line.h = esc(plain);
    return line;
  };
}
function ansi(s, st) {
  const out = [], re = /\x1b\[([\d;]*)([A-Za-z])|\x1b[()][A-Z0-9]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b./g;
  const cls = () => [st.fg && 'a' + st.fg, st.bold && 'ab', st.dim && 'ad', st.it && 'ai', st.un && 'au'].filter(Boolean).join(' ');
  let at = 0, m;
  while ((m = re.exec(s))) {
    if (m.index > at) out.push([cls(), s.slice(at, m.index)]);
    at = m.index + m[0].length;
    if (m[2] !== 'm') continue;
    const codes = (m[1] || '0').split(';').map(Number);
    for (let k = 0; k < codes.length; k++) {
      const c = codes[k];
      if (c === 0) { st.fg = 0; st.bold = st.dim = st.it = st.un = false; }
      else if (c === 1) st.bold = true; else if (c === 2) st.dim = true; else if (c === 3) st.it = true; else if (c === 4) st.un = true;
      else if (c === 22) st.bold = st.dim = false; else if (c === 23) st.it = false; else if (c === 24) st.un = false;
      else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) st.fg = c;
      else if (c === 39) st.fg = 0;
      else if (c === 38 || c === 48) {
        if (codes[k + 1] === 5) { const n = codes[k + 2]; if (c === 38 && n < 16) st.fg = n < 8 ? 30 + n : 82 + n; k += 2; }
        else if (codes[k + 1] === 2) k += 4;
      }
    }
  }
  if (at < s.length) out.push([cls(), s.slice(at)]);
  return out;
}

// JSON laid out two spaces to a level, token by token: numbers stay exactly as written, however long.
function prettyJson(text) {
  const t = text.match(/"(?:[^"\\]|\\.)*"|[{}[\],:]|[^\s{}[\],:"]+/g) || [];
  let out = '', depth = 0;
  const nl = () => '\n' + '  '.repeat(depth);
  for (let i = 0; i < t.length; i++) {
    const x = t[i];
    if (x === '{' || x === '[') {
      if (t[i + 1] === (x === '{' ? '}' : ']')) { out += x + t[++i]; continue; }
      depth++; out += x + nl();
    } else if (x === '}' || x === ']') { depth--; out += nl() + x; }
    else if (x === ',') out += ',' + nl();
    else if (x === ':') out += ': ';
    else out += x;
  }
  return out;
}

// CSV and TSV: quoted cells may hold the separator, quotes doubled, and line breaks.
function parseTable(text, sep, most = Infinity) {
  const rows = [];
  let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) { if (ch !== '"') cell += ch; else if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; }
    else if (ch === '"' && !cell) q = true;
    else if (ch === sep) { row.push(cell); cell = ''; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; if (rows.length >= most) return rows; }
    else if (ch !== '\r') cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
const NUMBER = /^[-+]?(?:\d[\d,_]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?%?$/;
// A table is a grid whose columns are as wide as the widest cell in its first rows, so its rows can go in a batch
// at a time without the browser measuring every row again, and a column of numbers is set right.
function tableShape(rows, total) {
  // each column: its widest text, its padding and its border
  const cols = Math.max(...rows.slice(0, 50).map(r => r.length), 1), widths = [`calc(${String(total).length}ch + 26px)`], right = [];
  for (let j = 0; j < cols; j++) {
    let w = 2, all = 0, nums = 0;
    for (const r of rows.slice(0, 500)) w = Math.max(w, (r[j] || '').length);
    for (const r of rows.slice(1, 200)) { const v = (r[j] || '').trim(); if (v) { all++; if (NUMBER.test(v)) nums++; } }
    widths.push(`calc(${Math.min(w, 48)}ch + 26px)`);
    right.push(all > 0 && nums / all >= 0.8);
  }
  return {right, cols: widths.join(' ')};
}
const rowHtml = (r, n, t, cls = 'gr') => `<div class="${cls}"><span>${n}</span>${t.right.map((right, j) => `<span${right ? ' class="r"' : ''}>${esc(r[j] ?? '')}</span>`).join('')}</div>`;
const tableHtml = (rows, t) => `<div class="grid" style="--cols:${t.cols}">${rowHtml(rows[0] || [], '', t, 'gr gh')}${rows.slice(1).map((r, i) => rowHtml(r, i + 1, t)).join('')}</div>`;

// ---- Markdown: blocks, then the text inside them. Raw HTML in it shows as text, and only web and mail links link.
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
const indentOf = l => l.length - l.trimStart().length;
const markdown = text => mdBlocks(text.replace(/\r\n?/g, '\n').split('\n'));
function mdBlocks(lines) {
  let out = '', para = [], i = 0, m;
  const flush = () => { if (para.length) out += `<p>${mdInline(para.join('\n'))}</p>`; para = []; };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { flush(); i++; continue; }
    if ((m = /^\s{0,3}(`{3,}|~{3,})\s*([^`\s]*)/.exec(line))) {
      flush();
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(m[1]); i++) body.push(lines[i]);
      i++;
      out += `<pre class="code">${blockHtml(body.join('\n'), langKind(m[2]))}</pre>`;
    } else if ((m = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/.exec(line))) {
      flush(); out += `<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`; i++;
    } else if (para.length && /^\s{0,3}(=+|-+)\s*$/.test(line)) {
      const h = line.trim()[0] === '=' ? 1 : 2;
      out += `<h${h}>${mdInline(para.join('\n'))}</h${h}>`; para = []; i++;
    } else if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush(); out += '<hr>'; i++;
    } else if (/^\s{0,3}>/.test(line)) {
      flush();
      const body = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) body.push(lines[i++].replace(/^\s{0,3}> ?/, ''));
      out += `<blockquote>${mdBlocks(body)}</blockquote>`;
    } else if ((m = LIST.exec(line))) {
      flush();
      const indent = m[1].length, ordered = /\d/.test(m[2]), items = [];
      while (i < lines.length) {
        const l = lines[i], it = LIST.exec(l);
        if (it && indentOf(l) <= indent + 1) {
          if (/\d/.test(it[2]) !== ordered) break;
          items.push([it[3]]); i++;
        } else if (!l.trim()) {
          const next = lines[i + 1];
          if (next === undefined || !(indentOf(next) > indent || (LIST.test(next) && indentOf(next) <= indent + 1))) break;
          items[items.length - 1].push(''); i++;
        } else if (indentOf(l) > indent) { items[items.length - 1].push(l.slice(Math.min(indentOf(l), indent + 2))); i++; }
        else if (items[items.length - 1].at(-1) !== '' && !/^\s{0,3}(#|>|```|~~~)/.test(l)) { items[items.length - 1].push(l.trim()); i++; }
        else break;
      }
      const tag = ordered ? 'ol' : 'ul', start = ordered && parseInt(m[2]) !== 1 ? ` start="${parseInt(m[2])}"` : '';
      out += `<${tag}${start}>${items.map(([first, ...rest]) => {
        const task = /^\[([ xX])\]\s+/.exec(first);
        const head = task ? `<span class="task${task[1] === ' ' ? '' : ' done'}"></span>${mdInline(first.slice(task[0].length))}` : mdInline(first);
        return `<li${task ? ' class="tasked"' : ''}>${head}${rest.length ? mdBlocks(rest) : ''}</li>`;
      }).join('')}</${tag}>`;
    } else if (line.includes('|') && i + 1 < lines.length && lines[i + 1].includes('|') && TABLE_RULE.test(lines[i + 1])) {
      flush();
      const cells = l => l.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '').split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, '|'));
      const head = cells(line), align = cells(lines[i + 1]).map(c => c.endsWith(':') ? (c.startsWith(':') ? 'center' : 'right') : '');
      const td = (tag, c, j) => `<${tag}${align[j] ? ` style="text-align:${align[j]}"` : ''}>${mdInline(c)}</${tag}>`;
      out += `<table><thead><tr>${head.map((c, j) => td('th', c, j)).join('')}</tr></thead><tbody>`;
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        const r = cells(lines[i]);
        out += `<tr>${head.map((_, j) => td('td', r[j] || '', j)).join('')}</tr>`;
      }
      out += '</tbody></table>';
    } else { para.push(line.trim()); i++; }
  }
  flush();
  return out;
}
function mdInline(text) {
  const kept = [], keep = h => `\uE000${kept.push(h) - 1}\uE001`;
  let s = esc(text);
  s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (_, q, c) => keep(`<code>${c.trim()}</code>`));
  // a link's address ends at a space, a bracket or a quote. Escaped, a quote is &quot; and an ampersand &amp;.
  s = s.replace(/!\[([^\]]*)\]\(((?:[^)\s&]|&amp;)+)(?:\s+&quot;.*?&quot;)?\)/g, (_, alt, url) => keep(mdLink(url, `🖼 ${alt || 'picture'}`)));
  s = s.replace(/\[([^\]]+)\]\(((?:[^)\s&]|&amp;)+)(?:\s+&quot;.*?&quot;)?\)/g, (_, label, url) => keep(mdLink(url, emphasis(label))));
  s = s.replace(/&lt;((?:https?:\/\/|mailto:)[^\s&]+)&gt;|\bhttps?:\/\/(?:[^\s<&]|&amp;)*[^\s<.,;:!?)\]'"&]/g, (u, inner) => keep(mdLink(inner || u, inner || u)));
  s = emphasis(s).replace(/ {2,}\n|\\\n/g, '<br>');
  return s.replace(/\uE000(\d+)\uE001/g, (_, n) => kept[n] ?? '');
}
const emphasis = s => s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*|__(?=\S)([\s\S]*?\S)__/g, (_, a, b) => `<strong>${a ?? b}</strong>`)
  .replace(/(^|[^\w*])\*(?=\S)([^*]*?\S)\*(?![\w*])/g, '$1<em>$2</em>').replace(/(^|[^\w])_(?=\S)([^_]*?\S)_(?!\w)/g, '$1<em>$2</em>')
  .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
// url is already escaped, so it cannot leave its attribute
const mdLink = (url, label) => /^(?:https?:\/\/|mailto:)/i.test(url)
  ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>` : `<span class="nolink" title="${url}">${label}</span>`;

// ---- the card under a message
const TEXTS = new Map();  // attachment id -> the attachment, for the viewer
const PREVIEW_LINES = 8;
function snipHtml(a, big) {
  TEXTS.delete(a.id); TEXTS.set(a.id, a);
  if (TEXTS.size > 2000) TEXTS.delete(TEXTS.keys().next().value);
  const {preview, lines, more} = a.text, f = fileKind(a.name, preview);
  let body;
  if (f.kind === 'markdown') body = `<div class="md">${markdown(preview)}</div>`;
  else if (f.kind === 'csv' || f.kind === 'tsv') {
    const rows = parseTable(preview, f.kind === 'csv' ? ',' : '\t', PREVIEW_LINES);
    body = tableHtml(rows, tableShape(rows, rows.length));
  } else body = `<div class="code">${textLines(preview, f.kind).slice(0, PREVIEW_LINES).map(lineHtml).join('')}</div>`;
  const cut = more || preview.split('\n').length > PREVIEW_LINES;
  return html`<div class="snip${big ? ' big' : ''}" data-text="${a.id}" tabindex="0" title="Open ${a.name}"><div class="sh"><span class="fx ${FAMILY[f.kind] || 'code'}">${f.badge}</span><b>${a.name}</b><span class="k">${f.label}${lines ? ` · ${plural(lines, 'line')}` : ''} · ${bytes(a.size)}</span><button class="dl" data-file="${a.id}" data-name="${a.name}" title="Download ${a.name}">${new Safe(DOWNLOAD_ICON)}</button></div><div class="sb${cut ? ' more' : ''}">${new Safe(body)}</div></div>`;
}
const DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>';

// ---- the viewer
const FORMATTED = {markdown: 'Formatted', csv: 'Table', tsv: 'Table', json: 'Formatted'};
// Runs fn as soon as the page has answered what is waiting. A timer can wait far longer than asked, as when the
// browser is busy; a message does not.
const soon = (() => {
  const ch = new MessageChannel(), queue = [];
  ch.port1.onmessage = () => queue.shift()();
  return fn => { queue.push(fn); ch.port2.postMessage(0); };
})();
const TABLE_ROWS = 20000;  // rows of a CSV file shown as a table. All of them show as its source.
const BUILD_LINES = 100000;  // a file with more lines builds each part of the page only when it comes into view
const PART = 500;  // lines formatted, and built into the page, at a time
async function openText(id) {
  const a = TEXTS.get(id);
  if (!a) return;
  const f = fileKind(a.name, a.text.preview);
  const box = document.createElement('div');
  box.className = 'tv';
  box.innerHTML = html`<div class="tvh"><span class="fx ${FAMILY[f.kind] || 'code'}">${f.badge}</span><b title="${a.name}">${a.name}</b><span class="k">${f.label} · ${bytes(a.size)}</span><span class="grow"></span>${FORMATTED[f.kind] ? html`<nav class="seg"><button data-mode="formatted" class="on">${FORMATTED[f.kind]}</button><button data-mode="source">Source</button></nav>` : ''}<button class="btn" data-wrap title="Wrap long lines">Wrap</button><button class="btn" data-file="${a.id}" data-name="${a.name}">Download</button><button class="x" data-close title="Close (Esc)">×</button></div><div class="tvb"><div class="loading">Loading ${a.name}</div></div>`.s;
  overlay.open(box);
  const body = $('.tvb', box);
  let r;
  try { r = await api('view_text', {id}); } catch (e) { body.innerHTML = html`<div class="loading">${e.message}</div>`.s; return; }
  if (!box.isConnected) return;
  const text = r.text.replace(/\r\n/g, '\n');
  let mode = FORMATTED[f.kind] ? 'formatted' : 'source', wrap = ['text', 'log', 'markdown'].includes(f.kind), draws = 0;
  if (f.kind === 'json') { try { JSON.parse(text); } catch (_) { mode = 'source'; $('.seg', box).remove(); } }
  const count = (text.match(/\n/g) || []).length + (text && !text.endsWith('\n') ? 1 : 0);
  $('.tvh .k', box).textContent = `${f.label} · ${r.cut ? 'first ' : ''}${plural(count, 'line')} · ${bytes(a.size)}`;
  if (r.cut) box.insertAdjacentHTML('beforeend', html`<div class="tvn">Showing the first ${bytes(r.shown)} of ${bytes(r.size)}. Download the file for all of it.</div>`.s);
  const draw = () => {
    const now = ++draws, live = () => now === draws && box.isConnected;
    for (const b of $$('[data-mode]', box)) b.classList.toggle('on', b.dataset.mode === mode);
    $('[data-wrap]', box).classList.toggle('on', wrap);
    $('[data-wrap]', box).hidden = mode === 'formatted' && f.kind !== 'json';
    body.classList.toggle('doc', mode === 'formatted' && f.kind === 'markdown');
    body.scrollTop = 0;
    if (mode === 'formatted' && f.kind === 'markdown') {  // a long document goes in parts, each laid out only when in view
      const page = document.createElement('template'), md = document.createElement('div');
      page.innerHTML = markdown(text);
      md.className = 'md';
      const blocks = [...page.content.children];
      if (blocks.length <= 200) md.append(...blocks);
      else for (let i = 0; i < blocks.length; i += 100) md.appendChild(document.createElement('div')).append(...blocks.slice(i, i + 100));
      for (const part of md.children) if (blocks.length > 200) part.className = 'chunk';
      return body.replaceChildren(md);
    }
    if (mode === 'formatted' && (f.kind === 'csv' || f.kind === 'tsv')) {
      const rows = parseTable(text, f.kind === 'csv' ? ',' : '\t', TABLE_ROWS + 2), over = rows.length > TABLE_ROWS + 1;
      if (over) rows.length = TABLE_ROWS + 1;
      const t = tableShape(rows, rows.length);
      body.innerHTML = tableHtml(rows.slice(0, 1), t) + (over ? html`<div class="loading">The table shows the first ${num(TABLE_ROWS)} rows. Source shows all of them.</div>`.s : '');
      const grid = $('.grid', body);
      // PART rows at a time, for 30 ms at a time, so the first show at once and the page keeps answering
      let at = 1;
      const add = () => {
        if (!live()) return;
        const until = performance.now() + 30;
        for (; at < rows.length && performance.now() < until; at += PART) {
          grid.insertAdjacentHTML('beforeend', `<div class="chunk">${rows.slice(at, at + PART).map((row, i) => rowHtml(row, at + i, t)).join('')}</div>`);
        }
        if (at < rows.length) soon(add);
      };
      return add();
    }
    const shown = mode === 'formatted' && f.kind === 'json' ? prettyJson(text) : text;
    const total = shown === text ? count : (shown.match(/\n/g) || []).length + 1;
    const code = document.createElement('div');
    code.className = `code nums${f.kind === 'diff' ? ' diff' : ''}${wrap ? ' wrap' : ''}`;
    code.style.setProperty('--w', String(total).length + 'ch');
    // The lines are formatted PART at a time, in the background. Each part becomes page elements when it comes near
    // the screen, and every one does when the file has at most BUILD_LINES lines, so the browser's find sees them
    // all. Until then each holds the height its lines will take, so the scroll bar is right from the start.
    const parts = Array.from({length: Math.max(1, Math.ceil(total / PART))}, (_, i) => {
      const part = document.createElement('div'), height = Math.min(PART, total - i * PART) * 20 + 'px';
      part.className = 'chunk';
      part.i = i;
      part.style.height = height;
      part.style.setProperty('contain-intrinsic-block-size', 'auto ' + height);
      return part;
    });
    code.append(...parts);
    body.replaceChildren(code);
    const made = [], wanted = new Set(), every = total <= BUILD_LINES;
    const build = i => {
      const part = parts[i];
      if (part.built || made[i] == null) return;
      part.built = true;
      part.innerHTML = made[i];
      part.style.height = '';
      near.unobserve(part);
    };
    const near = new IntersectionObserver(seen => {
      for (const e of seen) if (e.isIntersecting) { wanted.add(e.target.i); build(e.target.i); }
    }, {root: body, rootMargin: '1500px 0px'});
    parts.forEach(part => near.observe(part));
    const batches = lineBatches(shown, f.kind, PART);
    const step = () => {  // formats for 30 ms or builds one part, then lets the page answer
      if (!live()) return near.disconnect();
      const until = performance.now() + 30;
      do {
        const next = batches.next();
        if (next.done) return every && near.disconnect();
        const i = made.push(next.value.map(lineHtml).join('')) - 1;
        if (every || wanted.has(i)) { build(i); break; }
      } while (performance.now() < until);
      soon(step);
    };
    step();
  };
  box.addEventListener('click', e => {
    const t = e.target.closest('[data-mode], [data-wrap], [data-close]');
    if (!t) return;
    if (t.dataset.close != null) return overlay.close();
    if (t.dataset.wrap != null) { wrap = !wrap; const c = $('.code', body); if (c) { c.classList.toggle('wrap', wrap); t.classList.toggle('on', wrap); } return; }
    if (t.dataset.mode !== mode) { mode = t.dataset.mode; draw(); }
  });
  draw();
}

// One message. prev decides whether it continues the message before (same sender, within five minutes).
function messageHtml(m, prev, o = {}) {
  if (m.kind !== 'message') return systemHtml(m);
  const u = user(m.from);
  const cont = !o.tree && !o.flat && prev && prev.kind === 'message' && prev.from === m.from && prev.chat === m.chat && !m.parent &&
    ms(m.at) - ms(prev.at) < 300000 && dayOf(m.at) === dayOf(prev.at);
  const as = S.route.as;
  const mentionsMe = as && m.from !== as && (m.mentions || []).includes(as);
  const text = m.text || '';
  const long = !o.whole && (text.length > 1500 || text.split('\n').length > 18);
  const reactions = Object.entries(m.reactions || {});
  const depth = o.tree ? Math.min(m.depth || 0, 12) : 0;
  return html`<div class="msg${cont ? ' cont' : ''}${mentionsMe ? ' mentions-me' : ''}" data-seq="${m.seq}" data-chat="${m.chat}" data-id="${m.id}" data-time="${clock(m.at)}"${o.tree ? html` style="--d:${depth}"` : ''}>
${avatar(m.from)}<div class="body"><div class="head"><span class="name" data-person="${m.from}">${u.name}</span>${u.name !== m.from ? html`<span class="uid">${m.from}</span>` : ''}<time title="${full(m.at)}">${o.tree || o.showChat ? ago(m.at) : clock(m.at)}</time>${o.showChat ? html`<span class="chat" data-open="${m.chat}" data-at="${m.seq}">in ${chatTitle(m.chat)}</span>` : ''}${m.edited_at ? html`<span class="edited" title="Edited ${full(m.edited_at)}">(edited)</span>` : ''}</div>
${m.parent && !o.tree ? html`<div class="quote" data-jump="${m.parent.seq}" data-chat="${m.chat}" title="Go to the message it answers"><b>${name(m.parent.from)}</b><span>${m.parent.deleted ? 'deleted message' : m.parent.text || (m.parent.files ? 'a file' : '')}</span></div>` : ''}
${m.deleted ? html`<div class="text deleted">This message was deleted.</div>` : html`<div class="text${long ? ' long' : ''}">${rich(text, o.mark)}</div>${long ? html`<span class="more" data-more>Show all</span>` : ''}`}
${filesHtml(m.attachments)}
${reactions.length ? html`<div class="reacts">${reactions.map(([e, us]) => html`<span class="react" data-tip="${us.map(name).join(', ')}">${EMOJI[e] || e} ${us.length}</span>`)}</div>` : ''}
${m.replies && !o.tree ? html`<div class="foot"><span class="replies" data-thread="${m.id}">${plural(m.replies, 'reply', 'replies')}</span></div>` : ''}
${o.below ? html`<div class="foot"><span class="toggle" data-fold title="Fold or unfold everything below">▾ ${plural(o.below, 'reply', 'replies')} below</span></div>` : ''}
</div><div class="actions">${o.tree ? '' : html`<button data-thread="${m.id}">Thread</button>`}<button data-knew="${m.id}" title="How far ${u.name} had marked messages read in each chat when sending this">What had ${first(m.from)} read?</button><button data-receipts="${m.id}">Read by</button><button data-copy="${m.id}" title="Copy the message id">${m.id}</button></div></div>`;
}

function systemHtml(e) {
  const who = html`<b>${name(e.from)}</b>`;
  const people = us => {
    const names = (us || []).map(name);
    return names.length > 8 ? html`<b>${names.slice(0, 8).join(', ')}</b> and ${names.length - 8} more` : html`<b>${names.join(', ')}</b>`;
  };
  const what = {
    created: () => html`${who} made this chat${e.name ? html` as <b>#${e.name}</b>` : ''} with ${people(e.members)}${e.public ? ', public' : ''}`,
    added: () => e.users && e.users.length === 1 && e.users[0] === e.from ? html`${who} joined` : html`${who} added ${people(e.users)}`,
    removed: () => e.users && e.users.length === 1 && e.users[0] === e.from ? html`${who} left` : html`${who} removed ${people(e.users)}`,
    renamed: () => html`${who} renamed it <b>#${e.name}</b>`,
    managers: () => html`${who} ${e.added ? html`made ${people(e.added)} ${e.added.length === 1 ? 'a manager' : 'managers'}` : ''}${e.added && e.removed ? ' and ' : ''}${e.removed ? html`removed ${people(e.removed)} as ${e.removed.length === 1 ? 'a manager' : 'managers'}` : ''}`,
    described: () => e.description ? html`${who} changed the description to <b>${e.description}</b>` : html`${who} removed the description`,
    visibility: () => html`${who} made it ${e.public ? 'public' : 'private'}`,
    archived: () => html`${who} archived it`,
    unarchived: () => html`${who} unarchived it`,
  }[e.kind];
  return html`<div class="sys" data-seq="${e.seq}"><span>${what ? what() : e.kind}</span><span class="stamp" title="${full(e.at)}">${clock(e.at)}</span></div>`;
}

// ---------------------------------------------------------------------- one chat

class ChatView {
  constructor(main, chatId, o) {
    this.id = chatId; this.o = o; this.msgs = []; this.first = this.last = false; this.positions = {}; this.fresh = 0;
    this.main = main;
    const c = S.chats.get(chatId);
    main.innerHTML = html`<div class="mhead"></div><div class="banners"></div><div class="scroll"><div class="loading top" hidden>Loading earlier messages</div><div class="msgs"></div><div class="loading bottom" hidden>Loading</div></div><button class="newpill" hidden></button>`;
    this.head = $('.mhead', main); this.scroll = $('.scroll', main); this.box = $('.msgs', main); this.pill = $('.newpill', main);
    this.renderHead();
    if (!c) { this.box.innerHTML = html`<div class="empty">There is no chat ${chatId}.</div>`; return; }
    this.scroll.addEventListener('scroll', () => this.onScroll(), {passive: true});
    this.pill.addEventListener('click', () => this.toBottom(true));
    this.load();
  }
  renderHead() {
    const c = S.chats.get(this.id), r = S.route;
    if (!c) return;
    const kind = c.kind === 'channel' ? (c.public ? 'public' : 'private') : c.kind;
    this.head.innerHTML = html`<span class="ico ${c.kind}">${KIND[c.kind]}</span><h1 title="${title(c, null)}">${c.name ? '#' + c.name : title(c, null)}</h1><span class="pill ${kind}">${kind}</span>
${c.archived ? html`<span class="pill archived">archived</span>` : ''}
<span class="sub"><span class="link" data-members="${c.id}">${plural(c.members.length, 'member')}</span> · ${plural(c.messages, 'message')} · made by ${name(c.created_by)} ${ago(c.created_at)}</span>
<div class="grow"></div><button class="btn" data-goto="board">Open as a board</button>
${c.description ? html`<div class="about" title="${c.description}">${c.description}</div>` : ''}`;
    const banners = $('.banners', this.main);
    const notIn = r.as && !c.members.includes(r.as);
    banners.innerHTML = notIn ? html`<div class="banner">${name(r.as)} is not in this chat, so ${first(r.as)} cannot see any of it.</div>` : '';
  }
  async load(params) {
    const o = this.o, as = S.route.as, c = S.chats.get(this.id);
    const pos = as && S.as && S.as.positions[this.id];
    const start = o.jump ? {around: o.jump} : o.had === 0 ? {after: 0} : pos != null && c.last && pos < c.last.seq ? {around: pos + 1} : {};
    const r = await api('view_messages', params || {chat: this.id, limit: 150, ...start});
    if (this.dead) return;
    this.msgs = r.messages; this.first = r.first; this.last = r.last; this.positions = r.positions;
    this.draw();
    const d = this.divider();
    if (o.had === 0 && !params) this.scroll.scrollTop = 0;
    else if (o.jump && !params) this.reveal(o.jump);
    else if (d && d.cls === 'now' && !params) this.box.querySelector('.divider')?.scrollIntoView({block: 'center'});
    else this.toBottom();
  }
  divider() {
    const r = S.route;
    if (this.o.had != null && this.o.knew) {
      const who = S.knew && S.knew.id === this.o.knew ? S.knew : null;
      const what = this.o.had ? 'had read up to here' : 'had read nothing here';
      return {at: this.o.had, cls: 'then', label: who ? `${name(who.user)} ${what} when sending at ${clockS(who.at)}` : `${what} at that moment`};
    }
    if (r.as && S.as && S.as.positions[this.id] != null) {
      const pos = S.as.positions[this.id];
      const lastSeq = this.msgs.length ? this.msgs[this.msgs.length - 1].seq : 0;
      if (pos < lastSeq) return {at: pos, cls: 'now', label: `${name(r.as)} has read up to here`};
    }
    return null;
  }
  rows(list, prev) {
    const d = this.divider();
    let out = '', before = prev ? prev.seq : this.first ? 0 : null;  // the last message drawn above
    for (const m of list) {
      if (!prev || dayOf(prev.at) !== dayOf(m.at)) out += html`<div class="daysep"><span>${dayName(m.at)}</span></div>`.s;
      if (d && m.kind === 'message' && before != null && before <= d.at && m.seq > d.at) out += html`<div class="divider ${d.cls}">${d.label}</div>`.s;
      if (m.kind === 'message') before = m.seq;
      out += messageHtml(m, prev).s;
      prev = m;
    }
    return out;
  }
  draw() {
    const top = this.scroll.scrollTop;
    this.box.innerHTML = this.rows(this.msgs, null) || (this.last && this.first ? '<div class="empty">No messages yet.</div>' : '');
    this.scroll.scrollTop = top;
    $('.loading.top', this.main).hidden = this.first;
  }
  atBottom() { return this.scroll.scrollHeight - this.scroll.scrollTop - this.scroll.clientHeight < 80; }
  toBottom(fetch) {
    if (fetch && !this.last) { this.o.jump = null; return this.load({chat: this.id, limit: 150}); }
    this.scroll.scrollTop = this.scroll.scrollHeight;
    this.fresh = 0; this.pill.hidden = true;
  }
  reveal(seq) {
    const el = this.box.querySelector(`.msg[data-seq="${seq}"]`) || [...this.box.querySelectorAll('.msg')].find(e => +e.dataset.seq >= seq);
    if (!el) return;
    el.scrollIntoView({block: 'center'});
    el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
  }
  jump(seq) {
    if (this.msgs.some(m => m.seq === seq)) return this.reveal(seq);
    this.o.jump = seq;
    this.load({chat: this.id, around: seq, limit: 150}).then(() => this.reveal(seq));
  }
  async onScroll() {
    if (this.busy) return;
    const s = this.scroll;
    if (s.scrollTop < 500 && !this.first && this.msgs.length) {
      this.busy = true;
      const r = await api('view_messages', {chat: this.id, before: this.msgs.find(m => m.kind === 'message')?.seq ?? this.msgs[0].seq, limit: 200});
      if (this.dead) return;
      const h = s.scrollHeight, t = s.scrollTop;
      const have = new Set(this.msgs.map(m => m.seq));
      this.msgs = [...r.messages.filter(m => !have.has(m.seq)), ...this.msgs];
      this.first = r.first;
      if (this.msgs.length > 700) { this.msgs = this.msgs.slice(0, 600); this.last = false; }
      this.draw();
      s.scrollTop = t + (s.scrollHeight - h);
      this.busy = false;
    } else if (s.scrollHeight - s.scrollTop - s.clientHeight < 500 && !this.last && this.msgs.length) {
      this.busy = true;
      const r = await api('view_messages', {chat: this.id, after: this.msgs[this.msgs.length - 1].seq, limit: 200});
      if (this.dead) return;
      const have = new Set(this.msgs.map(m => m.seq));
      this.msgs = [...this.msgs, ...r.messages.filter(m => !have.has(m.seq))];
      this.last = r.last;
      if (this.msgs.length > 700) {
        const h = s.scrollHeight, t = s.scrollTop;
        this.msgs = this.msgs.slice(-600); this.first = false;
        this.draw();
        s.scrollTop = t - (h - s.scrollHeight);
      } else this.draw();
      this.busy = false;
    }
    if (this.atBottom()) { this.fresh = 0; this.pill.hidden = true; }
  }
  onEvents(ev) {
    if (ev.chats.has(this.id)) this.renderHead();
    const mine = ev.lines.filter(m => m.chat === this.id);
    const changed = ev.changes.filter(m => m.chat === this.id);
    for (const m of changed) {
      const i = this.msgs.findIndex(x => x.seq === m.seq);
      if (i < 0) continue;
      this.msgs[i] = m;
      const el = this.box.querySelector(`.msg[data-seq="${m.seq}"]`);
      if (el) el.outerHTML = messageHtml(m, this.msgs[i - 1]).s;
    }
    for (const e of ev.reads) if (e.chat === this.id) this.positions[e.user] = Math.max(this.positions[e.user] || 0, e.upto);
    if (!mine.length || !this.last) { if (mine.length) this.pillCount(mine.length); return; }
    const have = new Set(this.msgs.map(m => m.seq));
    const add = mine.filter(m => !have.has(m.seq));
    if (!add.length) return;
    const stick = this.atBottom();
    const parents = new Set(add.map(m => m.parent && m.parent.seq).filter(Boolean));
    const prev = this.msgs[this.msgs.length - 1];
    this.msgs.push(...add);
    for (const m of this.msgs) if (parents.has(m.seq)) m.replies = (m.replies || 0) + add.filter(a => a.parent && a.parent.seq === m.seq).length;
    if (parents.size || this.divider()) this.draw();
    else {
      this.box.querySelector('.empty')?.remove();
      this.box.insertAdjacentHTML('beforeend', this.rows(add, prev));
    }
    if (stick) {
      if (this.msgs.length > 700) { this.msgs = this.msgs.slice(-600); this.first = false; this.draw(); }
      this.toBottom();
    } else this.pillCount(add.length);
  }
  pillCount(n) {
    this.fresh += n;
    this.pill.hidden = false;
    this.pill.textContent = `${plural(this.fresh, 'new message')} ↓`;
  }
  onAs() { this.renderHead(); this.draw(); }
  destroy() { this.dead = true; }
}

// ---------------------------------------------------------------------- lists of messages from many chats

class ListView {
  constructor(main, heading) {
    this.main = main; this.msgs = [];
    main.innerHTML = html`<div class="mhead">${heading}</div><div class="scroll"><div class="msgs results"></div><div class="loading" hidden>Loading</div></div>`;
    this.scroll = $('.scroll', main); this.box = $('.msgs', main); this.wait = $('.loading', main);
  }
  draw(o = {}) {
    let prev = null, out = '';
    for (const m of this.msgs) { out += messageHtml(m, prev, {showChat: true, flat: this.flat, ...o}).s; prev = m; }
    this.box.innerHTML = out || html`<div class="empty">${this.none || 'Nothing here.'}</div>`.s;
  }
  destroy() { this.dead = true; }
}

class ActivityView extends ListView {
  constructor(main) {
    const as = S.route.as;
    super(main, html`<span class="ico">≋</span><h1>All activity</h1><span class="sub">${as ? `in ${name(as)}'s chats, ` : 'every chat, '}newest at the bottom, live</span>`);
    this.none = 'No messages yet. New ones appear here as they are written.';
    this.load();
  }
  async load() {
    this.wait.hidden = false;
    const r = await api('view_search', {limit: 150, as: S.route.as || undefined});
    if (this.dead) return;
    this.wait.hidden = true;
    this.msgs = r.messages.reverse();
    this.draw();
    this.scroll.scrollTop = this.scroll.scrollHeight;
  }
  onEvents(ev) {
    let add = ev.lines.filter(m => m.kind === 'message');
    if (S.route.as) add = add.filter(m => (S.chats.get(m.chat)?.members || []).includes(S.route.as));
    if (!add.length) return;
    const stick = this.scroll.scrollHeight - this.scroll.scrollTop - this.scroll.clientHeight < 80;
    const prev = this.msgs[this.msgs.length - 1];
    this.msgs.push(...add);
    if (this.msgs.length > 400) { this.msgs = this.msgs.slice(-300); this.draw(); }
    else {
      this.box.querySelector('.empty')?.remove();
      let p = prev, out = '';
      for (const m of add) { out += messageHtml(m, p, {showChat: true}).s; p = m; }
      this.box.insertAdjacentHTML('beforeend', out);
    }
    if (stick) this.scroll.scrollTop = this.scroll.scrollHeight;
  }
}

class UnreadView extends ListView {
  constructor(main) {
    const as = S.route.as;
    super(main, html`<span class="ico">✉</span><h1>What ${name(as)} would read now</h1><span class="sub">exactly what <code>swarmchat read</code> would hand ${first(as)}, oldest first. Nothing is marked read.</span>`);
    this.none = `Nothing unread: ${first(as)} has read everything.`;
    this.flat = true;
    this.draw2();
  }
  draw2() {
    this.msgs = S.as ? S.as.unread : [];
    this.draw();
    if (S.as && S.as.unreadLeft > this.msgs.length) this.box.insertAdjacentHTML('beforeend', html`<div class="note">${num(S.as.unreadLeft - this.msgs.length)} more after these.</div>`.s);
  }
  onAs() { this.draw2(); }
}

// Search words, and from:, in:, mentions:, has:file, after: and before:.
function parseQuery(q) {
  const p = {}, words = [];
  for (const m of q.match(/(\w+):"[^"]*"|(\w+):\S+|"[^"]*"|\S+/g) || []) {
    const f = m.match(/^(from|in|mentions|has|after|before):"?([^"]*)"?$/i);
    if (!f) { words.push(m.replace(/^"|"$/g, '')); continue; }
    const key = f[1].toLowerCase(), v = f[2];
    if (key === 'from') (p.from ||= []).push(findUser(v) || v.replace(/^@/, '').toLowerCase());
    else if (key === 'mentions') p.mentions = findUser(v) || v.replace(/^@/, '').toLowerCase();
    else if (key === 'in') { const c = findChat(v); if (c) (p.chats ||= []).push(c.id); else p.bad = `No chat called ${v}`; }
    else if (key === 'has') p.files = /^files?|attachments?$/i.test(v) || p.files;
    else { const t = Date.parse(v); if (!isNaN(t)) p[key] = new Date(t).toISOString(); }
  }
  p.text = words.join(' ');
  return p;
}
function findUser(v) {
  v = v.replace(/^@/, '').toLowerCase();
  if (S.users.has(v)) return v;
  const hit = [...S.users.values()].find(u => u.name.toLowerCase() === v || u.name.toLowerCase().split(/\s+/)[0] === v);
  return hit ? hit.id : null;
}
function findChat(v) {
  v = v.toLowerCase();
  if (S.chats.has(v)) return S.chats.get(v);
  const n = v.replace(/^#/, '');
  return [...S.chats.values()].find(c => c.name && c.name.toLowerCase() === n) || null;
}

class SearchView extends ListView {
  constructor(main, q) {
    const p = parseQuery(q);
    super(main, html`<span class="ico">⌕</span><h1>“${q}”</h1><span class="sub"></span>`);
    this.p = p; this.q = q; this.flat = true;
    this.none = p.bad || 'No messages match.';
    this.more = null;
    this.load();
  }
  async load(until) {
    const p = this.p;
    if (p.bad) return this.draw();
    this.wait.hidden = false;
    const t = performance.now();
    const r = await api('view_search', {text: p.text, from: p.from, mentions: p.mentions, chats: p.chats, files: p.files,
      after: p.after, before: p.before, as: S.route.as || undefined, limit: 100, until});
    if (this.dead) return;
    this.wait.hidden = true;
    this.msgs = until ? [...this.msgs, ...r.messages] : r.messages;
    this.draw({mark: p.text});
    $('.sub', this.main).textContent = `${this.msgs.length}${r.until ? '+' : ''} messages, newest first${S.route.as ? `, in ${name(S.route.as)}'s chats` : ''} · ${Math.round(performance.now() - t)} ms`;
    if (r.until) {
      const more = document.createElement('div');
      more.className = 'loading';
      more.innerHTML = '<button class="btn">Show more</button>';
      more.firstChild.onclick = () => { more.remove(); this.load(r.until); };
      this.box.append(more);
    }
  }
}

// ---------------------------------------------------------------------- one person

class PersonView {
  constructor(main, id) {
    this.main = main; this.id = id;
    const u = user(id);
    const chats = [...S.chats.values()].filter(c => c.members.includes(id)).sort((a, b) => (b.sent[id] || 0) - (a.sent[id] || 0));
    const fields = Object.entries(u.fields || {});
    main.innerHTML = html`<div class="scroll"><div class="person">
<div class="card">${avatar(id, 'lg')}<div><h1>${u.name}</h1><div class="uid">${id}${u.removed ? ' · removed' : ''}${u.status ? html` · <i>${u.status}</i>` : ''}</div>
${fields.length ? html`<div class="fields">${fields.map(([k, v]) => html`<span class="field"><i>${k}</i> ${v}</span>`)}</div>` : ''}</div>
<div class="grow"></div>${u.removed ? '' : html`<button class="btn primary" data-see-as="${id}">See as ${first(id)}</button>`}</div>
<div class="kpis"><div class="kpi"><b>${num(S.sent.get(id) || 0)}</b><span>messages sent</span></div><div class="kpi"><b>${num(chats.length)}</b><span>chats</span></div>
<div class="kpi"><b>${S.last.get(id) ? ago(S.last.get(id)) : 'never'}</b><span>last message</span></div><div class="kpi"><b>${num(chats.filter(c => c.kind === 'channel').length)}</b><span>channels</span></div></div>
<h3>Chats, by how much ${first(id)} wrote there</h3><div class="chips">${chats.map(c => html`<span class="chip" data-open="${c.id}"><span class="ico ${c.kind}">${KIND[c.kind]}</span>${title(c, id)}<span class="n">${num(c.sent[id] || 0)} of ${num(c.messages)}</span></span>`)}${chats.length ? '' : html`<span class="note">In no chat.</span>`}</div>
<h3>Latest messages</h3><div class="msgs results"><div class="loading">Loading</div></div></div></div>`;
    this.load();
  }
  async load() {
    const r = await api('view_search', {from: [this.id], limit: 40});
    if (this.dead) return;
    let prev = null, out = '';
    for (const m of r.messages) { out += messageHtml(m, prev, {showChat: true, flat: true}).s; prev = m; }
    $('.msgs', this.main).innerHTML = out || '<div class="note">No messages yet.</div>';
  }
  destroy() { this.dead = true; }
}

// ---------------------------------------------------------------------- boards

const ARROW = '<svg viewBox="0 0 16 16"><path d="M8 2.5 14 9H10v4.5H6V9H2z" fill="currentColor"/></svg>';
function postHtml(p, o = {}) {
  const lines = (p.text || '').split('\n');
  const head = p.deleted ? 'This post was deleted.' : (lines[0] || (p.attachments || []).map(a => a.name).join(', ') || '(no text)').slice(0, 300);
  const rest = p.deleted ? '' : lines.slice(1).join('\n').trim();
  const c = S.chats.get(p.chat);
  return html`<article class="post${p.deleted ? ' deleted' : ''}" data-post="${p.id}">
<div class="votes${p.votes ? ' has' : ''}" title="${p.votes ? Object.entries(p.reactions || {}).map(([e, us]) => `${EMOJI[e] || e} ${us.map(name).join(', ')}`).join('\n') : 'No reactions'}">${new Safe(ARROW)}<span>${num(p.votes)}</span></div>
<div class="main"><div class="ptitle">${head}</div>${rest ? html`<div class="pbody">${o.whole ? rich(rest) : rest}</div>` : ''}
${filesHtml(p.attachments, true)}
<div class="pmeta">${avatar(p.from, 'sm')}<b data-person="${p.from}">${name(p.from)}</b>${o.showChat ? html`<span class="chat" data-open="${p.chat}">${title(c)}</span>` : ''}<span title="${full(p.at)}">${ago(p.at)}</span><span>💬 ${plural(p.comments, 'comment')}</span>${p.comments ? html`<span title="${full(p.active_at)}">last ${ago(p.active_at)}</span>` : ''}</div></div></article>`;
}

class BoardView {
  constructor(main, chatId, sort) {
    this.main = main; this.chat = chatId; this.sort = sort; this.posts = []; this.fresh = 0;
    const c = chatId ? S.chats.get(chatId) : null;
    const scope = c ? (c.name ? '#' + c.name : title(c, null)) : 'Front page';
    const sub = c ? `${plural(c.members.length, 'member')} · ${plural(c.messages, 'message')}${c.kind === 'channel' ? (c.public ? ' · public' : ' · private') : ''}${c.archived ? ' · archived' : ''}`
      : `top-level messages from every channel and group${S.route.as ? `, in ${name(S.route.as)}'s chats` : ''}`;
    main.innerHTML = html`<div class="mhead"><span class="ico ${c ? c.kind : ''}">${c ? KIND[c.kind] : '★'}</span><h1>${scope}</h1><span class="sub">${sub}</span><div class="grow"></div>
<div class="sorts">${['hot', 'new', 'top', 'active'].map(s => html`<button data-sort="${s}" class="${s === sort ? 'on' : ''}" title="${{hot: 'Votes and comments, weighed by age', new: 'Newest posts first', top: 'Most votes and comments', active: 'Newest comment first'}[s]}">${s[0].toUpperCase() + s.slice(1)}</button>`)}</div>
${c ? html`<button class="btn" data-goto="chat">Open as a chat</button>` : ''}
${c && c.description ? html`<div class="about" title="${c.description}">${c.description}</div>` : ''}</div><div class="scroll"><div class="posts"><div class="loading">Loading</div></div></div><button class="newpill" hidden></button>`;
    this.box = $('.posts', main);
    this.pill = $('.newpill', main);
    this.pill.onclick = () => { this.pill.hidden = true; this.fresh = 0; this.load(); };
    this.load();
  }
  async load(more) {
    const r = await api('view_posts', {chat: this.chat || undefined, sort: this.sort, limit: 40, offset: more ? this.posts.length : 0, as: S.route.as || undefined});
    if (this.dead) return;
    this.posts = more ? [...this.posts, ...r.posts] : r.posts;
    this.box.innerHTML = this.posts.map(p => postHtml(p, {showChat: !this.chat}).s).join('') ||
      html`<div class="empty">No posts yet. A post is any message that does not reply to another.</div>`.s;
    if (r.more) {
      const b = document.createElement('div');
      b.className = 'loading';
      b.innerHTML = '<button class="btn">Show more</button>';
      b.firstChild.onclick = () => this.load(true);
      this.box.append(b);
    }
  }
  onEvents(ev) {
    const n = ev.lines.filter(m => m.kind === 'message' && !m.parent && (this.chat ? m.chat === this.chat : S.chats.get(m.chat)?.kind !== 'direct')).length;
    if (!n) return;
    this.fresh += n;
    this.pill.hidden = false;
    this.pill.textContent = `${plural(this.fresh, 'new post')}, show them`;
  }
  destroy() { this.dead = true; }
}

class PostView {
  constructor(main, id) {
    this.main = main; this.id = id;
    main.innerHTML = html`<div class="mhead"><button class="btn" data-back>← Back</button><h1></h1><div class="grow"></div><button class="btn" data-open-post-chat>Open in the chat</button></div><div class="scroll"><div class="thread-page"><div class="loading">Loading</div></div></div>`;
    this.load();
  }
  async load() {
    const r = await api('view_thread', {id: this.id});
    if (this.dead) return;
    const root = r.messages[0];
    this.chat = r.chat;
    $('h1', this.main).textContent = chatTitle(r.chat);
    const comments = r.messages.slice(1);
    const votes = Object.values(root.reactions || {}).reduce((n, us) => n + us.length, 0);
    $('.thread-page', this.main).innerHTML = postHtml({...root, votes, comments: comments.length, active_at: comments.length ? comments[comments.length - 1].at : root.at}, {whole: true}).s +
      html`<div class="comments tree">${treeHtml(comments)}</div>`.s;
  }
  onEvents(ev) {
    if (ev.lines.some(m => m.chat === this.chat && m.parent) || ev.changes.some(m => m.chat === this.chat)) this.reload();
  }
  reload = throttle(() => { if (!this.dead) this.load(); }, 1000);
  destroy() { this.dead = true; }
}

// A thread as a tree: each reply under what it answers, with a control to fold any branch.
function treeHtml(list) {
  let out = '';
  list.forEach((m, i) => {
    const kids = list.slice(i + 1).findIndex(x => x.depth <= m.depth);
    const below = kids < 0 ? list.length - i - 1 : kids;
    out += messageHtml(m, null, {tree: true, below}).s;
  });
  return new Safe(out);
}
function fold(el) {
  const msg = el.closest('.msg');
  const d = +getComputedStyle(msg).getPropertyValue('--d');
  const closing = !msg.classList.contains('folded');
  msg.classList.toggle('folded', closing);
  el.textContent = (closing ? '▸' : '▾') + el.textContent.slice(1);
  for (let n = msg.nextElementSibling; n && +getComputedStyle(n).getPropertyValue('--d') > d; n = n.nextElementSibling) n.hidden = closing;
}

// ---------------------------------------------------------------------- the pane on the right

class ThreadPane {
  constructor(id) {
    this.id = id;
    const pane = $('#pane');
    pane.innerHTML = html`<div class="phead"><h2>Thread</h2><button class="close" data-close-pane title="Close">×</button></div><div class="scroll"><div class="tree msgs"><div class="loading">Loading</div></div></div>`;
    this.box = $('.tree', pane);
    this.load();
  }
  async load() {
    try {
      const r = await api('view_thread', {id: this.id});
      if (this.dead) return;
      this.chat = r.chat;
      $('#pane h2').textContent = `Thread in ${chatTitle(r.chat)}`;
      this.box.innerHTML = treeHtml(r.messages).s;
      const me = this.box.querySelector(`.msg[data-id="${CSS.escape(this.id)}"]`);
      if (me && me !== this.box.firstElementChild) { me.scrollIntoView({block: 'center'}); me.classList.add('flash'); }
    } catch (e) { this.box.innerHTML = html`<div class="note">${e.message}</div>`.s; }
  }
  onEvents(ev) { if (ev.lines.some(m => m.chat === this.chat && m.parent) || ev.changes.some(m => m.chat === this.chat)) this.reload(); }
  reload = throttle(() => { if (!this.dead) this.load(); }, 1000);
  destroy() { this.dead = true; }
}

class KnewPane {
  constructor(id) {
    this.id = id;
    const pane = $('#pane');
    pane.innerHTML = html`<div class="phead"><h2>What had they read?</h2><button class="close" data-close-pane title="Close">×</button></div><div class="scroll"><div class="knew"><div class="loading">Loading</div></div></div>`;
    this.box = $('.knew', pane);
    this.load();
  }
  async load() {
    try {
      const r = await api('view_knew', {id: this.id});
      if (this.dead) return;
      S.knew = {id: this.id, user: r.user, at: r.at, chats: r.chats};
      $('#pane h2').textContent = `What ${name(r.user)} had read`;
      const rows = r.chats.map(k => {
        const c = S.chats.get(k.chat);
        return html`<div class="k" data-had="${k.chat}" data-upto="${k.upto}"><span class="t"><span class="ico ${c ? c.kind : ''}">${c ? KIND[c.kind] : ''}</span> ${title(c, r.user)}${k.chat === r.message.chat ? ' · here' : ''}</span>
<span class="u ${k.unseen ? 'some' : 'zero'}">${k.unseen ? `${num(k.unseen)} not seen` : 'all seen'}</span>
<span class="s">read ${num(k.read)} of ${plural(k.messages, 'message')}${k.read_at ? ` · last read at ${clockS(k.read_at)}` : ' · had read nothing'}</span></div>`;
      });
      this.box.innerHTML = html`<div class="note">When ${name(r.user)} sent this at <b>${clockS(r.at)}</b>, this is how far ${first(r.user)} had marked messages read in each chat. It counts only what was marked read: <code>--peek</code>, threads, previews and searches leave no trace, so ${first(r.user)} may have seen more.</div>
<div class="msgs">${messageHtml(r.message, null, {tree: true})}</div>${rows}`.s;
      if (S.view instanceof ChatView && S.view.o.knew === this.id) S.view.draw();
    } catch (e) { this.box.innerHTML = html`<div class="note">${e.message}</div>`.s; }
  }
  destroy() { this.dead = true; }
}

// ---------------------------------------------------------------------- overlays: search, pickers, popovers

const overlay = {
  el: null,
  open(content, anchor) {
    this.el = $('#overlay');
    this.el.innerHTML = '<div class="shade"></div>';
    this.el.append(content);
    this.el.classList.add('on');
    if (anchor) {
      const r = anchor.getBoundingClientRect();
      content.style.top = Math.min(r.bottom + 6, innerHeight - 200) + 'px';
      content.style.left = Math.max(8, Math.min(r.left, innerWidth - 400)) + 'px';
    }
    $('.shade', this.el).onclick = () => this.close();
  },
  close() { if (this.el) { this.el.classList.remove('on'); this.el.innerHTML = ''; } },
  get on() { return this.el && this.el.classList.contains('on'); },
};

function openSearch(initial = '') {
  const box = document.createElement('div');
  box.className = 'palette';
  box.innerHTML = html`<input placeholder="Search messages, people and chats" spellcheck="false"><div class="res"></div><div class="hint">Enter searches every message. Narrow it with <code>from:alice</code> <code>in:#ops</code> <code>mentions:bob</code> <code>has:file</code> <code>after:2026-10-01</code> <code>before:…</code></div>`.s;
  overlay.open(box);
  const input = $('input', box), res = $('.res', box);
  let items = [], sel = 0;
  const render = () => {
    const q = input.value.trim(), f = q.toLowerCase();
    items = [];
    if (q) items.push({label: html`Search messages for <b>${q}</b>`, sub: S.route.as ? `in ${name(S.route.as)}'s chats` : 'in every chat', go: () => go({q, layout: S.route.layout === 'map' ? 'chat' : S.route.layout})});
    const people = f ? [...S.users.values()].filter(u => `${u.id} ${u.name} ${u.status} ${Object.values(u.fields || {}).join(' ')}`.toLowerCase().includes(f)).slice(0, 6) : [];
    const chats = f ? [...S.chats.values()].filter(c => title(c, null).toLowerCase().includes(f) || c.id === f).slice(0, 6) : [];
    const groups = [];
    if (people.length) groups.push(['People', people.map(u => ({label: html`${avatar(u.id, 'sm')} ${u.name} <span class="s">${u.id}</span>`, sub: Object.values(u.fields || {}).join(' · '), go: () => go({person: u.id, layout: S.route.layout === 'map' ? 'chat' : S.route.layout})}))]);
    if (chats.length) groups.push(['Chats', chats.map(c => ({label: html`<span class="ico ${c.kind}">${KIND[c.kind]}</span> ${title(c, null)}`, sub: plural(c.messages, 'message'), go: () => go({chat: c.id, layout: S.route.layout === 'map' ? 'chat' : S.route.layout})}))]);
    let out = items.map((it, i) => html`<div class="it" data-i="${i}">${it.label}<span class="s">${it.sub}</span></div>`.s).join('');
    for (const [label, list] of groups) {
      out += html`<div class="grp">${label}</div>`.s;
      for (const it of list) { out += html`<div class="it" data-i="${items.length}"><span class="t">${it.label}</span><span class="s">${it.sub}</span></div>`.s; items.push(it); }
    }
    res.innerHTML = out || html`<div class="grp">Type to search. ${num(S.users.size)} people, ${num(S.chats.size)} chats, ${num(S.total)} messages.</div>`.s;
    sel = Math.min(sel, Math.max(0, items.length - 1));
    mark();
  };
  const mark = () => $$('.it', res).forEach(e => e.classList.toggle('on', +e.dataset.i === sel));
  input.addEventListener('input', () => { sel = 0; render(); });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { sel = Math.min(items.length - 1, sel + 1); mark(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sel = Math.max(0, sel - 1); mark(); e.preventDefault(); }
    else if (e.key === 'Enter' && items[sel]) { const it = items[sel]; overlay.close(); it.go(); }
  });
  res.addEventListener('click', e => { const it = e.target.closest('[data-i]'); if (it) { overlay.close(); items[+it.dataset.i].go(); } });
  input.value = initial;
  render();
  input.focus();
}

function openAsPicker(anchor) {
  const box = document.createElement('div');
  box.className = 'pop';
  box.innerHTML = html`<div class="ph">See everything as</div><input placeholder="Find an agent" spellcheck="false"><div class="list"></div>`.s;
  overlay.open(box, anchor);
  const input = $('input', box), list = $('.list', box);
  const people = [...S.users.values()].filter(u => !u.removed).sort((a, b) => (S.sent.get(b.id) || 0) - (S.sent.get(a.id) || 0));
  const render = () => {
    const f = input.value.trim().toLowerCase();
    const hits = people.filter(u => !f || `${u.id} ${u.name} ${Object.values(u.fields || {}).join(' ')}`.toLowerCase().includes(f)).slice(0, 200);
    list.innerHTML = (S.route.as ? html`<div class="it" data-as="">Everyone<span class="s">the whole server</span></div>`.s : '') +
      hits.map(u => html`<div class="it" data-as="${u.id}">${avatar(u.id, 'sm')} ${u.name}<span class="s">${Object.values(u.fields || {})[0] || u.id}</span></div>`.s).join('');
  };
  input.addEventListener('input', render);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { const it = $('[data-as]', list); if (it) { overlay.close(); go({as: it.dataset.as || null}, true); } } });
  list.addEventListener('click', e => { const it = e.target.closest('[data-as]'); if (it) { overlay.close(); go({as: it.dataset.as || null}, true); } });
  render();
  input.focus();
}

function openMembers(anchor, chatId) {
  const c = S.chats.get(chatId);
  if (!c) return;
  const pos = S.view && S.view.positions ? S.view.positions : {};
  const last = c.last ? c.last.seq : 0;
  const box = document.createElement('div');
  box.className = 'pop';
  box.innerHTML = html`<div class="ph">${plural(c.members.length, 'member')}</div>${c.members.map(u => {
    const p = pos[u];
    const state = p == null ? '' : p >= last ? 'read everything' : p ? `read up to ${chatId}:${p}` : 'read nothing';
    return html`<div class="it" data-person="${u}">${avatar(u, 'sm')} ${name(u)}${(c.managers || []).includes(u) ? html` <span class="pill">manager</span>` : ''}<span class="s">${state}</span></div>`;
  })}`.s;
  overlay.open(box, anchor);
  box.addEventListener('click', e => { const it = e.target.closest('[data-person]'); if (it) { overlay.close(); go({person: it.dataset.person}); } });
}

async function openReceipts(anchor, id) {
  const box = document.createElement('div');
  box.className = 'pop';
  box.innerHTML = '<div class="ph">Read by</div><div class="loading">Loading</div>';
  overlay.open(box, anchor);
  const r = await api('view_receipts', {id});
  box.innerHTML = html`<div class="ph">Read by ${r.read.length} of ${r.read.length + r.unread.length}</div>
${r.read.map(x => html`<div class="it" data-person="${x.id}">${avatar(x.id, 'sm')} ${name(x.id)}<span class="s">${x.at ? clockS(x.at) : 'read'}</span></div>`)}
${r.unread.length ? html`<div class="ph">Not yet</div>${r.unread.map(u => html`<div class="it" data-person="${u}">${avatar(u, 'sm')} ${name(u)}</div>`)}` : ''}`.s;
  box.addEventListener('click', e => { const it = e.target.closest('[data-person]'); if (it) { overlay.close(); go({person: it.dataset.person}); } });
}

let toastTimer;
function toast(text) {
  const tip = $('#tip');
  tip.textContent = text;
  tip.style.left = '50%'; tip.style.top = '60px'; tip.style.transform = 'translateX(-50%)';
  tip.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { tip.classList.remove('on'); tip.style.transform = ''; }, 2600);
}

// ---------------------------------------------------------------------- following everything live

async function follow() {
  for (;;) {
    try {
      const r = await api('view_feed', {after: S.feed, feed_id: S.server.feed_id, timeout: 25});
      setLive(S.server.read_only ? 'ro' : 'on');
      if (r.reset) {
        await loadSnapshot();
        S.viewKey = null; S.paneKey = null;
        await route();
        continue;
      }
      S.feed = r.feed;
      if (r.events.length) { apply(r.events); await sleep(100); }
    } catch (e) {
      if (e.status === 401) return login('The token was refused. It may have changed.');
      setLive('off');
      await sleep(2000);
    }
  }
}

function apply(events) {
  const ev = {lines: [], changes: [], reads: [], chats: new Set(), users: false};
  let asTouched = false;
  const as = S.route.as;
  for (const e of events) {
    if (e.kind === 'user') { S.users.set(e.user.id, e.user); ev.users = true; continue; }
    if (e.kind === 'chat') {
      addChat(e.summary);
      ev.chats.add(e.chat);
      ev.lines.push({...e.event, id: `${e.chat}:${e.seq}`, chat: e.chat});
      if (as && (e.summary.members.includes(as) || (e.event.users || []).includes(as))) asTouched = true;
      continue;
    }
    const c = S.chats.get(e.chat);
    if (!c) continue;
    const fresh = e.seq > c.seq;
    if (fresh) c.seq = e.seq;
    if (as && c.members.includes(as)) asTouched = true;
    if (e.kind === 'message') {
      const m = e.message;
      if (fresh) {
        if (e.count > c.messages) { S.total += e.count - c.messages; c.messages = e.count; }
        c.last = {seq: m.seq, from: m.from, at: m.at, text: (m.text || '').slice(0, 160), deleted: false, files: (m.attachments || []).length};
        c.last_at = m.at;
        c.recent = (c.recent || 0) + 1;
        c.sent[m.from] = (c.sent[m.from] || 0) + 1;
        S.sent.set(m.from, (S.sent.get(m.from) || 0) + 1);
        const t = ms(m.at) / 1000;  // seconds, as the snapshot gives them
        if (!(S.last.get(m.from) >= t)) S.last.set(m.from, t);
        S.rate.push(Date.now());
      }
      ev.lines.push(m);
      ev.chats.add(e.chat);
    } else if (e.kind === 'change') {
      if (c.last && c.last.seq === e.message.seq) c.last = {...c.last, text: e.message.deleted ? '' : (e.message.text || '').slice(0, 160), deleted: !!e.message.deleted};
      ev.changes.push(e.message);
    } else if (e.kind === 'read') {
      ev.reads.push(e);
    }
  }
  if (S.view && S.view.onEvents) S.view.onEvents(ev);
  if (S.pane && S.pane.onEvents) S.pane.onEvents(ev);
  if (asTouched) refreshAs();
  if (ev.chats.size || ev.users) buildSide();
}

// ---------------------------------------------------------------------- clicks anywhere

function onClick(e) {
  const t = e.target;
  const hit = sel => t.closest(sel);
  let el;
  if ((el = hit('[data-layout]'))) {
    const layout = el.dataset.layout, r = S.route;
    return go({layout, chat: r.chat, as: r.as, person: layout === 'map' ? null : r.person, q: layout === 'map' ? null : r.q});
  }
  if (hit('[data-clear-as]')) { e.stopPropagation(); return go({as: null}, true); }
  if ((el = hit('#as'))) return openAsPicker(el);
  if (hit('#find')) return openSearch();
  if (hit('#theme')) return setTheme(THEMES[theme()].next);
  if (hit('[data-home]')) { e.preventDefault(); return go({layout: S.route.layout, as: S.route.as, thread: null, knew: null}); }
  if ((el = hit('[data-see-as]'))) return go({as: el.dataset.seeAs, person: null});
  if ((el = hit('[data-file]'))) return download(el.dataset.file, el.dataset.name);
  if ((el = hit('[data-image]'))) return openPicture(el.dataset);
  if ((el = hit('[data-text]'))) return openText(el.dataset.text);
  if ((el = hit('[data-thread]'))) return go({thread: el.dataset.thread, knew: null}, true);
  if ((el = hit('[data-knew]'))) return go({knew: el.dataset.knew, thread: null}, true);
  if ((el = hit('[data-receipts]'))) return openReceipts(el, el.dataset.receipts);
  if ((el = hit('[data-copy]'))) { navigator.clipboard?.writeText(el.dataset.copy); return toast(`Copied ${el.dataset.copy}`); }
  if (hit('[data-close-pane]')) return go({thread: null, knew: null}, true);
  if ((el = hit('[data-fold]'))) return fold(el);
  if ((el = hit('[data-more]'))) { el.previousElementSibling.classList.remove('long'); return el.remove(); }
  if ((el = hit('[data-members]'))) return openMembers(el, el.dataset.members);
  if ((el = hit('[data-sort]'))) return go({sort: el.dataset.sort}, true);
  if ((el = hit('[data-board]'))) return go({layout: 'board', chat: el.dataset.board});
  if ((el = hit('[data-goto]'))) return go({layout: el.dataset.goto, chat: S.route.chat || (S.view && S.view.chat), post: null}, false);
  if (hit('[data-back]')) return history.length > 1 ? history.back() : go({post: null}, true);
  if (hit('[data-open-post-chat]')) return go({layout: 'chat', chat: S.view.chat, m: +S.view.id.split(':')[1]});
  if ((el = hit('[data-had]'))) return go({layout: 'chat', chat: el.dataset.had, m: el.dataset.upto || 1, had: el.dataset.upto, knew: S.route.knew});
  if ((el = hit('[data-jump]'))) {
    if (S.view instanceof ChatView && S.view.id === el.dataset.chat) return S.view.jump(+el.dataset.jump);
    return go({layout: 'chat', chat: el.dataset.chat, m: el.dataset.jump});
  }
  if ((el = hit('[data-open]'))) return go({layout: S.route.layout === 'map' ? 'chat' : S.route.layout, chat: el.dataset.open, m: el.dataset.at || null});
  if ((el = hit('[data-person]'))) return go({person: el.dataset.person, layout: S.route.layout === 'map' ? 'chat' : S.route.layout});
  if ((el = hit('[data-post]'))) return go({post: el.dataset.post}, true);
  if ((el = hit('.results .msg'))) return go({layout: S.route.layout === 'board' ? 'board' : 'chat', chat: el.dataset.chat, m: el.dataset.seq, q: null, person: null});
}

function onKey(e) {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '');
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); return overlay.on ? overlay.close() : openSearch(); }
  if (e.key === 'Escape') { if (overlay.on) return overlay.close(); if (S.route.thread || S.route.knew) return go({thread: null, knew: null}, true); }
  if (e.key === 'Enter' && document.activeElement?.dataset.text) return openText(document.activeElement.dataset.text);
  if (typing || overlay.on || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); return openSearch(); }
  if (['1', '2', '3'].includes(e.key)) return go({layout: ['chat', 'board', 'map'][+e.key - 1], chat: S.route.chat, as: S.route.as});
  if (S.view && S.view.onKey) S.view.onKey(e);
}

function tips() {
  const tip = $('#tip');
  document.addEventListener('mouseover', e => {
    const el = e.target.closest('[data-tip]');
    if (!el) return tip.classList.remove('on');
    tip.textContent = el.dataset.tip;
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(r.left, innerWidth - 370) + 'px';
    tip.style.top = (r.bottom + 6) + 'px';
    tip.classList.add('on');
  });
}

// ---------------------------------------------------------------------- the map: everyone at once

// Categorical colours in a fixed order, made to be told apart on a light or a dark background. Past eight, the rest are "other".
const PALETTE = ['#4269d0', '#efb118', '#ff725c', '#6cc5b0', '#3ca951', '#ff8ab7', '#a463f2', '#97bbf5'];
const OTHER = '#7b8394';
const MAP = {pos: new Map(), view: null, colorBy: null, lens: true};  // kept while the page is open

// The layout runs in a worker, so the page never stops answering. Forces as in Fruchterman and Reingold: every pair
// pushes apart, every link pulls together, and each step moves less than the one before.
function layoutWorker() {
  self.onmessage = e => {
    const {x, y, mass, a, b, pull, iterations, heat, k} = e.data;
    const n = x.length, dx = new Float32Array(n), dy = new Float32Array(n), kk = k * k, far = (12 * k) ** 2;
    for (let it = 0; it < iterations; it++) {
      dx.fill(0); dy.fill(0);
      if (n <= 2500) {
        for (let i = 0; i < n; i++) {
          const xi = x[i], yi = y[i], mi = mass[i];
          for (let j = i + 1; j < n; j++) {
            let ex = xi - x[j], ey = yi - y[j], d2 = ex * ex + ey * ey;
            if (d2 > far) continue;
            if (d2 < 0.01) { ex = Math.random() - 0.5; ey = Math.random() - 0.5; d2 = 0.5; }
            const f = kk * mi * mass[j] / d2;
            dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
          }
        }
      } else {  // many nodes: only neighbours within a few cells push
        const cell = 6 * k, grid = new Map();
        for (let i = 0; i < n; i++) {
          const key = Math.floor(x[i] / cell) * 65536 + Math.floor(y[i] / cell);
          (grid.get(key) || grid.set(key, []).get(key)).push(i);
        }
        for (let i = 0; i < n; i++) {
          const cx = Math.floor(x[i] / cell), cy = Math.floor(y[i] / cell);
          for (let gx = cx - 1; gx <= cx + 1; gx++) for (let gy = cy - 1; gy <= cy + 1; gy++) {
            const list = grid.get(gx * 65536 + gy);
            if (!list) continue;
            for (const j of list) {
              if (j <= i) continue;
              let ex = x[i] - x[j], ey = y[i] - y[j], d2 = ex * ex + ey * ey;
              if (d2 < 0.01) { ex = Math.random() - 0.5; ey = Math.random() - 0.5; d2 = 0.5; }
              const f = kk * mass[i] * mass[j] / d2;
              dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
            }
          }
        }
      }
      for (let e2 = 0; e2 < a.length; e2++) {
        const i = a[e2], j = b[e2], ex = x[j] - x[i], ey = y[j] - y[i];
        const d = Math.sqrt(ex * ex + ey * ey) || 0.1, f = d / k * pull[e2];
        dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
      }
      const t = heat * (1 - it / iterations) + 0.5;
      for (let i = 0; i < n; i++) {
        dx[i] -= x[i] * 0.03; dy[i] -= y[i] * 0.03;
        const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1, s = Math.min(d, t) / d;
        x[i] += dx[i] * s; y[i] += dy[i] * s;
      }
      if (it % 8 === 7 || it === iterations - 1) self.postMessage({x, y, done: it === iterations - 1});
    }
  };
}

class MapView {
  constructor(main) {
    this.main = main; this.pulses = []; this.win = null; this.winData = null; this.hover = null; this.selected = null;
    main.innerHTML = html`<div class="mapwrap"><canvas class="graph"></canvas><div class="legend"></div>
<div class="mapctl"><button class="btn" data-lens title="A lens that magnifies what is under the mouse (F)">Lens</button><button class="btn" data-fit title="Show everyone (0)">Fit</button></div>
<div class="maphint">Scroll to zoom, drag to move. Click an agent to see as it, a chat to open it.</div>
<div class="strip"><div class="sctl"><button class="btn" data-whole title="Count every message">Whole run</button><button class="btn" data-play title="Slide a window of time across the run">▶ Replay</button></div>
<div class="chart"><canvas></canvas><div class="when"><span></span><span></span></div><div class="sel"></div></div></div></div>`.s;
    this.canvas = $('canvas.graph', main); this.ctx = this.canvas.getContext('2d');
    this.strip = $('.strip canvas', main); this.sctx = this.strip.getContext('2d');
    this.legend = $('.legend', main);
    S.mapPane = true;
    $('#pane').classList.add('open');
    this.listen();
    this.build(true);
    this.loadActivity();
    this.resize = new ResizeObserver(() => { this.size(); this.draw(); this.drawStrip(); });
    this.resize.observe(this.canvas); this.resize.observe(this.strip);
    $('[data-lens]', main).classList.toggle('on', MAP.lens);
    this.showPane();
  }
  destroy() {
    this.dead = true; this.resize.disconnect(); if (this.worker) this.worker.terminate();
    cancelAnimationFrame(this.raf); this.raf = 0; S.mapPane = false; clearInterval(this.playing);
    $('#pane').classList.remove('open'); $('#pane').replaceChildren();
  }
  size() {
    const dpr = devicePixelRatio || 1;
    for (const c of [this.canvas, this.strip]) {
      const r = c.getBoundingClientRect();
      c.width = Math.max(1, Math.round(r.width * dpr)); c.height = Math.max(1, Math.round(r.height * dpr));
      c.w = r.width; c.h = r.height;
    }
    this.dpr = dpr;
  }

  // ---- the graph
  build(fresh) {
    const nodes = [], index = new Map(), edges = [];
    const inChats = new Set();
    for (const c of S.chats.values()) for (const u of c.members) inChats.add(u);
    for (const u of S.users.values()) if (inChats.has(u.id) || S.sent.get(u.id)) { index.set('u:' + u.id, nodes.length); nodes.push({key: 'u:' + u.id, kind: 'agent', user: u.id}); }
    for (const c of S.chats.values()) if (c.kind !== 'direct') { index.set('c:' + c.id, nodes.length); nodes.push({key: 'c:' + c.id, kind: 'hub', chat: c.id}); }
    for (const c of S.chats.values()) {
      if (c.kind === 'direct') {
        const [a, b] = c.members.map(u => index.get('u:' + u));
        if (a != null && b != null) edges.push({a, b, chat: c.id, kind: 'direct'});
      } else {
        const h = index.get('c:' + c.id);
        for (const u of c.members) { const i = index.get('u:' + u); if (i != null) edges.push({a: h, b: i, chat: c.id, user: u, kind: 'spoke'}); }
      }
    }
    const spread = Math.sqrt(nodes.length) * 40;
    const nbrs = new Map();
    for (const e of edges) { (nbrs.get(e.a) || nbrs.set(e.a, []).get(e.a)).push(e.b); (nbrs.get(e.b) || nbrs.set(e.b, []).get(e.b)).push(e.a); }
    let placed = 0;
    nodes.forEach((n, i) => {
      const p = MAP.pos.get(n.key);
      if (p) { n.x = p.x; n.y = p.y; placed++; return; }
      const near = (nbrs.get(i) || []).map(j => MAP.pos.get(nodes[j].key)).find(Boolean);
      const a = Math.random() * Math.PI * 2, r = near ? 20 + Math.random() * 30 : Math.sqrt(Math.random()) * spread;
      n.x = (near ? near.x : 0) + Math.cos(a) * r; n.y = (near ? near.y : 0) + Math.sin(a) * r;
    });
    this.nodes = nodes; this.index = index; this.edges = edges; this.nbrs = nbrs;
    this.weigh();
    this.colors();
    this.layout(fresh && placed < nodes.length / 2);
    if (!MAP.view) this.fit(); else this.view = MAP.view;
  }
  layout(fresh) {
    if (this.worker) this.worker.terminate();
    const n = this.nodes.length;
    if (!n) return this.draw();
    const blob = new Blob([`(${layoutWorker})()`], {type: 'text/javascript'});
    const url = URL.createObjectURL(blob);
    this.worker = new Worker(url);
    URL.revokeObjectURL(url);
    const x = new Float32Array(n), y = new Float32Array(n), mass = new Float32Array(n);
    this.nodes.forEach((d, i) => { x[i] = d.x; y[i] = d.y; mass[i] = d.kind === 'hub' ? 1 + Math.sqrt(S.chats.get(d.chat).members.length) / 2 : 1; });
    const a = new Int32Array(this.edges.length), b = new Int32Array(this.edges.length), pull = new Float32Array(this.edges.length);
    const most = this.edges.reduce((m, e) => Math.max(m, e.w0 || 0), 1);
    const degree = new Float32Array(n);
    for (const e of this.edges) { degree[e.a]++; degree[e.b]++; }
    // A link pulls less when either end has many: an agent in fifty chats is not pulled fifty times as hard.
    this.edges.forEach((e, i) => { a[i] = e.a; b[i] = e.b; pull[i] = (e.kind === 'direct' ? 0.6 : 1) * (0.4 + 0.6 * Math.log1p(e.w0 || 0) / Math.log1p(most)) / Math.sqrt(Math.min(degree[e.a], degree[e.b])); });
    const k = 26;
    this.worker.onmessage = m => {
      const {x: X, y: Y, done} = m.data;
      this.nodes.forEach((d, i) => { d.x = X[i]; d.y = Y[i]; MAP.pos.set(d.key, {x: d.x, y: d.y}); });
      if (this.autoFit) this.fit();
      if (done) this.autoFit = false;
      this.kick();
    };
    this.autoFit = fresh;
    this.worker.postMessage({x, y, mass, a, b, pull, k, iterations: fresh ? 320 : 90, heat: fresh ? Math.sqrt(n) * k / 3 : k * 1.5});
  }
  fit() {
    if (!this.nodes.length || !this.canvas.w) { this.view = MAP.view = {x: 0, y: 0, k: 1}; return; }
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of this.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const k = Math.min(4, 0.86 * Math.min(this.canvas.w / Math.max(60, x1 - x0), this.canvas.h / Math.max(60, y1 - y0)));
    this.view = MAP.view = {x: (x0 + x1) / 2, y: (y0 + y1) / 2, k};
    this.kick();
  }
  weigh() {
    const win = this.winData;
    const msgs = id => win ? (win[id] ? win[id].messages : 0) : S.chats.get(id).messages;
    const sent = (id, u) => win ? (win[id] ? win[id].sent[u] || 0 : 0) : S.chats.get(id).sent[u] || 0;
    const total = new Map();
    for (const e of this.edges) {
      e.w = e.kind === 'direct' ? msgs(e.chat) : sent(e.chat, e.user);
      if (e.w0 == null) e.w0 = e.w;
    }
    if (win) for (const [id, d] of Object.entries(win)) for (const [u, n] of Object.entries(d.sent)) total.set(u, (total.get(u) || 0) + n);
    let maxE = 1, maxA = 1, maxH = 1;
    for (const n of this.nodes) {
      if (n.kind === 'agent') { n.w = win ? total.get(n.user) || 0 : S.sent.get(n.user) || 0; n.r = 2.5 + 1.6 * Math.sqrt(Math.log2(1 + n.w)); maxA = Math.max(maxA, n.w); }
      else { const c = S.chats.get(n.chat); n.w = msgs(n.chat); n.r = Math.min(16, 3 + Math.sqrt(c.members.length)); maxH = Math.max(maxH, n.w); }
    }
    for (const e of this.edges) maxE = Math.max(maxE, e.w);
    this.maxE = maxE; this.maxA = maxA; this.maxH = maxH;
    if (MAP.colorBy === 'activity' && this.nodes.some(n => n.color)) this.colors();
    this.kick();
  }
  fields() {
    const keys = new Map();
    for (const u of S.users.values()) for (const [k, v] of Object.entries(u.fields || {})) {
      const m = keys.get(k) || keys.set(k, new Map()).get(k);
      m.set(String(v), (m.get(String(v)) || 0) + 1);
    }
    return [...keys.entries()].filter(([, vals]) => vals.size >= 2 && vals.size <= 60)
      .sort((p, q) => [...q[1].values()].reduce((s, n) => s + n, 0) - [...p[1].values()].reduce((s, n) => s + n, 0)).map(([k, vals]) => ({key: k, vals}));
  }
  colors() {
    const fields = this.fields();
    if (!MAP.colorBy || (MAP.colorBy !== 'activity' && !fields.some(f => f.key === MAP.colorBy))) MAP.colorBy = fields.length ? fields[0].key : 'activity';
    const by = MAP.colorBy;
    let items = [];
    if (by === 'activity') {
      for (const n of this.nodes) if (n.kind === 'agent') {
        const t = this.maxA > 1 ? Math.log1p(n.w || 0) / Math.log1p(this.maxA) : 0;
        n.color = ink().dark ? `hsl(220 ${40 + 50 * t}% ${30 + 45 * t}%)` : `hsl(220 ${35 + 50 * t}% ${82 - 40 * t}%)`;
      }
      items = ink().dark ? [['quiet', 'hsl(220 40% 30%)'], ['busy', 'hsl(220 90% 75%)']] : [['quiet', 'hsl(220 35% 82%)'], ['busy', 'hsl(220 85% 42%)']];
    } else {
      const vals = [...fields.find(f => f.key === by).vals.entries()].sort((p, q) => q[1] - p[1]);
      const color = new Map(vals.slice(0, 8).map(([v], i) => [v, PALETTE[i]]));
      for (const n of this.nodes) if (n.kind === 'agent') { const v = (user(n.user).fields || {})[by]; n.color = color.get(String(v)) || OTHER; }
      items = vals.slice(0, 8).map(([v, cnt]) => [v, color.get(v), cnt]);
      const rest = vals.slice(8).reduce((s, [, cnt]) => s + cnt, 0) + [...S.users.values()].filter(u => !(u.fields || {})[by]).length;
      if (rest) items.push(['other', OTHER, rest]);
    }
    for (const n of this.nodes) if (n.kind === 'hub') n.color = S.chats.get(n.chat).kind === 'channel' ? ink().channel : ink().group;
    const opts = [...fields.map(f => f.key), 'activity'];
    this.legend.innerHTML = html`<div class="lh">Agents by <select data-colorby>${opts.map(o => html`<option value="${o}"${o === by ? new Safe(' selected') : ''}>${o}</option>`)}</select></div>
${items.map(([label, c, cnt]) => html`<div class="li"><span class="dot" style="background:${c}"></span>${label}${cnt != null ? html`<span class="n">${num(cnt)}</span>` : ''}</div>`)}
<div class="shapes"><span><span class="sq" style="background:var(--channel)"></span>channel</span><span><span class="sq" style="background:var(--group)"></span>group</span><span>— direct chat</span></div>`.s;
    $('select', this.legend).onchange = e => { MAP.colorBy = e.target.value; this.colors(); this.kick(); };
  }

  // ---- drawing
  related() {
    if (!S.route.as || !S.as) return null;
    const chats = new Set(S.as.chats.keys()), agents = new Set([S.route.as]);
    for (const id of chats) for (const u of S.chats.get(id)?.members || []) agents.add(u);
    return {chats, agents};
  }
  project() {
    const W = this.canvas.w, H = this.canvas.h, v = this.view || {x: 0, y: 0, k: 1}, m = this.mouse;
    const lens = MAP.lens && m && !this.dragging, R = Math.min(260, Math.max(140, Math.min(W, H) / 3.2)), dist = 3.2;
    const k0 = Math.exp(dist) / (Math.exp(dist) - 1) * R, k1 = dist / R;
    for (const n of this.nodes) {
      let sx = (n.x - v.x) * v.k + W / 2, sy = (n.y - v.y) * v.k + H / 2, mag = 1;
      if (lens) {
        const dx = sx - m.x, dy = sy - m.y, d = Math.sqrt(dx * dx + dy * dy);
        if (d && d < R) { const f = k0 * (1 - Math.exp(-d * k1)) / d * 0.75 + 0.25; sx = m.x + dx * f; sy = m.y + dy * f; mag = Math.min(f, 6); }
        else if (!d) mag = 6;
      }
      n.sx = sx; n.sy = sy; n.mag = mag;
    }
  }
  draw() {
    if (!this.canvas.w || !this.nodes) return;
    const ctx = this.ctx, W = this.canvas.w, H = this.canvas.h, k = (this.view || {k: 1}).k;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    this.project();
    const rel = this.related(), focus = this.hover ?? this.selected;
    const lit = new Set();
    if (focus != null) { lit.add(focus); for (const j of this.nbrs.get(focus) || []) lit.add(j); }
    // links, in a few strengths so each is one stroke
    const buckets = new Map();
    for (const e of this.edges) {
      const a = this.nodes[e.a], b = this.nodes[e.b];
      const on = focus != null && (e.a === focus || e.b === focus);
      const dim = rel ? !rel.chats.has(e.chat) : false;
      const t = e.w ? Math.log1p(e.w) / Math.log1p(this.maxE) : 0;
      const level = on ? 4 : dim ? 0 : t === 0 && this.winData ? 0 : 1 + Math.min(2, Math.floor(t * 3));
      const key = (e.kind === 'direct' ? 'd' : 's') + level;
      (buckets.get(key) || buckets.set(key, []).get(key)).push(a.sx, a.sy, b.sx, b.sy);
    }
    const alpha = [0.035, 0.09, 0.2, 0.42, 0.85];
    ctx.lineCap = 'round';
    for (const [key, pts] of buckets) {
      const level = +key[1];
      ctx.strokeStyle = key[0] === 'd' ? `rgba(${ink().edgeDm}, ${alpha[level]})` : `rgba(${ink().edge}, ${alpha[level] * 0.8})`;
      ctx.lineWidth = level >= 3 ? 1.6 : 1;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i += 4) { ctx.moveTo(pts[i], pts[i + 1]); ctx.lineTo(pts[i + 2], pts[i + 3]); }
      ctx.stroke();
    }
    // chats, then agents
    const scale = Math.max(0.55, Math.min(2.2, Math.sqrt(k)));
    for (const pass of ['hub', 'agent']) {
      for (let i = 0; i < this.nodes.length; i++) {
        const n = this.nodes[i];
        if (n.kind !== pass) continue;
        const dim = (rel && (pass === 'hub' ? !rel.chats.has(n.chat) : !rel.agents.has(n.user))) || (this.winData && !n.w) || (focus != null && !lit.has(i));
        const r = this.radius(n, scale);
        ctx.globalAlpha = dim ? 0.18 : 1;
        ctx.fillStyle = n.color;
        if (pass === 'hub') {
          const t = n.w ? 0.35 + 0.65 * Math.log1p(n.w) / Math.log1p(this.maxH) : 0.25;
          ctx.globalAlpha *= t;
          ctx.beginPath(); ctx.roundRect(n.sx - r, n.sy - r, r * 2, r * 2, r * 0.35); ctx.fill();
        } else {
          ctx.beginPath(); ctx.arc(n.sx, n.sy, r, 0, Math.PI * 2); ctx.fill();
          if (S.route.as && n.user === S.route.as) { ctx.strokeStyle = ink().text; ctx.lineWidth = 2; ctx.stroke(); }
        }
        if (i === this.selected) { ctx.strokeStyle = ink().text; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(n.sx, n.sy, r + 4, 0, Math.PI * 2); ctx.stroke(); }
      }
    }
    ctx.globalAlpha = 1;
    // names: around the focus, under the lens, and the busiest when zoomed in. The most important go first, and a
    // name that would overlap one already placed is left out, so every name shown can be read.
    ctx.font = '600 11.5px ' + (MAP.font ||= getComputedStyle(document.body).fontFamily);
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 3; ctx.strokeStyle = `rgba(${ink().bg}, .9)`;
    const wanted = [];
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      if (n.sx < -50 || n.sy < -20 || n.sx > W + 50 || n.sy > H + 20) continue;
      const me = S.route.as && n.user === S.route.as;
      if (!(n.mag > 1.3 || lit.has(i) || me || k > 1.2)) continue;
      wanted.push({i, rank: (i === focus ? 4e9 : 0) + (me ? 2e9 : 0) + (lit.has(i) ? 1e9 : 0) + n.mag * 1e6 + (n.w || 0)});
    }
    wanted.sort((p, q) => q.rank - p.rank);
    const taken = new Map(), cell = 24, widths = MAP.widths ||= new Map();
    let placed = 0;
    for (const {i} of wanted) {
      if (placed >= 300) break;
      const n = this.nodes[i];
      const label = n.kind === 'agent' ? name(n.user) : title(S.chats.get(n.chat), null);
      let w = widths.get(label);
      if (w == null) { w = ctx.measureText(label).width; widths.set(label, w); }
      const x = n.sx + this.radius(n, scale) + 4, y0 = n.sy - 8, y1 = n.sy + 8, x1 = x + w;
      let free = true;
      const keys = [];
      for (let gx = Math.floor(x / cell); gx <= Math.floor(x1 / cell) && free; gx++) for (let gy = Math.floor(y0 / cell); gy <= Math.floor(y1 / cell); gy++) {
        const key = gx * 4096 + gy;
        keys.push(key);
        for (const b of taken.get(key) || []) if (x < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]) { free = false; break; }
        if (!free) break;
      }
      if (!free) continue;
      for (const key of keys) (taken.get(key) || taken.set(key, []).get(key)).push([x, y0, x1, y1]);
      placed++;
      ctx.globalAlpha = rel && !(n.kind === 'agent' ? rel.agents.has(n.user) : rel.chats.has(n.chat)) ? 0.35 : 1;
      ctx.strokeText(label, x, n.sy); ctx.fillStyle = n.kind === 'agent' ? ink().text : ink().chatName; ctx.fillText(label, x, n.sy);
    }
    ctx.globalAlpha = 1;
    // messages on their way: sender to chat, then out to its members
    const now = performance.now();
    this.pulses = this.pulses.filter(p => now < p.t0 + p.dur);
    for (const p of this.pulses) {
      const t = (now - p.t0) / p.dur;
      if (t < 0) continue;
      const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      ctx.fillStyle = p.color;
      for (const [i, j] of p.legs) {
        const a = this.nodes[i], b = this.nodes[j];
        if (!a || !b) continue;
        const x = a.sx + (b.sx - a.sx) * e, y = a.sy + (b.sy - a.sy) * e;
        ctx.globalAlpha = Math.min(1, (1 - t) * 3);
        ctx.beginPath(); ctx.arc(x, y, 2.6, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha *= 0.25;
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }
  radius(n, scale) {  // the lens enlarges shapes less than it spreads them, so they never cover their neighbours
    return Math.min(n.kind === 'hub' ? 34 : 26, n.r * scale * (1 + (n.mag - 1) * 0.45));
  }
  kick(ms = 0) {
    this.until = Math.max(this.until || 0, performance.now() + ms);
    if (this.raf || this.dead) return;
    const loop = () => {
      this.raf = 0;
      if (this.dead) return;
      this.draw();
      if (this.pulses.length || performance.now() < this.until) this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  // ---- the mouse
  listen() {
    const c = this.canvas;
    let down = null;
    c.addEventListener('mousemove', e => {
      const r = c.getBoundingClientRect();
      this.mouse = {x: e.clientX - r.left, y: e.clientY - r.top};
      if (down) {
        const dx = e.clientX - down.x, dy = e.clientY - down.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) { this.dragging = true; c.classList.add('drag'); }
        if (this.dragging) { this.view.x = down.vx - dx / this.view.k; this.view.y = down.vy - dy / this.view.k; }
      } else {
        const hit = this.hit(this.mouse.x, this.mouse.y);
        if (hit !== this.hover) { this.hover = hit; c.classList.toggle('point', hit != null); this.showPane(); }
      }
      this.kick();
    });
    c.addEventListener('mouseleave', () => { this.mouse = null; if (this.hover != null) { this.hover = null; this.showPane(); } this.kick(); });
    c.addEventListener('mousedown', e => { down = {x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y}; this.dragging = false; });
    addEventListener('mouseup', this.up = () => {
      if (!down) return;
      const clicked = !this.dragging;
      down = null; this.dragging = false; c.classList.remove('drag');
      if (clicked && this.mouse) this.click(this.hit(this.mouse.x, this.mouse.y));
      this.kick();
    });
    c.addEventListener('wheel', e => {
      e.preventDefault();
      const r = c.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top, v = this.view;
      const k = Math.max(0.03, Math.min(12, v.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))));
      v.x += (mx - c.w / 2) * (1 / v.k - 1 / k); v.y += (my - c.h / 2) * (1 / v.k - 1 / k); v.k = k;
      this.kick();
    }, {passive: false});
    this.main.addEventListener('click', e => {
      if (e.target.closest('[data-lens]')) { MAP.lens = !MAP.lens; e.target.closest('[data-lens]').classList.toggle('on', MAP.lens); this.kick(); }
      if (e.target.closest('[data-fit]')) this.fit();
      if (e.target.closest('[data-whole]')) this.setWindow(null);
      if (e.target.closest('[data-play]')) this.play();
    });
    this.stripEvents();
  }
  hit(x, y) {
    let best = null, bd = Infinity;
    const scale = Math.max(0.55, Math.min(2.2, Math.sqrt(this.view.k)));
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i], d = Math.hypot(n.sx - x, n.sy - y), r = this.radius(n, scale) + 5;
      if (d < r && d < bd) { bd = d; best = i; }
    }
    return best;
  }
  click(i) {
    if (i == null) { this.selected = null; this.showPane(); return; }
    const n = this.nodes[i];
    this.selected = i;
    if (n.kind === 'agent') go({as: n.user === S.route.as ? null : n.user}, true);
    else go({layout: 'chat', chat: n.chat});
    this.showPane();
  }
  onKey(e) {
    if (e.key === 'f' || e.key === 'F') { MAP.lens = !MAP.lens; $('[data-lens]', this.main).classList.toggle('on', MAP.lens); this.kick(); }
    if (e.key === '0') this.fit();
  }
  onAs() { this.kick(); this.showPane(); }
  onTheme() { this.colors(); this.kick(); this.drawStrip(); }

  // ---- live
  onEvents(ev) {
    let grew = false;
    for (const m of ev.lines) {
      if (m.kind !== 'message') { grew = true; continue; }
      const c = S.chats.get(m.chat), from = this.index.get('u:' + m.from);
      if (!c || from == null) { grew = true; continue; }
      const color = this.nodes[from].color || ink().text, now = performance.now();
      if (c.kind === 'direct') {
        const to = this.index.get('u:' + c.members.find(u => u !== m.from));
        if (to != null) this.pulses.push({legs: [[from, to]], t0: now, dur: 900, color});
      } else {
        const hub = this.index.get('c:' + c.id);
        if (hub == null) { grew = true; continue; }
        this.pulses.push({legs: [[from, hub]], t0: now, dur: 450, color});
        const out = c.members.filter(u => u !== m.from).slice(0, 30).map(u => this.index.get('u:' + u)).filter(i => i != null);
        if (out.length) this.pulses.push({legs: out.map(o => [hub, o]), t0: now + 450, dur: 650, color});
      }
      this.bump(ms(m.at) / 1000);
    }
    if (this.pulses.length > 1500) this.pulses = this.pulses.slice(-1500);
    if (grew) this.regrow();
    if (!this.winData) this.reweigh();
    this.kick();
  }
  regrow = throttle(() => { if (!this.dead) this.build(false); }, 3000);
  reweigh = throttle(() => { if (!this.dead) { this.weigh(); if (this.selected == null && this.hover == null) this.showPane(); } }, 1000);

  // ---- the pane: what is under the mouse, or the busiest when nothing is
  showPane() {
    const pane = $('#pane');
    pane.classList.add('open');
    const i = this.hover ?? this.selected;
    const n = i != null ? this.nodes[i] : null;
    const win = this.win ? ` from ${clock(this.win[0])} to ${clock(this.win[1])}` : '';
    if (!n) {
      const agents = this.nodes.filter(d => d.kind === 'agent').sort((p, q) => q.w - p.w).slice(0, 12);
      const chats = [...S.chats.values()].map(c => ({c, w: this.winData ? (this.winData[c.id]?.messages || 0) : c.messages})).sort((p, q) => q.w - p.w).slice(0, 12);
      pane.innerHTML = html`<div class="phead"><h2>Busiest${win}</h2></div><div class="scroll"><div class="note">Hover over anything for more. Click an agent to see everything as it sees it.</div>
<h3 class="ph3">Agents</h3>${agents.map(d => html`<div class="prow" data-person="${d.user}">${avatar(d.user, 'sm')}<span class="t">${name(d.user)}</span><span class="stamp">${num(d.w)}</span></div>`)}
<h3 class="ph3">Chats</h3>${chats.map(({c, w}) => html`<div class="prow" data-open="${c.id}"><span class="ico ${c.kind}">${KIND[c.kind]}</span><span class="t">${title(c, null)}</span><span class="stamp">${num(w)}</span></div>`)}</div>`.s;
      return;
    }
    if (n.kind === 'agent') {
      const u = user(n.user);
      const chats = this.edges.filter(e => e.a === i || e.b === i).map(e => ({c: S.chats.get(e.chat), w: e.w})).filter(x => x.c).sort((p, q) => q.w - p.w);
      pane.innerHTML = html`<div class="phead">${avatar(n.user, 'sm')}<h2>${u.name}</h2></div><div class="scroll"><div class="note">${u.id}${u.status ? html` · <i>${u.status}</i>` : ''}<br>${Object.entries(u.fields || {}).map(([k, v]) => html`<span class="field"><i>${k}</i> ${v}</span> `)}</div>
<div class="note"><b>${num(n.w)}</b> messages${win || ' in all'} · in ${plural(chats.length, 'chat')}</div>
<div class="note"><button class="btn primary" data-see-as="${n.user}">See as ${first(n.user)}</button> <button class="btn" data-person="${n.user}">Profile</button></div>
<h3 class="ph3">Where ${first(n.user)} writes</h3>${chats.slice(0, 30).map(({c, w}) => html`<div class="prow" data-open="${c.id}"><span class="ico ${c.kind}">${KIND[c.kind]}</span><span class="t">${title(c, n.user)}</span><span class="stamp">${num(w)}</span></div>`)}</div>`.s;
    } else {
      const c = S.chats.get(n.chat);
      const people = c.members.map(u => ({u, w: this.winData ? (this.winData[c.id]?.sent[u] || 0) : c.sent[u] || 0})).sort((p, q) => q.w - p.w);
      pane.innerHTML = html`<div class="phead"><span class="ico ${c.kind}">${KIND[c.kind]}</span><h2>${title(c, null)}</h2></div><div class="scroll">
<div class="note">${c.kind}${c.public ? ', public' : ''} · ${plural(c.members.length, 'member')} · <b>${num(n.w)}</b> messages${win}</div>
${c.last ? html`<div class="note"><b>${name(c.last.from)}</b> ${ago(c.last.at)}: ${c.last.deleted ? 'deleted message' : c.last.text || (c.last.files ? plural(c.last.files, 'file') : '')}</div>` : ''}
<div class="note"><button class="btn primary" data-open="${c.id}">Open the chat</button> <button class="btn" data-board="${c.id}">As a board</button></div>
<h3 class="ph3">Who writes here</h3>${people.slice(0, 40).map(({u, w}) => html`<div class="prow" data-person="${u}">${avatar(u, 'sm')}<span class="t">${name(u)}</span><span class="stamp">${num(w)}</span></div>`)}</div>`.s;
    }
  }

  // ---- the strip of messages over time
  async loadActivity() {
    const buckets = Math.max(60, Math.min(600, Math.round((this.strip.getBoundingClientRect().width || 900) / 4)));
    const r = await api('view_activity', {buckets, as: S.route.as || undefined});
    if (this.dead) return;
    this.act = r;
    const when = $$('.when span', this.main);
    when[0].textContent = r.start ? full(r.start) : '';
    when[1].textContent = r.end ? full(r.end) : '';
    this.drawStrip();
  }
  bump(t) {
    const a = this.act;
    if (!a || !a.start) return;
    const i = Math.floor((t - a.start) / a.width);
    if (i >= a.counts.length) { this.reloadActivity(); return; }
    if (i >= 0) a.counts[i]++;
    this.drawStripSoon();
  }
  reloadActivity = throttle(() => { if (!this.dead) this.loadActivity(); }, 10000);
  drawStripSoon = throttle(() => this.drawStrip(), 250);
  drawStrip() {
    const a = this.act, ctx = this.sctx, W = this.strip.w, H = this.strip.h;
    if (!W || !ctx) return;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!a || !a.counts.length) { ctx.fillStyle = ink().faint; ctx.font = '12px sans-serif'; ctx.fillText('No messages yet.', 8, H / 2); return; }
    const top = Math.max(...a.counts, 1), bw = W / a.counts.length, h = H - 16;
    for (let i = 0; i < a.counts.length; i++) {
      const t0 = a.start + i * a.width;
      const inWin = !this.win || (t0 + a.width >= this.win[0] && t0 <= this.win[1]);
      ctx.fillStyle = inWin ? ink().accent : ink().line2;
      const bh = a.counts[i] ? Math.max(1.5, h * a.counts[i] / top) : 0;
      ctx.fillRect(i * bw + 0.5, h - bh, Math.max(1, bw - 1), bh);
    }
    if (this.win) {
      const x0 = (this.win[0] - a.start) / (a.end - a.start) * W, x1 = (this.win[1] - a.start) / (a.end - a.start) * W;
      ctx.strokeStyle = ink().accentSoft; ctx.lineWidth = 1;
      ctx.strokeRect(x0 + 0.5, 0.5, Math.max(2, x1 - x0) - 1, h);
      const sel = $('.sel', this.main);
      sel.style.left = Math.min(W - 160, Math.max(0, x0)) + 'px';
      sel.textContent = `${clockS(this.win[0])} to ${clockS(this.win[1])}`;
    } else $('.sel', this.main).textContent = '';
  }
  stripEvents() {
    const c = this.strip;
    let start = null;
    const timeAt = x => { const a = this.act; return a.start + Math.max(0, Math.min(1, x / c.w)) * (a.end - a.start); };
    c.addEventListener('mousedown', e => { if (!this.act || !this.act.start) return; this.stop(); start = timeAt(e.offsetX); });
    c.addEventListener('mousemove', e => {
      if (start == null) return;
      const t = timeAt(e.offsetX);
      this.win = [Math.min(start, t), Math.max(start, t)];
      this.drawStrip();
    });
    addEventListener('mouseup', this.stripUp = e => {
      if (start == null) return;
      const t = this.win ? this.win : null;
      start = null;
      if (!t || t[1] - t[0] < (this.act.end - this.act.start) / 300) this.setWindow(null); else this.setWindow(t);
    });
  }
  setWindow(w) {
    this.win = w;
    this.drawStrip();
    if (!w) { this.winData = null; this.stop(); this.weigh(); this.showPane(); return; }
    this.fetchWindow();
  }
  fetchWindow = throttle(async () => {
    if (!this.win) return;
    const [from, to] = this.win;
    const r = await api('view_activity', {buckets: 1, from, to, as: S.route.as || undefined});
    if (this.dead || !this.win) return;
    this.winData = r.window;
    this.weigh();
    if (this.selected == null && this.hover == null) this.showPane();
  }, 120);
  play() {
    const a = this.act;
    if (!a || !a.start) return;
    if (this.playing) return this.stop();
    const span = a.end - a.start, width = this.win ? this.win[1] - this.win[0] : span / 12;
    let t = this.win && this.win[1] < a.end ? this.win[0] : a.start;
    $('[data-play]', this.main).textContent = '❚❚ Pause';
    this.playing = setInterval(() => {
      t += span / 200;
      if (t + width > a.end) { this.win = [a.end - width, a.end]; this.fetchWindow(); this.drawStrip(); return this.stop(); }
      this.win = [t, t + width];
      this.drawStrip();
      this.fetchWindow();
    }, 100);
  }
  stop() {
    clearInterval(this.playing); this.playing = null;
    const b = $('[data-play]', this.main);
    if (b) b.textContent = '▶ Replay';
  }
}

// ---------------------------------------------------------------------- theme

// The page follows the system's light or dark setting, unless one is picked with the button in the top bar. Every
// color lives in the stylesheet, and the canvases read theirs from it through ink().
const THEME_KEY = 'swarmchat.view.theme';
const ICON = body => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const THEMES = {
  system: {next: 'light', title: 'Theme: same as the system. Click for light.', icon: ICON('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>')},
  light: {next: 'dark', title: 'Theme: light. Click for dark.', icon: ICON('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>')},
  dark: {next: 'system', title: 'Theme: dark. Click to follow the system.', icon: ICON('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>')},
};
const systemDark = matchMedia('(prefers-color-scheme: dark)');
let INK = null;

function theme() { const t = localStorage.getItem(THEME_KEY); return THEMES[t] ? t : 'system'; }
function setTheme(t) {
  if (t === 'system') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, t);
  showTheme();
}
function showTheme() {
  const t = theme();
  if (t === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  INK = null;
  const b = $('#theme');
  b.innerHTML = THEMES[t].icon;
  b.title = THEMES[t].title;
  if (S.view && S.view.onTheme) S.view.onTheme();
}
systemDark.addEventListener('change', () => { if (theme() === 'system') showTheme(); });

// The theme's colors as a canvas needs them, resolved to rgb(). Some are kept as bare "r, g, b" to add an alpha to.
function ink() {
  if (INK) return INK;
  const probe = document.body.appendChild(document.createElement('i'));
  const read = name => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
  const bare = name => read(name).match(/[\d.]+/g).slice(0, 3).join(', ');
  const t = theme();
  INK = {dark: t === 'dark' || (t === 'system' && systemDark.matches), text: read('--text'), faint: read('--faint'), line2: read('--line2'),
    accent: read('--accent'), accentSoft: read('--accent-soft'), channel: read('--channel'), group: read('--group'),
    chatName: read('--channel-soft'), bg: bare('--bg'), edge: bare('--edge'), edgeDm: bare('--edge-dm')};
  probe.remove();
  return INK;
}

// ---------------------------------------------------------------------- start

function login(error) {
  const box = $('#login');
  box.classList.add('on');
  $('.err', box).textContent = error || '';
  const form = $('form', box);
  form.onsubmit = async e => {
    e.preventDefault();
    TOKEN = form.token.value.trim();
    try {
      await loadSnapshot();
      localStorage.setItem('swarmchat.view.token', TOKEN);
      box.classList.remove('on');
      start();
    } catch (err) { $('.err', box).textContent = err.status === 401 ? 'That token was refused.' : err.message; }
  };
  form.token.focus();
}

let started = false;
async function start() {
  if (started) return;
  started = true;
  document.title = `swarmchat view · ${S.server.data ? S.server.data.split(/[\\/]/).filter(Boolean).pop() : ''}`;
  setLive(S.server.read_only ? 'ro' : 'on');
  S.since = Date.now();
  side.init();
  tips();
  document.addEventListener('click', onClick);
  new MutationObserver(watchPictures).observe(document.body, {childList: true, subtree: true});
  document.addEventListener('keydown', onKey);
  addEventListener('hashchange', route);
  await route();
  follow();
}

(async function boot() {
  showTheme();
  const found = location.hash.match(/(^#|&)token=([0-9a-f]{16,})/);
  if (found) {
    localStorage.setItem('swarmchat.view.token', found[2]);
    const rest = location.hash.replace(/(^#|&)token=[0-9a-f]+/, '$1').replace(/^#&/, '#');
    history.replaceState(null, '', location.pathname + (rest.length > 1 ? rest : ''));
  }
  TOKEN = localStorage.getItem('swarmchat.view.token') || '';
  if (!TOKEN) return login();
  try {
    await loadSnapshot();
  } catch (e) {
    return login(e.status === 401 ? 'The saved token was refused. The server may have a new one.' : e.message);
  }
  start();
})();
