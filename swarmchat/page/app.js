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
const FULL = new Map();
const full = at => {  // a time in words, for tooltips. Making one is slow, and a page shows the same ones again and again.
  let s = FULL.get(at);
  if (s == null) {
    if (FULL.size > 5000) FULL.clear();
    FULL.set(at, s = new Date(ms(at)).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'medium'}));
  }
  return s;
};
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
const natural = (a, b) => String(a).localeCompare(String(b), undefined, {numeric: true});
const iso = t => new Date(Math.round(t * 1000)).toISOString();  // seconds since 1970, as the server writes times
const isoAfter = t => new Date(Math.round(t * 1000) + 1).toISOString();  // a bound that still lets in what was at t
const span = s => {  // a stretch of time as a clock shows it: 4:05, or 1:02:03
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
};
function hashHue(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619); return (h >>> 0) % 360; }
// Each agent's own colour, on its avatar, its place on the map and its messages in flight. A team of up to eight has
// eight colours made to be told apart, given in the order of their ids, so a1 to a8 always look the same. A larger
// swarm takes a hue from each id, all at one lightness, so white initials read on every one.
const IDENTITY = ['#e8492a', '#3b5bdb', '#2f9e44', '#7048e8', '#d97706', '#1098ad', '#d6336c', '#9c36b5'];
let RANK = null;  // id -> its place in IDENTITY. Worked out again when people change.
function userColor(id) {
  if (!RANK) {
    const ids = [...S.users.keys()].sort(natural);
    RANK = new Map(ids.length <= IDENTITY.length ? ids.map((u, i) => [u, i]) : []);
  }
  const i = RANK.get(id);
  return i != null ? IDENTITY[i] : `oklch(0.58 0.14 ${hashHue(id)})`;
}
function initials(name) {
  const w = String(name).replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  return ((w[0] || '?')[0] + (w.length > 1 ? w[w.length - 1][0] : (w[0] || '')[1] || '')).toUpperCase();
}
// The page's shape follows the screen. A phone shows one screen at a time, a finger gets bigger rows than a mouse, and
// with nothing to hover with, a tap on a message shows what the mouse shows over it.
const PHONE = matchMedia('(max-width: 699px)');
const COARSE = matchMedia('(pointer: coarse)');
const HOVERLESS = matchMedia('(hover: none)');

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
  S.users = new Map(r.users.map(u => [u.id, u])); RANK = null;
  S.chats = new Map(); S.total = 0; S.sent = new Map(); S.last = new Map();
  for (const c of r.chats) {
    addChat(c);
    for (const [u, n] of Object.entries(c.sent)) S.sent.set(u, (S.sent.get(u) || 0) + n);
    for (const [u, at] of Object.entries(c.last_by)) if (!S.last.has(u) || S.last.get(u) < at) S.last.set(u, at);
  }
  S.loadedIn = performance.now() - t;
}

// ---------------------------------------------------------------------- the address bar holds where you are

const KEYS = ['layout', 'chat', 'm', 'had', 'thread', 'knew', 'as', 'person', 'q', 'unread', 'post', 'sort', 'all'];
function readRoute() {
  const p = new URLSearchParams(location.hash.slice(1));
  const r = {};
  for (const k of KEYS) if (p.get(k)) r[k] = p.get(k);
  r.layout = ['chat', 'board', 'map'].includes(r.layout) ? r.layout : 'chat';
  return r;
}
// Goes somewhere. Keys set to null are dropped. Without keep, only the layout and the agent seen as come along.
function hashOf(changes, keep = false) {
  const base = keep ? {...S.route} : {layout: S.route.layout, as: S.route.as};
  const r = {...base, ...changes};
  const p = new URLSearchParams();
  for (const k of KEYS) if (r[k] != null && r[k] !== '') p.set(k, r[k]);
  return '#' + p.toString();
}
function go(changes, keep = false) {
  const hash = hashOf(changes, keep);
  if (location.hash !== hash) { S.from = location.hash; location.hash = hash; } else route();
}
// Goes back as the browser's back button does, when the place before was in this page and, if `changes` are given,
// is the place they lead to. Otherwise goes to `changes`. Each place keeps the one it came from in history.state.
function back(changes, keep = false) {
  const prev = history.state && history.state.from;
  if (prev != null && (!changes || prev === hashOf(changes, keep))) return history.back();
  go(changes || {}, keep);
}
// Whether a phone shows a screen of its own, with a way back, rather than a layout's first screen
function opened(r) {
  return r.layout !== 'map' && !!(r.chat || r.person || r.q || r.unread || r.post || (r.layout === 'chat' && r.all));
}

function viewKey(r) {
  if (r.layout === 'map') return 'map';
  if (r.layout === 'chat' && PHONE.matches && !opened(r)) return 'home';
  if (r.q) return `q|${r.q}|${r.as}`;
  if (r.person) return `person|${r.person}`;
  if (r.unread && r.as) return `unread|${r.as}`;
  if (r.layout === 'board') return r.post ? `post|${r.post}|${r.as}` : `board|${r.chat}|${r.sort}|${r.as}`;
  return r.chat ? `chat|${r.chat}|${r.m}|${r.had}|${r.knew}|${r.as}` : `all|${r.as}`;
}

async function route() {
  if (S.from != null) { history.replaceState({from: S.from}, ''); S.from = null; }
  const prev = S.route;
  S.route = readRoute();
  const r = S.route, body = document.body, deep = opened(r), was = body.classList.contains('opened');
  const hide = PHONE.matches && deep && !was, show = PHONE.matches && was && !deep;
  if (hide) side.top = side.list ? side.list.scrollTop : 0;  // a phone's list keeps its place under an opened screen
  for (const l of ['chat', 'board', 'map']) body.classList.toggle('layout-' + l, l === r.layout);
  body.classList.toggle('opened', deep);
  body.classList.toggle('picked', r.layout === 'map' && !!(r.chat || r.person));
  if (show && side.list) side.list.scrollTop = side.top || 0;
  if (replay.on && r.layout === 'board') await replay.exit(false);  // a board is only shown live
  if (r.as !== prev.as) await (replay.on ? replay.reload() : loadAs());
  const key = viewKey(r);
  if (r.as !== prev.as && key === S.viewKey && S.view && S.view.onAs) S.view.onAs();
  if (key === S.viewKey && S.view && S.view.onRoute) S.view.onRoute();
  if (key !== S.viewKey) {
    if (S.view && S.view.destroy) S.view.destroy();
    S.viewKey = key;
    const main = $('#main');
    main.replaceChildren();
    S.view = r.layout === 'map' ? new MapView(main)
      : key === 'home' ? new HomeView()
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
  replay.show();
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
  for (const b of $$('#tabbar button')) b.classList.toggle('on', b.dataset.nav === (r.layout === 'chat' ? S.tab : r.layout));
  const agents = [...S.users.values()].filter(u => !u.removed).length;
  const now = Date.now();
  S.rate = S.rate.filter(t => now - t < 10000);
  const rate = replay.on ? 0 : S.rate.length / Math.max(1, Math.min(10, (now - S.since) / 1000));
  const chats = replay.on ? [...S.chats.values()].filter(c => c.born).length : S.chats.size;
  $('#stats').innerHTML = html`<b>${num(agents)}</b> agents · <b>${num(chats)}</b> chats · <b>${num(S.total)}</b> messages${replay.on ? ' so far' : ''}${rate ? html` · <b>${rate < 10 ? rate.toFixed(1) : Math.round(rate)}</b>/s` : ''}`;
  const as = $('#as');
  as.classList.toggle('on', !!r.as);
  as.innerHTML = r.as ? html`${avatar(r.as, 'sm')}<span><span class="pre">As </span><b>${name(r.as)}</b></span><span class="x" data-clear-as title="See everything again">×</span>`
    : html`<span><span class="pre">As </span><span class="who">everyone</span></span><span>▾</span>`;
}
setInterval(() => renderTop(), 2000);

function setLive(state) {
  if (S.liveState === state) return;
  S.liveState = state;
  const el = $('#live');
  el.className = 'live ' + state;
  el.textContent = {on: 'Live', ro: 'Read-only', off: 'Offline', rp: 'Replay'}[state] || '';
  el.title = {on: 'Following every chat as it happens', ro: `No server is running on ${S.server.data}: this is the folder as it was on disk`,
    off: 'Lost the server. Trying again.', rp: 'Showing the run as it stood at the moment on the bar below'}[state] || '';
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
    this.h = COARSE.matches ? 44 : 30;  // as tall as .row in the stylesheet
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
    else rows.push(special('All activity', '≋', !r.chat && !r.q && !r.person && !r.unread, () => go({all: 1})));
    if (r.as && S.as && !replay.on) rows.push(special(`${first(r.as)}'s unread`, '✉', !!r.unread, () => go({unread: 1}),
      S.as.order.reduce((n, id) => n + (S.as.chats.get(id).unread || 0), 0)));
  }
  let chats;
  if (r.as && S.as) chats = S.as.order.map(id => S.chats.get(id)).filter(Boolean);
  else chats = [...S.chats.values()];
  if (replay.on) {  // only the chats made by the moment, in the order they were made, so the list holds still
    chats = chats.filter(c => c.born).sort((a, b) => (a.created_at > b.created_at) - (a.created_at < b.created_at));
  } else if (!(r.as && S.as)) chats.sort((a, b) => (b.last_at > a.last_at) - (b.last_at < a.last_at));
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
    open: () => go({chat: c.id, person: null, layout: r.layout}),
    html: i => {
      const on = r.chat === c.id && !r.q && !r.person && !r.unread;
      // in a replay, as no one: what came since this chat was last open, as in a chat app
      const fresh = replay.on && !mine && !on ? c.messages - (replay.seen.get(c.id) ?? c.messages) : 0;
      const unread = mine ? mine.unread : fresh;
      const badge = mine ? (mine.mentioned ? html`<span class="badge at" title="Mentioned">@${unread}</span>` : unread ? html`<span class="badge${mine.notify !== 'all' ? ' mute' : ''}" title="Unread for ${name(r.as)}${mine.notify !== 'all' ? `, notify ${mine.notify}` : ''}">${num(unread)}</span>` : '')
        : fresh > 0 ? html`<span class="badge" title="New since you last opened it">${num(fresh)}</span>`
        : replay.on ? '' : html`<span class="n" title="Messages">${num(c.messages)}</span>`;
      const busy = !replay.on && c.recent ? Math.max(8, Math.round(100 * Math.sqrt(c.recent / S.busiest))) : 0;
      const other = c.kind === 'direct' ? c.members.find(u => u !== r.as) || c.members[0] : null;
      return html`<div class="row${on ? ' on' : ''}${unread ? ' unread' : ''}${replay.on && !c.messages ? ' quiet' : ''}" data-i="${i}" title="${title(c)}${c.public ? ' (public)' : ''}${c.archived ? ' (archived)' : ''}${c.description ? '\n' + c.description : ''}
${replay.on ? plural(c.messages, 'message') + ' so far' : num(c.recent || 0) + ' messages in the last 15 minutes'}">${other ? avatar(other, 'xs') : html`<span class="ico ${c.kind}">${KIND[c.kind]}</span>`}<span class="t">${c.name || title(c)}</span>${busy ? html`<span class="busy"><i style="width:${busy}%"></i></span>` : ''}${badge}</div>`.s;
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
      open: () => go({person: u.id, chat: null, layout: r.layout}),
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
  return html`<div class="msg${cont ? ' cont' : ''}${mentionsMe ? ' mentions-me' : ''}" data-seq="${m.seq}" data-chat="${m.chat}" data-id="${m.id}" data-time="${replay.on ? span(ms(m.at) / 1000 - replay.act.start) : clock(m.at)}"${o.tree ? html` style="--d:${depth}"` : ''}>
${avatar(m.from)}<div class="body"><div class="head"><span class="name" data-person="${m.from}">${u.name}</span>${u.name !== m.from ? html`<span class="uid">${m.from}</span>` : ''}<time title="${full(m.at)}">${replay.on ? span(ms(m.at) / 1000 - replay.act.start) : o.tree || o.showChat ? ago(m.at) : clock(m.at)}</time>${o.showChat ? html`<span class="chat" data-open="${m.chat}" data-at="${m.seq}">in ${chatTitle(m.chat)}</span>` : ''}${m.edited_at ? html`<span class="edited" title="Edited ${full(m.edited_at)}">(edited)</span>` : ''}</div>
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
  return html`<div class="sys" data-seq="${e.seq}"><span>${what ? what() : e.kind}</span><span class="stamp" title="${full(e.at)}">${replay.on ? span(Math.max(0, ms(e.at) / 1000 - replay.act.start)) : clock(e.at)}</span></div>`;
}

// ---------------------------------------------------------------------- one chat

// A replay shows only what was written by its moment. Messages from a server, cut at that moment, with whether any were.
function upToNow(list) {
  if (!replay.on) return [list, false];
  const kept = list.filter(m => ms(m.at) <= Math.round(replay.t * 1000));
  return [kept, kept.length < list.length];
}

class ChatView {
  constructor(main, chatId, o) {
    this.id = chatId; this.o = o; this.msgs = []; this.first = this.last = false; this.positions = {}; this.fresh = 0;
    this.main = main; this.ahead = []; this.want = 0;  // in a replay: fetched but not reached yet, and the newest reached
    if (replay.on) replay.seen.set(chatId, S.chats.get(chatId)?.messages || 0);
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
    const made = replay.on ? `at ${span(ms(c.created_at) / 1000 - replay.act.start)}` : ago(c.created_at);
    this.head.innerHTML = html`<span class="ico ${c.kind}">${KIND[c.kind]}</span><h1 title="${title(c, null)}">${c.name ? '#' + c.name : title(c, null)}</h1><span class="pill ${kind}">${kind}</span>
${c.archived ? html`<span class="pill archived">archived</span>` : ''}
<span class="sub"><span class="link" data-members="${c.id}">${plural(c.members.length, 'member')}</span> · ${plural(c.messages, 'message')}${replay.on ? ' so far' : ''}${this.o.inPane ? '' : ` · made by ${name(c.created_by)} ${made}`}</span>
<div class="grow"></div>${this.o.inPane ? html`<button class="btn" data-open="${c.id}" title="Open this chat on its own">Open</button>` : html`<button class="btn" data-goto="board">Open as a board</button>`}
${c.description && !this.o.inPane ? html`<div class="about" title="${c.description}">${c.description}</div>` : ''}`;
    const banners = $('.banners', this.main);
    const notIn = r.as && !c.members.includes(r.as);
    banners.innerHTML = notIn ? html`<div class="banner">${name(r.as)} is not in this chat, so ${first(r.as)} cannot see any of it.</div>` : '';
  }
  async load(params) {
    const o = this.o, as = S.route.as, c = S.chats.get(this.id), gen = replay.gen;
    const pos = as && S.as && S.as.positions[this.id];
    const start = o.jump ? {around: o.jump} : o.had === 0 ? {after: 0} : replay.on ? {at: replay.t}
      : pos != null && c.last && pos < c.last.seq ? {around: pos + 1} : {};
    const r = await api('view_messages', params || {chat: this.id, limit: 150, ...start});
    if (this.dead || gen !== replay.gen) return;
    const [msgs, cut] = upToNow(r.messages);
    this.msgs = msgs; this.first = r.first; this.last = r.last || cut || (replay.on && !params && !o.jump && o.had !== 0);
    this.positions = r.positions;
    if (replay.on) this.want = msgs.length ? msgs[msgs.length - 1].seq : 0;
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
      const [later, cut] = upToNow(r.messages);
      this.msgs = [...this.msgs, ...later.filter(m => !have.has(m.seq))];
      this.last = r.last || cut;
      if (replay.on && this.last) this.want = Math.max(this.want, this.msgs[this.msgs.length - 1].seq);
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
    this.add(mine);
  }
  add(list) {  // messages newer than any shown, at the bottom
    const have = new Set(this.msgs.map(m => m.seq));
    const add = list.filter(m => !have.has(m.seq));
    if (!add.length) return;
    const stick = this.atBottom();
    const parents = new Set(add.map(m => m.parent && m.parent.seq).filter(Boolean));
    const prev = this.msgs[this.msgs.length - 1];
    this.msgs.push(...add);
    for (const m of this.msgs) if (parents.has(m.seq)) m.replies = (m.replies || 0) + add.filter(a => a.parent && a.parent.seq === m.seq).length;
    if (parents.size || this.divider()) this.draw();
    else {
      this.box.querySelector('.empty')?.remove();
      const tail = this.box.lastElementChild;
      this.box.insertAdjacentHTML('beforeend', this.rows(add, prev));
      if (add.length <= ARRIVING) for (let el = tail ? tail.nextElementSibling : this.box.firstElementChild; el; el = el.nextElementSibling) el.classList.add('arrive');
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
  // ---- a replay: each message is shown when the moment passes it. The next ones are fetched a page ahead.
  onReplay(list) {
    let top = 0;
    for (const m of list) if (m[1] === this.id) top = m[2];
    if (!top) return;
    this.want = Math.max(this.want, top);
    replay.seen.set(this.id, S.chats.get(this.id).messages);
    this.renderHeadSoon();
    if (this.last) this.pull(); else this.pillCount(list.filter(m => m[1] === this.id).length);
  }
  renderHeadSoon = throttle(() => { if (!this.dead) this.renderHead(); }, 500);
  async pull() {
    if (this.pulling) return;
    this.pulling = true;
    const gen = replay.gen;
    try {
      while (!this.dead && gen === replay.gen) {
        const shown = this.msgs.length ? this.msgs[this.msgs.length - 1].seq : 0;
        if (shown >= this.want) break;
        const ready = this.ahead.filter(m => m.seq <= this.want);
        const known = this.ahead.length ? this.ahead[this.ahead.length - 1].seq : shown;
        if (known < this.want && !this.aheadEnd) {
          const r = await api('view_messages', {chat: this.id, after: known, limit: 100});
          if (this.dead || gen !== replay.gen) return;
          this.ahead.push(...r.messages.filter(m => m.seq > known));
          this.aheadEnd = r.last;
          continue;
        }
        this.ahead = this.ahead.filter(m => m.seq > this.want);
        if (ready.length) this.add(ready); else break;
      }
    } finally { this.pulling = false; }
  }
  onSeek() {  // the replay moved: start again from its new moment
    this.ahead = []; this.aheadEnd = false; this.want = 0; this.o.jump = null; this.fresh = 0; this.pill.hidden = true;
    replay.seen.set(this.id, S.chats.get(this.id)?.messages || 0);
    this.renderHead();
    this.load();
  }
  onAs() { this.renderHead(); this.draw(); }
  destroy() { this.dead = true; }
}

// A phone's first screen in the chat layout is the list itself, and nothing is drawn behind it.
class HomeView { onSeek() {} }

// ---------------------------------------------------------------------- lists of messages from many chats

// A few new messages fade in as they arrive. A larger batch, as a fast replay brings every fifth of a second, just
// appears: faded in together, every message on screen would be mid-fade all the time.
const ARRIVING = 4;

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
  constructor(main, o = {}) {
    super(main, html`<span class="ico">≋</span><h1>All activity</h1><span class="sub"></span>`);
    this.o = o;
    this.load();
  }
  heading() {
    const as = S.route.as;
    $('.sub', this.main).textContent = `${as ? `in ${name(as)}'s chats, ` : this.o.inPane ? '' : 'every chat, '}newest at the bottom${replay.on ? ', as the replay reaches them' : ', live'}`;
    this.none = replay.on ? 'No messages yet at this moment of the run.' : 'No messages yet. New ones appear here as they are written.';
  }
  async load() {
    this.wait.hidden = false;
    this.heading();
    const gen = replay.gen;
    const r = await api('view_search', {limit: 150, as: S.route.as || undefined, before: replay.on ? isoAfter(replay.t) : undefined});
    if (this.dead || gen !== replay.gen) return;
    this.wait.hidden = true;
    this.msgs = r.messages.reverse();
    this.draw();
    this.scroll.scrollTop = this.scroll.scrollHeight;
  }
  onEvents(ev) { this.append(ev.lines.filter(m => m.kind === 'message')); }
  append(add) {
    if (S.route.as) add = add.filter(m => (S.chats.get(m.chat)?.members || []).includes(S.route.as));
    const have = new Set(this.msgs.slice(-300).map(m => m.id));
    add = add.filter(m => !have.has(m.id));
    if (!add.length) return;
    const stick = this.scroll.scrollHeight - this.scroll.scrollTop - this.scroll.clientHeight < 80;
    const prev = this.msgs[this.msgs.length - 1];
    this.msgs.push(...add);
    if (this.msgs.length > 400) { this.msgs = this.msgs.slice(-300); this.draw(); }
    else {
      this.box.querySelector('.empty')?.remove();
      let p = prev, out = '';
      for (const m of add) { const h = messageHtml(m, p, {showChat: true}).s; out += add.length <= ARRIVING ? h.replace('<div class="msg', '<div class="arrive msg') : h; p = m; }
      this.box.insertAdjacentHTML('beforeend', out);
    }
    if (stick) this.scroll.scrollTop = this.scroll.scrollHeight;
  }
  // A replay: the newest messages up to the moment, a few times a second. A burst larger than a page shows its newest.
  onReplay(list) {
    if (S.route.as) list = list.filter(m => (S.chats.get(m[1])?.members || []).includes(S.route.as));
    if (list.length) this.pullSoon();
  }
  pullSoon = throttle(() => this.pull(), 200);
  async pull() {  // one at a time: on a slow server, asking again before the answer only makes a queue
    if (this.dead || !replay.on) return;
    if (this.pulling) { this.again = true; return; }
    this.pulling = true; this.again = false;
    const gen = replay.gen, last = this.msgs[this.msgs.length - 1];
    try {
      const r = await api('view_search', {limit: 100, as: S.route.as || undefined, before: isoAfter(replay.t), after: last ? last.at : undefined});
      if (!this.dead && gen === replay.gen) this.append(r.messages.reverse());
    } finally { this.pulling = false; }
    if (this.again) this.pullSoon();
  }
  onSeek() { this.load(); }
}

class UnreadView extends ListView {
  constructor(main) {
    const as = S.route.as;
    super(main, html`<span class="ico">✉</span><h1>What ${name(as)} would read now</h1><span class="sub">exactly what <code>swarmchat read</code> would hand ${first(as)}, oldest first. Nothing is marked read.</span>`);
    this.none = replay.on ? `A replay does not rebuild this list. The number on each of ${first(as)}'s chats is what ${first(as)} had not read at this moment.`
      : `Nothing unread: ${first(as)} has read everything.`;
    this.flat = true;
    this.draw2();
  }
  draw2() {
    this.msgs = S.as && !replay.on ? S.as.unread : [];
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
      after: p.after, before: replay.on && !(p.before < isoAfter(replay.t)) ? isoAfter(replay.t) : p.before, as: S.route.as || undefined, limit: 100, until});
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
    const r = await api('view_search', {from: [this.id], limit: 40, before: replay.on ? isoAfter(replay.t) : undefined});
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
      this.box.innerHTML = treeHtml(upToNow(r.messages)[0]).s;
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
  box.innerHTML = html`<div class="pq"><input placeholder="Search messages, people and chats" spellcheck="false"><button class="cancel" data-dismiss>Cancel</button></div><div class="res"></div><div class="hint">Enter searches every message. Narrow it with <code>from:alice</code> <code>in:#ops</code> <code>mentions:bob</code> <code>has:file</code> <code>after:2026-10-01</code> <code>before:…</code></div>`.s;
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
    if (replay.on) { await replay.ended; continue; }  // a replay has the page. Its end takes a new snapshot.
    try {
      const r = await api('view_feed', {after: S.feed, feed_id: S.server.feed_id, timeout: 25});
      if (replay.on) continue;
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
      if (!replay.on) setLive('off');
      await sleep(2000);
    }
  }
}

function apply(events) {
  const ev = {lines: [], changes: [], reads: [], chats: new Set(), users: false};
  let asTouched = false;
  const as = S.route.as;
  for (const e of events) {
    if (e.kind === 'user') { if (!S.users.has(e.user.id)) RANK = null; S.users.set(e.user.id, e.user); ev.users = true; continue; }
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
        replay.bump(t);
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
  if (hit('#back')) return back();
  if ((el = hit('[data-nav]'))) {
    const nav = el.dataset.nav;
    if (nav === 'search') return openSearch();
    if (nav === 'chats' || nav === 'people') { S.tab = nav; return go({layout: 'chat'}); }
    return go({layout: nav});
  }
  if (hit('[data-dismiss]')) return overlay.close();
  if (hit('[data-unpick]')) return go({chat: null, person: null});
  if ((el = hit('[data-layout]'))) {
    const layout = el.dataset.layout, r = S.route;
    return go({layout, chat: r.chat, as: r.as, person: layout === 'map' ? null : r.person, q: layout === 'map' ? null : r.q});
  }
  if (hit('[data-clear-as]')) { e.stopPropagation(); return go({as: null}, true); }
  if ((el = hit('#as'))) return openAsPicker(el);
  if (hit('#find')) return openSearch();
  if (hit('#replay')) return replay.enter();
  if (hit('#theme')) return setTheme(THEMES[theme()].next);
  if (hit('[data-home]')) { e.preventDefault(); return go({layout: S.route.layout, as: S.route.as, thread: null, knew: null}); }
  if ((el = hit('[data-see-as]'))) return go({as: el.dataset.seeAs, person: null});
  if ((el = hit('[data-file]'))) return download(el.dataset.file, el.dataset.name);
  if ((el = hit('[data-image]'))) return openPicture(el.dataset);
  if ((el = hit('[data-text]'))) return openText(el.dataset.text);
  // on the map, the pane is the map's: a thread or what a sender had read opens beside the chat instead
  const away = S.route.layout === 'map' ? id => ({layout: 'chat', chat: id.split(':')[0], person: null}) : () => ({});
  if ((el = hit('[data-thread]'))) return go({thread: el.dataset.thread, knew: null, ...away(el.dataset.thread)}, true);
  if ((el = hit('[data-knew]'))) return go({knew: el.dataset.knew, thread: null, ...away(el.dataset.knew)}, true);
  if ((el = hit('[data-receipts]'))) return openReceipts(el, el.dataset.receipts);
  if ((el = hit('[data-copy]'))) { navigator.clipboard?.writeText(el.dataset.copy); return toast(`Copied ${el.dataset.copy}`); }
  if (hit('[data-close-pane]')) return back({thread: null, knew: null}, true);
  if ((el = hit('[data-fold]'))) return fold(el);
  if ((el = hit('[data-more]'))) { el.previousElementSibling.classList.remove('long'); return el.remove(); }
  if ((el = hit('[data-members]'))) return openMembers(el, el.dataset.members);
  if ((el = hit('[data-sort]'))) return go({sort: el.dataset.sort}, true);
  if ((el = hit('[data-board]'))) return go({layout: 'board', chat: el.dataset.board});
  if ((el = hit('[data-goto]'))) return go({layout: el.dataset.goto, chat: S.route.chat || (S.view && S.view.chat), post: null}, false);
  if (hit('[data-back]')) return back({post: null}, true);
  if (hit('[data-open-post-chat]')) return go({layout: 'chat', chat: S.view.chat, m: +S.view.id.split(':')[1]});
  if ((el = hit('[data-had]'))) return go({layout: 'chat', chat: el.dataset.had, m: el.dataset.upto || 1, had: el.dataset.upto, knew: S.route.knew});
  if ((el = hit('[data-jump]'))) {
    if (S.view instanceof ChatView && S.view.id === el.dataset.chat) return S.view.jump(+el.dataset.jump);
    return go({layout: 'chat', chat: el.dataset.chat, m: el.dataset.jump});
  }
  if ((el = hit('[data-open]'))) return go({layout: S.route.layout === 'map' ? 'chat' : S.route.layout, chat: el.dataset.open, m: el.dataset.at || null});
  if ((el = hit('[data-person]'))) return go({person: el.dataset.person, chat: null, layout: S.route.layout});
  if ((el = hit('[data-post]'))) return go({post: el.dataset.post}, true);
  if ((el = hit('.results .msg'))) {
    if (S.route.layout === 'map') return go({chat: el.dataset.chat, person: null});
    return go({layout: S.route.layout === 'board' ? 'board' : 'chat', chat: el.dataset.chat, m: el.dataset.seq, q: null, person: null});
  }
  if (HOVERLESS.matches && (el = hit('.msg')) && !hit('a, button, .actions')) {  // a tap shows the message's actions
    const was = el.classList.contains('act');
    for (const m of $$('.msg.act')) m.classList.remove('act');
    el.classList.toggle('act', !was);
    if (!was) $('.actions', el).scrollIntoView({block: 'nearest'});
  }
}

function onKey(e) {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '');
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); return overlay.on ? overlay.close() : openSearch(); }
  if (e.key === 'Escape') { if (overlay.on) return overlay.close(); if (S.route.thread || S.route.knew) return back({thread: null, knew: null}, true); }
  if (e.key === 'Enter' && document.activeElement?.dataset.text) return openText(document.activeElement.dataset.text);
  if (typing || overlay.on || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); return openSearch(); }
  if (e.key === ' ' && replay.shown && !['BUTTON', 'A'].includes(document.activeElement?.tagName)) { e.preventDefault(); return replay.toggle(); }
  if ((e.key === 'r' || e.key === 'R') && S.route.layout !== 'board') return replay.on ? replay.exit() : replay.enter();
  if (replay.on && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    return replay.seek(replay.t + (e.key === 'ArrowLeft' ? -1 : 1) * Math.max(10, replay.speed * 5));
  }
  if (['1', '2', '3'].includes(e.key)) return go({layout: ['chat', 'board', 'map'][+e.key - 1], chat: S.route.chat, as: S.route.as});
  if (S.view && S.view.onKey) S.view.onKey(e);
}

function tips() {
  const tip = $('#tip');
  document.addEventListener('touchstart', () => tip.classList.remove('on'), {passive: true});  // a tap's tip goes at the next touch
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

// ---------------------------------------------------------------------- replay: the run again, from any moment

// A replay puts the whole page back at one moment of the run: each chat as it stood then, and every view showing only
// what had been written by then. Playing moves the moment on, and each message arrives in its chat, in the list and on
// the map as the moment passes it. The messages come from a stream the server reads from memory, a few thousand ahead
// of the moment, so a replay at any speed reads no log and costs the agents nothing. The live feed waits while a
// replay runs, and the page takes a new snapshot when it ends.
const SPEEDS = [10, 30, 60, 120, 600];
const SPEED_KEY = 'swarmchat.view.speed';
const FLIGHT = 1.2;  // seconds a message takes to cross the map, at any speed
const AHEAD = 2000;  // messages fetched at a time

const replay = {
  on: false, t: 0, playing: false, dragging: false, busy: false,
  speed: SPEEDS.includes(+localStorage.getItem(SPEED_KEY)) ? +localStorage.getItem(SPEED_KEY) : 60,
  quiet: true,  // jump over stretches with no messages
  act: null,  // messages over the whole run, in buckets, for the bar
  gen: 0,  // moved on by each seek: an answer meant for an older moment is dropped
  queue: [], qi: 0, cursor: 0, more: false, fetching: false, retry: 0,  // what is fetched and not yet reached
  recent: [],  // what arrived in the last few seconds of play, for the map
  seen: new Map(),  // chat -> its messages when it was last open, for the counts in the list
  ended: null, wake: () => {},
  drawn: {},

  get shown() { return this.on || S.route.layout === 'map'; },
  get end() { return this.act ? this.act.end : 0; },
  trail() { return FLIGHT * this.speed; },  // seconds of the run a message is in flight on the map

  init() {
    const el = this.el = $('#bar');
    this.canvas = $('canvas', el); this.ctx = this.canvas.getContext('2d');
    this.head = $('.head', el); this.hov = $('.hov', el); this.track = $('.track', el); this.clock = $('.clock', el);
    // the clock changes every frame while playing: its two lines are text nodes, changed in place
    this.clock.innerHTML = '<b> </b><span> </span>';
    [this.big, this.small] = [$('b', this.clock).firstChild, $('span', this.clock).firstChild];
    this.clock.addEventListener('pointerenter', () => {
      const a = this.act;
      this.clock.title = !a || !a.start ? '' : this.on ? full(iso(Math.max(a.start, this.t))) : `From ${full(a.start)} to ${full(a.end)}`;
    });
    $('.speeds', el).innerHTML = SPEEDS.map(s => `<button data-speed="${s}" title="${s} seconds of the run each second">${s}×</button>`).join('');
    el.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.play != null) this.toggle();
      else if (b.dataset.speed) { this.speed = +b.dataset.speed; localStorage.setItem(SPEED_KEY, this.speed); this.draw(true); }
      else if (b.dataset.quiet != null) { this.quiet = !this.quiet; this.draw(true); }
      else if (b.dataset.live != null) this.exit();
    });
    const timeAt = e => {
      const r = this.track.getBoundingClientRect(), a = this.act;
      return a.start + Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (a.end - a.start);
    };
    this.track.addEventListener('pointerdown', e => {
      if (!this.act || !this.act.start) return;
      this.track.setPointerCapture(e.pointerId);
      this.dragging = true;
      this.t = timeAt(e);
      if (this.on) this.seekSoon(); else this.enter(this.t, false);
      this.draw();
    });
    this.track.addEventListener('pointermove', e => {
      if (!this.act || !this.act.start) return;
      const t = timeAt(e), r = this.track.getBoundingClientRect();
      if (this.dragging && this.on) { this.t = t; this.seekSoon(); this.draw(); }
      const i = Math.min(this.act.counts.length - 1, Math.floor((t - this.act.start) / this.act.width));
      this.hov.textContent = `${span(t - this.act.start)} · ${plural(this.act.counts[i] || 0, 'message')}`;
      this.hov.style.left = Math.max(0, Math.min(r.width - this.hov.offsetWidth, e.clientX - r.left - this.hov.offsetWidth / 2)) + 'px';
      this.hov.classList.add('on');
    });
    this.track.addEventListener('pointerup', e => {
      if (!this.dragging) return;
      this.dragging = false;
      if (this.on) this.seek(timeAt(e));
    });
    this.track.addEventListener('pointerleave', () => this.hov.classList.remove('on'));
    // only the strip is drawn again: a change to the buttons here would resize the bar while the browser is still
    // telling of its last size
    new ResizeObserver(([e]) => { this.tw = e.contentRect.width; this.th = e.contentRect.height; this.drawn.bucket = null; this.draw(); }).observe(this.track);
    setInterval(() => { if (this.shown && !this.on) this.activity(); }, 30000);  // a live run grows
  },

  async activity() {
    const w = this.track.getBoundingClientRect().width || 900;
    const r = await api('view_activity', {buckets: Math.max(60, Math.min(600, Math.round(w / 5)))});
    if (!this.act || r.end !== this.act.end || r.counts.length !== this.act.counts.length || !this.on) this.act = r;
    this.draw(true);
  },
  bump(t) {  // a live message: one more in its bucket
    const a = this.act;
    if (!a || !a.start || this.on) return;
    const i = Math.floor((t - a.start) / a.width);
    if (i >= a.counts.length) return this.activitySoon();
    if (i >= 0) a.counts[i]++;
    this.drawSoon();
  },
  activitySoon: throttle(() => replay.activity(), 5000),
  drawSoon: throttle(() => replay.draw(true), 250),

  // ---- moving through the run
  async enter(t, play = true) {
    if (this.on) return;
    if (S.route.layout === 'board') return;
    if (!this.act || !this.act.start) await this.activity();
    if (!this.act.start) return toast('There is nothing to replay yet: no one has written.');
    this.on = true;
    this.ended = new Promise(r => { this.wake = r; });
    document.body.classList.add('replaying');
    setLive('rp');
    this.show();
    requestAnimationFrame(n => this.tick(n));
    await this.seek(t ?? this.act.start - 0.001);
    if (play && this.on) this.play();
  },
  async exit(reroute = true) {
    if (!this.on) return;
    this.on = false; this.playing = false; this.dragging = false; this.gen++; this.queue = []; this.recent = [];
    document.body.classList.remove('replaying');
    this.draw(true);
    await loadSnapshot();
    S.as = null;
    if (S.route.as) await loadAs();
    setLive(S.server.read_only ? 'ro' : 'on');
    this.wake();
    S.viewKey = null; S.paneKey = null;
    if (reroute) await route();
    this.activity();
  },
  play() {
    if (!this.on) return this.enter();
    if (this.atEnd()) return this.seek(this.act.start - 0.001).then(() => { this.playing = true; this.draw(true); });
    this.playing = true;
    this.draw(true);
  },
  pause() { this.playing = false; this.draw(true); },
  toggle() { if (this.playing) this.pause(); else this.play(); },
  atEnd() { return !this.more && this.qi >= this.queue.length && this.t >= this.end; },
  seekSoon: throttle(() => { if (replay.on) replay.seek(replay.t); }, 200),

  // Puts the page at the moment t: every chat as it stood then, and the messages after it to come.
  async seek(t) {
    const a = this.act;
    t = Math.max(a.start - 0.001, Math.min(a.end, t));
    const gen = ++this.gen;
    this.t = t; this.queue = []; this.qi = 0; this.more = true; this.fetching = true; this.busy = true;
    let at, ahead;
    try {
      [at, ahead] = await Promise.all([api('view_at', {at: t, as: S.route.as || undefined}),
        api('view_replay', {after: t - 2 * this.trail(), limit: AHEAD})]);  // from a little before, for what is in flight
    } catch (e) {
      if (gen === this.gen) { this.fetching = false; this.busy = false; toast(e.message); }
      return;
    }
    if (gen !== this.gen || !this.on) return;
    this.apply(at);
    const list = ahead.messages;
    let i = 0;
    while (i < list.length && list[i][0] <= t) i++;
    this.recent = list.slice(0, i); this.queue = list.slice(i);
    this.cursor = list.length ? list[list.length - 1] : t; this.more = ahead.more;
    this.fetching = false; this.busy = false;
    this.seen = new Map([...S.chats.values()].map(c => [c.id, c.messages]));  // nothing is new at a new moment
    if (S.view && S.view.onSeek) S.view.onSeek(); else S.viewKey = null;
    if (S.pane && S.pane.onSeek) S.pane.onSeek(); else if (S.pane) S.paneKey = null;
    if (S.viewKey === null || S.paneKey === null) await route(); else { side.build(); renderTop(); }
    this.draw(true);
  },
  // The agent seen as, at the same moment: when it changes in a replay.
  async reload() {
    const gen = this.gen;
    this.busy = true;
    try {
      const r = await api('view_at', {at: this.t, as: S.route.as || undefined});
      if (gen === this.gen) this.apply(r);
    } finally { if (gen === this.gen) this.busy = false; }
  },
  // Every chat as the server says it stood at the moment.
  apply(r) {
    S.total = 0; S.sent = new Map(); S.last = new Map();
    for (const c of S.chats.values()) {
      const x = r.chats[c.id] || {born: false, messages: 0, sent: {}, recent: 0};
      c.born = x.born || x.messages > 0; c.messages = x.messages; c.sent = x.sent; c.recent = x.recent;
      c.last = x.last ? {...x.last, text: '', deleted: false, files: 0} : null;
      c.last_at = x.last_at || c.created_at;
      S.total += x.messages;
      for (const [u, n] of Object.entries(x.sent)) S.sent.set(u, (S.sent.get(u) || 0) + n);
      if (x.last) { const t = ms(x.last.at) / 1000; if (!(S.last.get(x.last.from) >= t)) S.last.set(x.last.from, t); }
    }
    S.as = null;
    if (S.route.as) this.applyAs(r.chats, null);
  },
  // What the agent seen as had read in each of its chats at that moment. Messages since the answer was asked for are
  // counted on top.
  applyAs(chats, since) {
    const id = S.route.as, old = S.as;
    const mine = [...S.chats.values()].filter(c => chats[c.id] && chats[c.id].as && c.born)
      .sort((a, b) => (b.last_at > a.last_at) - (b.last_at < a.last_at));
    const later = since == null ? [] : this.recent.filter(m => m[0] > since && m[3] !== id);
    S.as = {user: id, replay: true, order: mine.map(c => c.id), positions: {}, unread: [], unreadLeft: 0, chats: new Map()};
    for (const c of mine) {
      const x = chats[c.id].as;
      S.as.positions[c.id] = x.upto;
      S.as.chats.set(c.id, {id: c.id, mentioned: x.mentioned, notify: old?.chats.get(c.id)?.notify || 'all',
        unread: x.unread + later.filter(m => m[1] === c.id).length});
    }
  },
  // Reads are not in the stream: while playing as an agent, its unread counts are asked for again every few seconds.
  refreshAs: throttle(async () => {
    const r = replay, gen = r.gen, t = r.t, id = S.route.as;
    if (!r.on || !id) return;
    const at = await api('view_at', {at: t, as: id, mine: true});
    if (gen !== r.gen || !r.on || S.route.as !== id) return;
    r.applyAs(at.chats, t);
    buildSide();
    if (S.view && S.view.onAs) S.view.onAs();
  }, 2000),

  // ---- playing
  tick(now) {
    if (!this.on) return;
    requestAnimationFrame(n => this.tick(n));
    const dt = this.last ? Math.min(1, (now - this.last) / 1000) : 0;  // more than a second: the tab was hidden
    this.last = now;
    if (this.playing && !this.dragging && !this.busy) {
      const next = this.queue[this.qi];
      let t = this.t + dt * this.speed;
      // a stretch with nothing in it for more than two seconds of play: jump to just before its end
      if (this.quiet && next && next[0] - t > this.speed * 2) t = next[0] - this.speed * 0.6;
      else if (this.quiet && !next && !this.more) t = Math.max(t, this.end);
      if (this.more) t = Math.min(t, Array.isArray(this.cursor) ? this.cursor[0] : this.cursor);  // never past what is fetched
      this.t = Math.max(this.t, Math.min(t, Math.max(this.end, next ? next[0] : 0)));
      const due = [];
      while (this.qi < this.queue.length && this.queue[this.qi][0] <= this.t) due.push(this.queue[this.qi++]);
      if (due.length) this.arrive(due);
      if (this.atEnd()) this.pause();
      if (this.more && !this.fetching && this.queue.length - this.qi < AHEAD / 2 && now > this.retry) this.fill();
      if (S.route.as) this.refreshAs();
    }
    this.draw();
  },
  async fill() {
    this.fetching = true;
    const gen = this.gen;
    try {
      const r = await api('view_replay', {after: this.cursor, limit: AHEAD});
      if (gen !== this.gen) return;
      this.queue = this.queue.slice(this.qi).concat(r.messages); this.qi = 0;
      if (r.messages.length) this.cursor = r.messages[r.messages.length - 1];
      this.more = r.more;
    } catch (e) {
      this.retry = performance.now() + 2000;
    } finally { if (gen === this.gen) this.fetching = false; }
  },
  // Messages the moment has just passed, as [time, chat, seq, sender]: counted, and handed to the views.
  arrive(list) {
    for (const m of list) {
      const [t, chat, seq, from] = m, c = S.chats.get(chat);
      if (!c) continue;
      const at = iso(t);
      c.messages++; c.born = true; c.recent = (c.recent || 0) + 1;
      c.last = {seq, from, at, text: '', deleted: false, files: 0}; c.last_at = at;
      c.sent[from] = (c.sent[from] || 0) + 1;
      S.total++;
      S.sent.set(from, (S.sent.get(from) || 0) + 1);
      S.last.set(from, t);
      const mine = S.as && S.as.chats.get(chat);
      if (mine && from !== S.as.user) mine.unread++;
    }
    this.recent.push(...list);
    const old = this.t - 2 * this.trail();
    if (this.recent.length > 6000 || this.recent[0][0] < old) {
      let i = 0;
      while (i < this.recent.length && this.recent[i][0] < old) i++;
      this.recent = this.recent.slice(Math.max(i, this.recent.length - 6000));
    }
    if (S.view && S.view.onReplay) S.view.onReplay(list);
    if (S.pane && S.pane.onReplay) S.pane.onReplay(list);
    buildSide();
  },

  // ---- the bar
  show() {
    if (this.shown && (!this.act || !this.act.start)) this.activity(); else this.draw(true);
  },
  draw(all) {
    const a = this.act, el = this.el;
    if (!el || !this.shown) return;
    const t = this.on ? this.t : a ? a.end : 0;
    const state = this.on ? (this.playing ? 'pause' : this.atEnd() ? 'again' : 'play') : 'replay';
    if (all || state !== this.drawn.state) {
      const play = $('[data-play]', el);
      play.innerHTML = {pause: '❚❚ Pause', play: '▶ Play', again: '↻ Replay', replay: '▶ Replay'}[state];
      play.title = this.on ? 'Play or pause (space)' : 'Play the run again from its start (R)';
      for (const b of $$('[data-speed]', el)) b.classList.toggle('on', +b.dataset.speed === this.speed);
      $('[data-quiet]', el).classList.toggle('on', this.quiet);
      $('[data-quiet]', el).hidden = $('.speeds', el).hidden = $('[data-live]', el).hidden = !this.on;
      this.drawn.state = state;
    }
    const length = a && a.start ? Math.max(0.001, a.end - a.start) : 0;
    const big = !length ? 'No messages' : this.on ? span(t - a.start) : 'Live', small = !length ? '' : this.on ? `of ${span(length)}` : `${span(length)} so far`;
    if (this.big.data !== big) this.big.data = big;
    if (this.small.data !== small) this.small.data = small;
    if (!length) return;
    const frac = Math.max(0, Math.min(1, (t - a.start) / length));
    this.head.style.transform = `translateX(${Math.round(frac * (this.tw || 0))}px)`;
    this.head.hidden = !this.on;
    const bucket = this.on ? Math.floor(frac * a.counts.length) : -1;
    if (!all && bucket === this.drawn.bucket) return;
    this.drawn.bucket = bucket;
    const c = this.canvas, W = this.tw, H = this.th, dpr = devicePixelRatio || 1;
    if (!W) return;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    const ctx = this.ctx, n = a.counts.length, bw = W / n, top = Math.max(...a.counts, 1);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const played = ink().played, future = ink().unplayed;
    for (let i = 0; i < n; i++) {
      if (!a.counts[i]) continue;
      const h = Math.max(2, (H - 4) * Math.sqrt(a.counts[i] / top));
      ctx.fillStyle = !this.on || i < bucket ? played : future;
      ctx.fillRect(i * bw + 0.5, H - h, Math.max(1, bw - 1), h);
    }
  },
};

// ---------------------------------------------------------------------- the map: everyone at once

// Categorical colours in a fixed order, made to be told apart on a light or a dark background. Past eight, the rest are "other".
const PALETTE = ['#4269d0', '#efb118', '#ff725c', '#6cc5b0', '#3ca951', '#ff8ab7', '#a463f2', '#97bbf5'];
const OTHER = '#7b8394';
const MAP = {pos: new Map(), view: null, colorBy: null, lens: !COARSE.matches, mode: null};  // kept while the page is open
const RING = 300;  // the radius of the ring agents sit on, in the map's own units
const RING_MOST = 60;  // more agents than this are laid out as a web

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

// The map shows every agent and every channel and group chat, and each message as a dot that flies from its sender to
// its chat, in the sender's colour. Up to RING_MOST agents sit on a ring, in groups by the field the legend picks, with
// each chat inside near its members and each direct chat a chord across. A larger swarm is laid out by forces ("web"),
// so agents that talk sit together. The pane beside it shows the chat or agent picked, or every message as it comes.
class MapView {
  constructor(main) {
    this.main = main; this.pulses = []; this.lit = new Map(); this.hover = null; this.hoverEdge = null; this.selected = null;
    main.innerHTML = html`<div class="mapwrap"><div class="stage"><canvas class="layer"></canvas><canvas class="layer"></canvas><canvas class="graph"></canvas></div><div class="legend"></div>
<div class="mapctl"><div class="seg small"><button data-mode="ring" title="Agents on a ring, their chats inside">Ring</button><button data-mode="web" title="Agents that talk sit together">Web</button></div><button class="btn" data-lens title="A lens that magnifies what is under the mouse (F)">Lens</button><button class="btn" data-fit title="Show everyone (0)">Fit</button></div>
<div class="mapcard" hidden></div></div>`.s;
    // Three canvases, one over another: links at the bottom, then what moves, then chats, agents and names on top, which
    // also takes the mouse. Only the middle one is drawn every frame.
    [this.base, this.fly] = $$('canvas.layer', main);
    this.canvas = $('canvas.graph', main); this.ctx = this.canvas.getContext('2d');
    this.legend = $('.legend', main); this.card = $('.mapcard', main);
    S.mapPane = true;
    this.listen();
    this.size();
    this.build(true);
    this.resize = new ResizeObserver(() => { this.size(); if (MAP.mode === 'ring') this.fit(); this.draw(); });
    this.resize.observe(this.canvas);
    this.onRoute();
  }
  destroy() {
    this.dead = true; this.resize.disconnect(); if (this.worker) this.worker.terminate();
    cancelAnimationFrame(this.raf); this.raf = 0; S.mapPane = false;
    if (this.side && this.side.destroy) this.side.destroy();
    $('#pane').classList.remove('open', 'mappane'); $('#pane').replaceChildren();
  }
  size() {
    const dpr = Math.min(2, devicePixelRatio || 1), c = this.canvas, r = c.getBoundingClientRect();  // 3, as on many phones, would draw 2.25 times as many pixels for no gain anyone sees
    for (const x of [this.base, this.fly, c]) { x.width = Math.max(1, Math.round(r.width * dpr)); x.height = Math.max(1, Math.round(r.height * dpr)); }
    c.w = r.width; c.h = r.height;
    this.dpr = dpr;
    this.layersKey = null;
  }
  controls() {
    for (const b of $$('[data-mode]', this.main)) b.classList.toggle('on', b.dataset.mode === MAP.mode);
    $('.stage', this.main).classList.toggle('web', MAP.mode === 'web');  // on a web, what moves is drawn over the names
    const lens = $('[data-lens]', this.main);
    lens.classList.toggle('on', MAP.lens);
    lens.hidden = MAP.mode === 'ring';
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
    const nbrs = new Map();
    for (const e of edges) { (nbrs.get(e.a) || nbrs.set(e.a, []).get(e.a)).push(e.b); (nbrs.get(e.b) || nbrs.set(e.b, []).get(e.b)).push(e.a); }
    this.nodes = nodes; this.index = index; this.edges = edges; this.nbrs = nbrs;
    // New counts are shown four times a second, or once on a web of thousands of links, which takes long to paint
    if ((edges.length > 2000) !== this.big) { this.big = edges.length > 2000; this.reweigh = throttle(() => { if (!this.dead) this.weigh(); }, this.big ? 1000 : 250); }
    this.direct = new Map(edges.filter(e => e.kind === 'direct').map(e => [e.chat, e]));
    MAP.mode ||= nodes.filter(n => n.kind === 'agent').length <= RING_MOST ? 'ring' : 'web';
    this.controls();
    this.weigh();
    this.colors();
    if (MAP.mode === 'ring') {
      if (this.worker) { this.worker.terminate(); this.worker = null; }
      this.ring();
      this.fit();
      return;
    }
    const spread = Math.sqrt(nodes.length) * 40;
    let placed = 0;
    nodes.forEach((n, i) => {
      const p = MAP.pos.get(n.key);
      if (p) { n.x = p.x; n.y = p.y; placed++; return; }
      const near = (nbrs.get(i) || []).map(j => MAP.pos.get(nodes[j].key)).find(Boolean);
      const a = Math.random() * Math.PI * 2, r = near ? 20 + Math.random() * 30 : Math.sqrt(Math.random()) * spread;
      n.x = (near ? near.x : 0) + Math.cos(a) * r; n.y = (near ? near.y : 0) + Math.sin(a) * r;
    });
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
      if (MAP.mode !== 'web') return;
      const {x: X, y: Y, done} = m.data;
      this.nodes.forEach((d, i) => { d.x = X[i]; d.y = Y[i]; MAP.pos.set(d.key, {x: d.x, y: d.y}); });
      this.moved = (this.moved || 0) + 1;
      if (this.autoFit) this.fit();
      if (done) this.autoFit = false;
      this.kick();
    };
    this.autoFit = fresh;
    this.worker.postMessage({x, y, mass, a, b, pull, k, iterations: fresh ? 320 : 90, heat: fresh ? Math.sqrt(n) * k / 3 : k * 1.5});
  }
  // The ring: agents in order of their group, then of their ids, with a space between groups. Each chat sits toward
  // its members: in the middle when they are all around the ring, near them when they sit together. Chats that land
  // on each other are pushed apart.
  ring() {
    const agents = this.nodes.filter(n => n.kind === 'agent'), hubs = this.nodes.filter(n => n.kind === 'hub');
    const by = MAP.colorBy && MAP.colorBy !== 'activity' ? MAP.colorBy : null;
    const group = n => by ? String((user(n.user).fields || {})[by] ?? '') : '';
    const size = new Map();
    for (const n of agents) size.set(group(n), (size.get(group(n)) || 0) + 1);
    agents.sort((a, b) => size.get(group(b)) - size.get(group(a)) || natural(group(a), group(b)) || natural(a.user, b.user));
    const gaps = size.size > 1 && size.size < agents.length ? size.size : 0, slots = Math.max(1, agents.length + gaps);
    this.arcs = [];
    let slot = 0, prev = null;
    for (const n of agents) {
      const g = group(n);
      if (gaps && prev !== null && g !== prev) slot++;
      const a = -Math.PI / 2 + slot / slots * Math.PI * 2;
      n.ang = a; n.x = RING * Math.cos(a); n.y = RING * Math.sin(a);
      if (gaps && g !== prev) this.arcs.push({group: g, a0: a, a1: a, color: (this.groupColor && this.groupColor.get(g)) || OTHER});
      else if (gaps) this.arcs[this.arcs.length - 1].a1 = a;
      prev = g; slot++;
    }
    this.rr = Math.min(18, Math.PI * RING / slots * 0.62);  // an agent's radius: room on the ring for each
    // chats: bigger with more members and more messages, all smaller when there are many
    let area = 0;
    for (const h of hubs) {
      const c = S.chats.get(h.chat);
      h.hr = 8 + 18 * Math.sqrt(c.members.length / Math.max(1, agents.length)) + Math.min(8, Math.sqrt(c.messages) * 0.5);
      area += h.hr * h.hr;
    }
    const room = 0.3 * (RING * 0.75) ** 2, shrink = area > room ? Math.sqrt(room / area) : 1;
    for (const h of hubs) {
      h.hr = Math.max(5, h.hr * shrink);
      let sx = 0, sy = 0, k = 0;
      for (const u of S.chats.get(h.chat).members) {
        const i = this.index.get('u:' + u);
        if (i == null) continue;
        sx += Math.cos(this.nodes[i].ang); sy += Math.sin(this.nodes[i].ang); k++;
      }
      h.x = k ? sx / k * RING * 0.62 : 0; h.y = k ? sy / k * RING * 0.62 : 0;
    }
    for (let it = 0; it < 120; it++) {
      let moved = false;
      for (let i = 0; i < hubs.length; i++) for (let j = i + 1; j < hubs.length; j++) {
        const a = hubs[i], b = hubs[j], want = a.hr + b.hr + 6;
        let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy);
        if (d >= want) continue;
        if (d < 0.01) { dx = Math.cos(i * 7 + j); dy = Math.sin(i * 7 + j); d = 1; }
        const push = (want - d) / 2;
        a.x -= dx / d * push; a.y -= dy / d * push; b.x += dx / d * push; b.y += dy / d * push;
        moved = true;
      }
      for (const h of hubs) {
        const d = Math.hypot(h.x, h.y), most = RING - this.rr - h.hr - 10;
        if (d > most) { h.x *= most / d; h.y *= most / d; }
      }
      if (!moved) break;
    }
    // names go outside the ring when they do not fit inside a circle, and run out from the centre when there are many
    this.agents = agents.length;
    this.outside = agents.some(n => name(n.user).length > 3);
    this.ctx.font = `600 12.5px ${MAP.font ||= getComputedStyle(document.body).fontFamily}`;
    this.labelRoom = this.outside ? Math.min(150, Math.max(...agents.map(n => this.ctx.measureText(name(n.user)).width)) + 24) : 0;
  }
  fit() {
    const W = this.canvas.w, H = this.canvas.h;
    if (!this.nodes.length || !W) { this.view = {x: 0, y: 0, k: 1}; return; }
    if (MAP.mode === 'ring') {
      // As large as fits beside the legend: a circle needs to keep clear only of the legend's corner, so a few
      // centres are tried, from the middle down, and the one that leaves the largest circle wins.
      const l = this.legend, x0 = l.offsetLeft - 8, x1 = l.offsetLeft + l.offsetWidth + 8, y1 = l.offsetTop + l.offsetHeight + 8;
      const names = this.agents > 12 ? this.labelRoom : this.outside ? 30 : 0;
      let best = {r: 0, dy: 0};
      for (let dy = 0; dy <= y1; dy += y1 / 6) {
        const cx = W / 2, cy = H / 2 + dy, clear = Math.hypot(Math.max(x0 - cx, 0, cx - x1), Math.max(cy - y1, 0));
        const r = Math.min(W / 2 - 12, H / 2 - dy - 12, clear);
        if (r > best.r) best = {r, dy};
      }
      const k = Math.max(0.2, (best.r - names - 10) / (RING + this.rr));
      this.view = {x: 0, y: -best.dy / k, k};
      return this.kick();
    }
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of this.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const k = Math.min(4, 0.86 * Math.min(W / Math.max(60, x1 - x0), H / Math.max(60, y1 - y0)));
    this.view = MAP.view = {x: (x0 + x1) / 2, y: (y0 + y1) / 2, k};
    this.kick();
  }
  // How much each link and agent carries, from the chats as they stand (at the moment, in a replay).
  weigh() {
    let maxE = 1, maxA = 1, maxH = 1;
    for (const e of this.edges) {
      const c = S.chats.get(e.chat);
      e.w = !c ? 0 : e.kind === 'direct' ? c.messages : c.sent[e.user] || 0;
      if (e.w0 == null) e.w0 = e.w;
      maxE = Math.max(maxE, e.w);
    }
    for (const n of this.nodes) {
      if (n.kind === 'agent') { n.w = S.sent.get(n.user) || 0; n.r = 2.5 + 1.6 * Math.sqrt(Math.log2(1 + n.w)); maxA = Math.max(maxA, n.w); }
      else { const c = S.chats.get(n.chat); n.w = c.messages; n.r = Math.min(16, 3 + Math.sqrt(c.members.length)); maxH = Math.max(maxH, n.w); }
    }
    this.maxE = maxE; this.maxA = maxA; this.maxH = maxH; this.version = (this.version || 0) + 1;
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
  // On a ring each agent wears its own colour, and the field picked in the legend groups the ring, with a band in the
  // field's colour around each group. On a web the field colours the agents.
  colors() {
    const fields = this.fields();
    const ring = MAP.mode === 'ring', small = this.nodes.filter(n => n.kind === 'agent').length <= 16;
    if (!MAP.colorBy || (MAP.colorBy !== 'activity' && !fields.some(f => f.key === MAP.colorBy))) MAP.colorBy = fields.length && !(ring && small) ? fields[0].key : 'activity';
    const by = MAP.colorBy;
    let items = [];
    this.groupColor = new Map();
    if (by === 'activity') {
      for (const n of this.nodes) if (n.kind === 'agent') {
        const t = this.maxA > 1 ? Math.log1p(n.w || 0) / Math.log1p(this.maxA) : 0;
        n.color = ink().dark ? `hsl(220 ${40 + 50 * t}% ${30 + 45 * t}%)` : `hsl(220 ${35 + 50 * t}% ${82 - 40 * t}%)`;
      }
      if (!ring) items = ink().dark ? [['quiet', 'hsl(220 40% 30%)'], ['busy', 'hsl(220 90% 75%)']] : [['quiet', 'hsl(220 35% 82%)'], ['busy', 'hsl(220 85% 42%)']];
    } else {
      const vals = [...fields.find(f => f.key === by).vals.entries()].sort((p, q) => q[1] - p[1]);
      this.groupColor = new Map(vals.slice(0, 8).map(([v], i) => [v, PALETTE[i]]));
      for (const n of this.nodes) if (n.kind === 'agent') { const v = (user(n.user).fields || {})[by]; n.color = this.groupColor.get(String(v)) || OTHER; }
      items = vals.slice(0, 8).map(([v, cnt]) => [v, this.groupColor.get(v), cnt]);
      const rest = vals.slice(8).reduce((s, [, cnt]) => s + cnt, 0) + [...S.users.values()].filter(u => !(u.fields || {})[by]).length;
      if (rest) items.push(['other', OTHER, rest]);
    }
    if (ring) for (const n of this.nodes) if (n.kind === 'agent') n.color = userColor(n.user);
    this.version = (this.version || 0) + 1;
    for (const n of this.nodes) if (n.kind === 'hub') n.color = S.chats.get(n.chat).kind === 'channel' ? ink().channel : ink().group;
    const opts = [...fields.map(f => f.key), 'activity'];
    this.legend.innerHTML = html`<span class="lh">${ring ? 'Grouped by' : 'Agents by'} <select data-colorby>${opts.map(o => html`<option value="${o}"${o === by ? new Safe(' selected') : ''}>${o === 'activity' && ring ? 'nothing' : o}</option>`)}</select></span>
${items.map(([label, c, cnt]) => html`<span class="li" title="${label}${cnt != null ? ` · ${num(cnt)}` : ''}"><span class="${ring ? 'band' : 'dot'}" style="background:${c}"></span><span class="lbl">${label}${cnt != null ? html`<span class="n">${num(cnt)}</span>` : ''}</span></span>`)}
<span class="shapes">${ring ? html`<span><span class="ring"></span>chat</span><span><span class="chord"></span>direct chat</span><span><span class="flying"></span>message</span>`
    : html`<span><span class="sq" style="background:var(--channel)"></span>channel</span><span><span class="sq" style="background:var(--group)"></span>group</span><span>— direct chat</span>`}</span>
<span class="hint">Click anything to see its messages. Scroll to zoom${ring ? '' : ', F for a lens'}.</span>`.s;
    this.legend.classList.toggle('ring', ring);  // on a ring, one line until the mouse is over it: the ring needs the room
    $('select', this.legend).onchange = e => { MAP.colorBy = e.target.value; this.colors(); if (MAP.mode === 'ring') { this.ring(); this.fit(); } this.kick(); };
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
    const lens = MAP.mode === 'web' && MAP.lens && m && !this.dragging, R = Math.min(260, Math.max(140, Math.min(W, H) / 3.2)), dist = 3.2;
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
  // The messages in flight now: [from node, to node, how far along, colour]. In a replay they move with the moment, so
  // they stop when it is paused. Live, each crosses in FLIGHT seconds.
  flights() {
    const out = [], now = performance.now();
    const add = (chat, from, p) => {
      const c = S.chats.get(chat), i = this.index.get('u:' + from);
      if (!c || i == null) return;
      const j = c.kind === 'direct' ? this.index.get('u:' + c.members.find(u => u !== from)) : this.index.get('c:' + chat);
      if (j != null) out.push([i, j, p, this.nodes[i].color]);
    };
    if (replay.on) {
      const t = replay.t, trail = replay.trail(), list = replay.recent;
      for (let i = list.length - 1; i >= 0 && out.length < 3000; i--) {
        const age = t - list[i][0];
        if (age > trail) break;
        if (age >= 0) add(list[i][1], list[i][3], age / trail);
      }
    } else {
      this.pulses = this.pulses.filter(p => now < p.t0 + FLIGHT * 1000);
      for (const p of this.pulses) add(p.chat, p.from, (now - p.t0) / (FLIGHT * 1000));
    }
    return out;
  }
  glow(u) {  // 1 just after an agent wrote, fading to 0
    if (replay.on) { const t = S.last.get(u); return t == null ? 0 : Math.max(0, 1 - (replay.t - t) / (2 * replay.trail())); }
    const t = this.lit.get(u);
    return t == null ? 0 : Math.max(0, 1 - (performance.now() - t) / (2000 * FLIGHT));
  }
  draw() {
    if (!this.canvas.w || !this.nodes) return;
    const W = this.canvas.w, H = this.canvas.h;
    this.project();
    if (MAP.mode === 'ring') this.drawRing(W, H); else this.drawWeb(W, H);
  }
  // The ring in three coats. What changes only with the counts, the mouse or the view (links, chats, agents and names)
  // is painted into two layers kept between frames, and each frame lays the glows and the messages in flight between
  // them: a frame costs two copies and a few dots, however many links there are.
  drawRing(W, H) {
    const v = this.view, key = [W, H, v.x, v.y, v.k, this.hover, this.hoverEdge && this.hoverEdge.chat, this.selected, this.selChat,
      this.version, S.route.as, S.route.person, S.route.chat].join();
    if (key !== this.layersKey) { this.paintRing(W, H); this.layersKey = key; }
    const ctx = this.layer(this.fly, W, H), k = v.k, ra = this.rr * k, rel = this.related(), focus = this.hover ?? this.selected;
    const near = focus == null ? null : new Set([focus, ...(this.nbrs.get(focus) || [])]);
    for (let i = 0; i < this.nodes.length; i++) {  // an agent glows just after it writes
      const n = this.nodes[i];
      if (n.kind !== 'agent') continue;
      const g = this.glow(n.user);
      if (g <= 0) continue;
      const dim = (rel && !rel.agents.has(n.user)) || (near && !near.has(i));
      ctx.fillStyle = n.color; ctx.globalAlpha = (dim ? 0.08 : 0.2) * g;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, ra * 1.65, 0, Math.PI * 2); ctx.fill();
    }
    const dot = Math.max(3, Math.min(5.5, 4.2 * Math.sqrt(k)));
    for (const [i, j, p, color] of this.flights()) {  // messages in flight, under what they fly between
      const a = this.nodes[i], b = this.nodes[j], e = p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2;
      ctx.globalAlpha = 1 - p * 0.55;
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(a.sx + (b.sx - a.sx) * e, a.sy + (b.sy - a.sy) * e, dot, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  layer(c, W, H) {  // one of the canvases, cleared, drawn on in page pixels
    const ctx = c.getContext('2d'), dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    return ctx;
  }
  paintRing(W, H) {
    const k = this.view.k, rel = this.related(), focus = this.hover ?? this.selected, I = ink();
    const sel = this.selChat, hot = this.hoverEdge ? this.hoverEdge.chat : null;
    const lit = new Set();
    if (focus != null) { lit.add(focus); for (const j of this.nbrs.get(focus) || []) lit.add(j); }
    const live = id => !replay.on || S.chats.get(id)?.born;
    const ra = this.rr * k;
    let ctx = this.layer(this.base, W, H);
    ctx.lineCap = 'round';
    // spokes from each agent to its chats, then direct chats as chords, thicker with more messages
    const spokes = [[], []];
    for (const e of this.edges) {
      if (e.kind !== 'spoke' || !live(e.chat)) continue;
      const a = this.nodes[e.a], b = this.nodes[e.b];
      spokes[focus != null && (e.a === focus || e.b === focus) ? 1 : 0].push(a.sx, a.sy, b.sx, b.sy);
    }
    spokes.forEach((pts, on) => {
      ctx.strokeStyle = on ? I.text : `rgba(${I.spoke}, ${focus != null || rel ? 0.45 : 1})`;
      ctx.globalAlpha = on ? 0.35 : 1;
      ctx.lineWidth = on ? 1.4 : 1.1;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i += 4) { ctx.moveTo(pts[i], pts[i + 1]); ctx.lineTo(pts[i + 2], pts[i + 3]); }
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
    for (const e of this.edges) {
      if (e.kind !== 'direct' || (!e.w && e.chat !== sel && e.chat !== hot)) continue;
      const a = this.nodes[e.a], b = this.nodes[e.b];
      const on = e.chat === sel || e.chat === hot || (focus != null && (e.a === focus || e.b === focus));
      const dim = (rel && !rel.chats.has(e.chat)) || (focus != null && !on && !sel);
      const t = Math.log1p(e.w) / Math.log1p(this.maxE);
      ctx.strokeStyle = `rgba(${I.edgeDm}, ${on ? 0.7 : dim ? 0.06 : 0.12 + 0.2 * t})`;
      ctx.lineWidth = (0.8 + 3.6 * t) * Math.min(1.4, Math.max(0.7, k));
      ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
    }
    // groups of agents: a band in the group's colour just outside the ring
    if (this.arcs.length > 1) {
      const R = RING * k + ra + 6, cx = (0 - this.view.x) * k + W / 2, cy = (0 - this.view.y) * k + H / 2, half = Math.PI / Math.max(1, this.nodes.length) * 0.8;
      ctx.lineWidth = 3;
      for (const arc of this.arcs) {
        ctx.strokeStyle = arc.color;
        ctx.beginPath(); ctx.arc(cx, cy, R, arc.a0 - Math.min(half, ra / R), arc.a1 + Math.min(half, ra / R)); ctx.stroke();
      }
    }
    ctx = this.layer(this.canvas, W, H);
    // chats: a disc with the name inside when it fits
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const font = MAP.font ||= getComputedStyle(document.body).fontFamily;
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      if (n.kind !== 'hub' || !live(n.chat)) continue;
      const c = S.chats.get(n.chat), r = n.hr * k, picked = S.route.chat === n.chat;
      const dim = (rel && !rel.chats.has(n.chat)) || (focus != null && !lit.has(i));
      ctx.globalAlpha = dim ? 0.3 : 1;
      ctx.fillStyle = I.panel; ctx.strokeStyle = picked ? I.text : I.line2; ctx.lineWidth = picked ? 2 : 1.5;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      const label = c.name ? '#' + c.name : title(c, null);
      let size = Math.round(Math.max(9, Math.min(12.5, r * 0.42)) * 2) / 2;
      const widths = MAP.widths ||= new Map();
      ctx.font = `600 ${size}px ${font}`;
      let w = widths.get(size + label);
      if (w == null) widths.set(size + label, w = ctx.measureText(label).width);
      if (w > r * 2 - 6 && size * (r * 2 - 6) / w >= 9) {  // a smaller size that fits, down to 9px
        w = r * 2 - 6; size = Math.floor(size * w / widths.get(size + label) * 2) / 2;
        ctx.font = `600 ${size}px ${font}`;
      }
      if (w <= r * 2 - 6) { ctx.fillStyle = I.text; ctx.fillText(label, n.sx, n.sy); }
      else if (r >= 7 && c.kind === 'channel') { ctx.fillStyle = I.muted; ctx.font = `700 ${Math.min(13, r)}px ${font}`; ctx.fillText('#', n.sx, n.sy + 0.5); }
    }
    // agents: a disc in its colour, with its name or initials inside
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      if (n.kind !== 'agent') continue;
      const dim = (rel && !rel.agents.has(n.user)) || (focus != null && !lit.has(i));
      ctx.globalAlpha = dim ? 0.25 : 1;
      ctx.fillStyle = n.color;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, ra, 0, Math.PI * 2); ctx.fill();
      if (S.route.person === n.user || S.route.as === n.user || i === this.hover) {
        ctx.strokeStyle = I.text; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, ra + 3.5, 0, Math.PI * 2); ctx.stroke();
      }
      if (ra >= 8) {
        const u = name(n.user), inside = u.length <= 3 ? u : initials(u);
        ctx.font = `650 ${Math.min(12, ra * 0.68)}px ${font}`; ctx.fillStyle = '#fff';
        ctx.fillText(inside, n.sx, n.sy + 0.5);
      }
    }
    // names outside the ring, read from the centre out
    if (this.outside) {
      const radial = this.agents > 12, cx = -this.view.x * k + W / 2, cy = -this.view.y * k + H / 2;
      const R = RING * k + ra + (this.arcs.length > 1 ? 14 : 9);
      ctx.font = `600 ${radial ? Math.max(10, Math.min(12.5, ra * 0.85)) : 12.5}px ${font}`;
      ctx.textBaseline = 'middle';
      for (let i = 0; i < this.nodes.length; i++) {
        const n = this.nodes[i];
        if (n.kind !== 'agent') continue;
        const on = lit.has(i) || S.route.as === n.user || S.route.person === n.user, c = Math.cos(n.ang), s = Math.sin(n.ang);
        ctx.globalAlpha = (rel && !rel.agents.has(n.user)) || (focus != null && !lit.has(i)) ? 0.3 : 1;
        ctx.fillStyle = on ? I.text : I.muted;
        if (radial) {  // along the radius, turned so it never reads upside down
          ctx.save();
          ctx.translate(cx + c * R, cy + s * R);
          ctx.rotate(c < 0 ? n.ang + Math.PI : n.ang);
          ctx.textAlign = c < 0 ? 'right' : 'left';
          ctx.fillText(name(n.user), 0, 0);
          ctx.restore();
        } else {
          ctx.textAlign = Math.abs(c) < 0.25 ? 'center' : c > 0 ? 'left' : 'right';
          ctx.fillText(name(n.user), cx + c * R, cy + s * R + (Math.abs(c) < 0.25 ? s * 8 : 0));
        }
      }
    }
    ctx.globalAlpha = 1;
  }
  // The web in the same three coats as the ring: links, then chats, agents and names, painted when the view, the
  // layout or the counts change, and over them each frame the glows and the messages on their way.
  drawWeb(W, H) {
    const v = this.view, lens = MAP.lens && this.mouse && !this.dragging ? this.mouse : null;
    const key = ['web', W, H, v.x, v.y, v.k, lens && lens.x, lens && lens.y, this.hover, this.selected, this.selChat, this.version,
      this.moved, S.route.as].join();
    if (key !== this.layersKey) { this.paintWeb(W, H); this.layersKey = key; }
    const ctx = this.layer(this.fly, W, H), scale = Math.max(0.55, Math.min(2.2, Math.sqrt(v.k))), rel = this.related();
    const focus = this.hover ?? this.selected, near = focus == null ? null : new Set([focus, ...(this.nbrs.get(focus) || [])]);
    for (let i = 0; i < this.nodes.length; i++) {  // an agent glows just after it writes
      const n = this.nodes[i];
      if (n.kind !== 'agent') continue;
      const g = this.glow(n.user);
      if (g <= 0) continue;
      const dim = (rel && !rel.agents.has(n.user)) || (replay.on && !n.w) || (near && !near.has(i));
      ctx.fillStyle = n.color; ctx.globalAlpha = (dim ? 0.05 : 0.22) * g;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, this.radius(n, scale) * 2.2, 0, Math.PI * 2); ctx.fill();
    }
    const flying = this.flights(), halo = 0.25 * Math.max(0, Math.min(1, (600 - flying.length) / 400));
    for (const [i, j, p, color] of flying) {  // messages on their way, each with a faint halo while there are few of them
      const a = this.nodes[i], b = this.nodes[j], e = p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2;
      const x = a.sx + (b.sx - a.sx) * e, y = a.sy + (b.sy - a.sy) * e;
      ctx.fillStyle = color;
      ctx.globalAlpha = Math.min(1, (1 - p) * 3);
      ctx.beginPath(); ctx.arc(x, y, 2.6, 0, Math.PI * 2); ctx.fill();
      if (!halo) continue;
      ctx.globalAlpha *= halo;
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  paintWeb(W, H) {
    const k = this.view.k;
    let ctx = this.layer(this.base, W, H);
    const rel = this.related(), focus = this.hover ?? this.selected;
    const lit = new Set();
    if (focus != null) { lit.add(focus); for (const j of this.nbrs.get(focus) || []) lit.add(j); }
    // links, in a few strengths so each is one stroke
    const buckets = new Map();
    for (const e of this.edges) {
      if (replay.on && !S.chats.get(e.chat)?.born) continue;
      const a = this.nodes[e.a], b = this.nodes[e.b];
      const on = focus != null && (e.a === focus || e.b === focus) || e.chat === this.selChat;
      const dim = rel ? !rel.chats.has(e.chat) : false;
      const t = e.w ? Math.log1p(e.w) / Math.log1p(this.maxE) : 0;
      const level = on ? 4 : dim ? 0 : t === 0 && replay.on ? 0 : 1 + Math.min(2, Math.floor(t * 3));
      const key = (e.kind === 'direct' ? 'd' : 's') + level;
      (buckets.get(key) || buckets.set(key, []).get(key)).push(a.sx, a.sy, b.sx, b.sy);
    }
    const alpha = [0.035, 0.09, 0.2, 0.42, 0.85];
    ctx.lineCap = 'round';
    for (const [key, pts] of buckets) {
      // The faint links, nearly all of them, are one screen pixel wide. The browser draws a line that thin in a fast
      // way of its own: thousands of 1px lines on a 2x screen took it hundreds of milliseconds each time the counts
      // changed, and those at one pixel a few. Thin lines that cross add up where wider ones in one path did not, so
      // their alpha is left as it was, and the busy middle looks much as before.
      const level = +key[1], thin = level < 3;
      ctx.strokeStyle = key[0] === 'd' ? `rgba(${ink().edgeDm}, ${alpha[level]})` : `rgba(${ink().edge}, ${alpha[level] * 0.8})`;
      ctx.lineWidth = thin ? 1 / this.dpr : 1.6;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i += 4) { ctx.moveTo(pts[i], pts[i + 1]); ctx.lineTo(pts[i + 2], pts[i + 3]); }
      ctx.stroke();
    }
    // chats, then agents
    ctx = this.layer(this.canvas, W, H);
    const scale = Math.max(0.55, Math.min(2.2, Math.sqrt(k)));
    for (const pass of ['hub', 'agent']) {
      for (let i = 0; i < this.nodes.length; i++) {
        const n = this.nodes[i];
        if (n.kind !== pass) continue;
        if (pass === 'hub' && replay.on && !S.chats.get(n.chat)?.born) continue;
        const dim = (rel && (pass === 'hub' ? !rel.chats.has(n.chat) : !rel.agents.has(n.user))) || (replay.on && !n.w) || (focus != null && !lit.has(i));
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
      if (n.kind === 'hub' && replay.on && !S.chats.get(n.chat)?.born) continue;
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
  }
  radius(n, scale) {  // the lens enlarges shapes less than it spreads them, so they never cover their neighbours
    if (MAP.mode === 'ring') return (n.kind === 'hub' ? n.hr : this.rr) * this.view.k;
    return Math.min(n.kind === 'hub' ? 34 : 26, n.r * scale * (1 + (n.mag - 1) * 0.45));
  }
  kick(ms = 0) {
    this.until = Math.max(this.until || 0, performance.now() + ms);
    if (this.raf || this.dead) return;
    const loop = () => {
      this.raf = 0;
      if (this.dead) return;
      this.draw();
      const now = performance.now();
      if (this.pulses.length || (replay.on && replay.playing) || now < this.until || (!replay.on && [...this.lit.values()].some(t => now - t < 2000 * FLIGHT))) this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  // ---- the mouse and fingers
  listen() {
    const c = this.canvas, fingers = new Map();
    const at = e => { const r = c.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; };
    let down = null, pinch = null;
    c.addEventListener('pointerdown', e => {
      c.setPointerCapture(e.pointerId);
      this.slop = e.pointerType === 'mouse' ? 0 : 12;  // a fingertip is less exact than a mouse
      if (e.pointerType !== 'mouse') fingers.set(e.pointerId, at(e));
      if (fingers.size === 2) {  // two fingers zoom about the point between them, and move the map as they move
        const [p, q] = [...fingers.values()], v = this.view, mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
        pinch = {d: Math.max(10, Math.hypot(p.x - q.x, p.y - q.y)), k: v.k, wx: v.x + (mx - c.w / 2) / v.k, wy: v.y + (my - c.h / 2) / v.k};
        down = null; this.dragging = true; this.mouse = null; this.card.hidden = true;
        return;
      }
      down = {x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y}; this.dragging = false;
      this.mouse = at(e);
    });
    c.addEventListener('pointermove', e => {
      if (fingers.has(e.pointerId)) fingers.set(e.pointerId, at(e));
      if (pinch) {
        if (fingers.size < 2) return;
        const [p, q] = [...fingers.values()], v = this.view, mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
        v.k = Math.max(0.03, Math.min(12, pinch.k * Math.hypot(p.x - q.x, p.y - q.y) / pinch.d));
        v.x = pinch.wx - (mx - c.w / 2) / v.k; v.y = pinch.wy - (my - c.h / 2) / v.k;
        return this.kick();
      }
      this.mouse = at(e);
      if (down) {
        const dx = e.clientX - down.x, dy = e.clientY - down.y;
        if (Math.abs(dx) + Math.abs(dy) > (e.pointerType === 'mouse' ? 3 : 8)) { this.dragging = true; c.classList.add('drag'); this.card.hidden = true; }
        if (this.dragging) { this.view.x = down.vx - dx / this.view.k; this.view.y = down.vy - dy / this.view.k; }
      } else if (e.pointerType === 'mouse') {
        const hit = this.hit(this.mouse.x, this.mouse.y), edge = hit == null ? this.hitEdge(this.mouse.x, this.mouse.y) : null;
        if (hit !== this.hover || edge !== this.hoverEdge) { this.hover = hit; this.hoverEdge = edge; c.classList.toggle('point', hit != null || !!edge); }
        this.showCard();
      }
      this.kick();
    });
    const up = (e, cancel) => {
      fingers.delete(e.pointerId);
      if (pinch) {  // the last finger up ends a pinch, and taps nothing
        if (!fingers.size) { pinch = null; this.dragging = false; }
        return this.kick();
      }
      const clicked = down && !this.dragging && !cancel;
      down = null; this.dragging = false; c.classList.remove('drag');
      if (clicked && this.mouse) this.click(this.hit(this.mouse.x, this.mouse.y), this.hitEdge(this.mouse.x, this.mouse.y));
      if (e.pointerType !== 'mouse') { this.mouse = null; this.hover = null; this.hoverEdge = null; }  // nothing stays under a lifted finger
      this.kick();
    };
    c.addEventListener('pointerup', e => up(e, false));
    c.addEventListener('pointercancel', e => up(e, true));
    c.addEventListener('pointerleave', e => {
      if (e.pointerType !== 'mouse' || down) return;
      this.mouse = null; this.hover = null; this.hoverEdge = null; this.card.hidden = true; this.kick();
    });
    c.addEventListener('wheel', e => {
      e.preventDefault();
      const r = c.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top, v = this.view;
      const k = Math.max(0.03, Math.min(12, v.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))));
      v.x += (mx - c.w / 2) * (1 / v.k - 1 / k); v.y += (my - c.h / 2) * (1 / v.k - 1 / k); v.k = k;
      this.card.hidden = true;
      this.kick();
    }, {passive: false});
    $('.mapwrap', this.main).addEventListener('click', e => {  // on the map's own element: #main outlives this view
      if (e.target.closest('[data-lens]')) { MAP.lens = !MAP.lens; this.controls(); this.kick(); }
      if (e.target.closest('[data-fit]')) this.fit();
      const mode = e.target.closest('[data-mode]');
      if (mode && mode.dataset.mode !== MAP.mode) { MAP.mode = mode.dataset.mode; this.build(true); this.kick(); }
    });
  }
  hit(x, y) {
    let best = null, bd = Infinity;
    const scale = Math.max(0.55, Math.min(2.2, Math.sqrt(this.view.k)));
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      if (n.kind === 'hub' && replay.on && !S.chats.get(n.chat)?.born) continue;
      const d = Math.hypot(n.sx - x, n.sy - y), r = this.radius(n, scale) + (MAP.mode === 'ring' ? 3 : 5) + (this.slop || 0);
      if (d < r && d < bd) { bd = d; best = i; }
    }
    return best;
  }
  hitEdge(x, y) {  // a direct chat's chord under the mouse
    let best = null, bd = 5 + (this.slop || 0);
    for (const e of this.direct.values()) {
      if (!e.w && MAP.mode === 'ring') continue;
      const a = this.nodes[e.a], b = this.nodes[e.b], dx = b.sx - a.sx, dy = b.sy - a.sy, l = dx * dx + dy * dy;
      const t = l ? Math.max(0, Math.min(1, ((x - a.sx) * dx + (y - a.sy) * dy) / l)) : 0;
      const d = Math.hypot(a.sx + t * dx - x, a.sy + t * dy - y);
      if (d < bd) { bd = d; best = e; }
    }
    return best;
  }
  click(i, edge) {
    if (i == null && !edge) return go({chat: null, person: null});
    if (i == null) return go({chat: edge.chat, person: null});
    const n = this.nodes[i];
    if (n.kind === 'agent') go({person: n.user === S.route.person ? null : n.user, chat: null});
    else go({chat: n.chat === S.route.chat ? null : n.chat, person: null});
  }
  showCard() {
    const n = this.hover != null ? this.nodes[this.hover] : null, e = this.hoverEdge, m = this.mouse;
    if ((!n && !e) || !m) { this.card.hidden = true; return; }
    const so = replay.on ? ' so far' : '';
    if (n && n.kind === 'agent') {
      const u = user(n.user), chats = (this.nbrs.get(this.hover) || []).length + [...this.direct.values()].filter(d => d.a === this.hover || d.b === this.hover).length;
      this.card.innerHTML = html`<div class="ch">${avatar(n.user, 'sm')}<b>${u.name}</b>${u.name !== u.id ? html`<span>${u.id}</span>` : ''}</div>
${Object.keys(u.fields || {}).length ? html`<div class="cf">${Object.entries(u.fields).map(([k, v]) => html`<span class="field"><i>${k}</i> ${v}</span> `)}</div>` : ''}
<div class="cs">${plural(n.w, 'message')}${so} · in ${plural(chats, 'chat')}</div>`.s;
    } else {
      const c = S.chats.get(n ? n.chat : e.chat);
      this.card.innerHTML = html`<div class="ch"><span class="ico ${c.kind}">${KIND[c.kind]}</span><b>${title(c, null)}</b></div>
<div class="cs">${c.kind === 'direct' ? 'direct chat' : plural(c.members.length, 'member')} · ${plural(c.messages, 'message')}${so}</div>${c.description ? html`<div class="cf">${c.description}</div>` : ''}`.s;
    }
    this.card.hidden = false;
    const w = this.card.offsetWidth, h = this.card.offsetHeight;
    this.card.style.left = Math.min(this.canvas.w - w - 8, m.x + 16) + 'px';
    this.card.style.top = Math.min(this.canvas.h - h - 8, m.y + 16) + 'px';
  }
  onKey(e) {
    if ((e.key === 'f' || e.key === 'F') && MAP.mode === 'web') { MAP.lens = !MAP.lens; this.controls(); this.kick(); }
    if (e.key === '0') this.fit();
  }
  onTheme() { this.colors(); this.kick(); }

  // ---- the pane: the chat picked on the map or in the list, an agent's messages, or every message as it comes
  onRoute() {
    const r = S.route, key = r.chat ? 'c:' + r.chat : r.person ? 'u:' + r.person : '';
    this.selected = key ? this.index.get(key) ?? null : null;
    this.selChat = r.chat && S.chats.get(r.chat)?.kind === 'direct' ? r.chat : null;
    if (key + PHONE.matches !== this.sideKey) {
      this.sideKey = key + PHONE.matches;
      if (this.side && this.side.destroy) this.side.destroy();
      const pane = $('#pane');
      pane.classList.add('open', 'mappane');
      pane.replaceChildren();
      // On a phone the pane is a sheet under the map, for what is picked: every message as it comes has no room there.
      this.side = r.chat && S.chats.has(r.chat) ? new ChatView(pane, r.chat, {inPane: true})
        : r.person ? new AgentPane(pane, r.person) : PHONE.matches ? null : new ActivityView(pane, {inPane: true});
      if (key) pane.insertAdjacentHTML('afterbegin', '<button class="sheetx" data-unpick title="Close" aria-label="Close">×</button>');
    }
    this.kick();
  }
  onAs() { this.version++; this.kick(); if (this.side && this.side.onAs) this.side.onAs(); }

  // ---- live, and in a replay
  onEvents(ev) {
    let grew = false;
    const now = performance.now();
    for (const m of ev.lines) {
      if (m.kind !== 'message') { grew = true; continue; }
      if (!S.chats.has(m.chat) || this.index.get('u:' + m.from) == null) { grew = true; continue; }
      if (S.chats.get(m.chat).kind !== 'direct' && !this.index.has('c:' + m.chat)) { grew = true; continue; }
      this.pulses.push({chat: m.chat, from: m.from, t0: now});
      this.lit.set(m.from, now);
    }
    if (this.pulses.length > 1500) this.pulses = this.pulses.slice(-1500);
    if (this.side && this.side.onEvents) this.side.onEvents(ev);
    if (grew) this.regrow();
    this.reweigh();
    this.kick();
  }
  onReplay(list) {
    if (this.side && this.side.onReplay) this.side.onReplay(list);
    this.reweigh();
    this.kick();
  }
  onSeek() {
    this.weigh();
    if (this.side && this.side.onSeek) this.side.onSeek();
    else { this.sideKey = null; this.onRoute(); }
    this.kick();
  }
  regrow = throttle(() => { if (!this.dead) this.build(false); }, 3000);
}

// An agent picked on the map: who it is, and its latest messages.
class AgentPane {
  constructor(main, id) {
    this.main = main; this.id = id;
    const u = user(id);
    const chats = [...S.chats.values()].filter(c => c.members.includes(id) && (!replay.on || c.born)).length;
    main.innerHTML = html`<div class="mhead">${avatar(id)}<div class="who"><h1>${u.name}</h1><span class="sub">${[u.name !== id ? id : '', ...Object.values(u.fields || {})].filter(Boolean).join(' · ')}</span></div><div class="grow"></div><button class="btn" data-see-as="${id}" title="See everything as ${u.name} sees it">See as ${first(id)}</button></div>
<div class="scroll"><div class="note"><b>${num(S.sent.get(id) || 0)}</b> messages${replay.on ? ' so far' : ''} · in ${plural(chats, 'chat')}${u.status ? html` · <i>${u.status}</i>` : ''}</div><div class="msgs results"><div class="loading">Loading</div></div></div>`.s;
    this.scroll = $('.scroll', main);
    this.load();
  }
  async load() {  // one at a time, as in the activity list
    if (this.loading) { this.again = true; return; }
    this.loading = true; this.again = false;
    const gen = replay.gen;
    let r;
    try { r = await api('view_search', {from: [this.id], limit: 50, before: replay.on ? isoAfter(replay.t) : undefined}); }
    finally { this.loading = false; }
    if (this.again) this.loadSoon();
    if (this.dead || gen !== replay.gen) return;
    let prev = null, out = '';
    for (const m of r.messages.reverse()) { out += messageHtml(m, prev, {showChat: true}).s; prev = m; }
    $('.msgs', this.main).innerHTML = out || html`<div class="note">No messages${replay.on ? ' yet at this moment' : ' yet'}.</div>`.s;
    $('.note b', this.main).textContent = num(S.sent.get(this.id) || 0);
    this.scroll.scrollTop = this.scroll.scrollHeight;
  }
  onEvents(ev) { if (ev.lines.some(m => m.from === this.id)) this.loadSoon(); }
  onReplay(list) { if (list.some(m => m[3] === this.id)) this.loadSoon(); }
  loadSoon = throttle(() => { if (!this.dead) this.load(); }, 500);
  onSeek() { this.load(); }
  destroy() { this.dead = true; }
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
  $('meta[name="theme-color"]').content = getComputedStyle($('#top')).backgroundColor;  // a phone's browser bar matches
  if (S.view && S.view.onTheme) S.view.onTheme();
  replay.draw(true);
}
systemDark.addEventListener('change', () => { if (theme() === 'system') showTheme(); });

// The theme's colors as a canvas needs them, resolved to rgb(). Some are kept as bare "r, g, b" to add an alpha to.
function ink() {
  if (INK) return INK;
  const probe = document.body.appendChild(document.createElement('i'));
  const read = name => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
  const bare = name => read(name).match(/[\d.]+/g).slice(0, 3).join(', ');
  const t = theme();
  INK = {dark: t === 'dark' || (t === 'system' && systemDark.matches), text: read('--text'), muted: read('--muted'), faint: read('--faint'),
    line2: read('--line2'), panel: read('--panel'), accent: read('--accent'), accentSoft: read('--accent-soft'), channel: read('--channel'),
    group: read('--group'), chatName: read('--channel-soft'), played: read('--played'), unplayed: read('--unplayed'),
    bg: bare('--bg'), edge: bare('--edge'), edgeDm: bare('--edge-dm'), spoke: bare('--spoke')};
  probe.remove();
  return INK;
}

// ---------------------------------------------------------------------- widths

// The sidebar and the pane beside the chat are as wide as their handles are dragged, and stay so in this browser. A
// double click on a handle puts its default back, and the arrow keys move a handle that has the focus. The chat in
// between keeps at least 400px.
const WIDTHS_KEY = 'swarmchat.view.widths';
const WIDE = {side: [180, 480], pane: [300, 760]};
function grips() {
  const app = $('#app'), saved = JSON.parse(localStorage.getItem(WIDTHS_KEY) || '{}');
  const width = which => $(which === 'side' ? '#side' : '#pane').getBoundingClientRect().width;
  const set = (which, w) => {
    const other = which === 'side' ? width('pane') : width('side'), [lo, hi] = WIDE[which];
    w = Math.round(Math.max(lo, Math.min(hi, innerWidth - other - 400, w)));
    app.style.setProperty(`--${which}-w`, w + 'px');
    return w;
  };
  const keep = (which, w) => {
    if (w == null) delete saved[which]; else saved[which] = w;
    localStorage.setItem(WIDTHS_KEY, JSON.stringify(saved));
  };
  for (const which in WIDE) if (saved[which]) app.style.setProperty(`--${which}-w`, saved[which] + 'px');
  app.addEventListener('pointerdown', e => {
    const g = e.target.closest('.grip');
    if (!g || e.button !== 0) return;
    e.preventDefault();
    const which = g.dataset.grip, x0 = e.clientX, w0 = width(which), sign = which === 'side' ? 1 : -1;
    let w = w0;
    g.setPointerCapture(e.pointerId);
    document.body.classList.add('resizing'); g.classList.add('on');
    g.onpointermove = m => { w = set(which, w0 + sign * (m.clientX - x0)); };
    g.onpointerup = g.onpointercancel = () => {
      g.onpointermove = g.onpointerup = g.onpointercancel = null;
      document.body.classList.remove('resizing'); g.classList.remove('on');
      if (w !== w0) keep(which, w);
    };
  });
  app.addEventListener('dblclick', e => {
    const g = e.target.closest('.grip');
    if (g) { app.style.removeProperty(`--${g.dataset.grip}-w`); keep(g.dataset.grip, null); }
  });
  app.addEventListener('keydown', e => {
    const g = e.target.closest('.grip');
    if (!g || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    e.preventDefault(); e.stopPropagation();  // not a step in a replay
    const which = g.dataset.grip, sign = which === 'side' ? 1 : -1;
    keep(which, set(which, width(which) + sign * (e.key === 'ArrowRight' ? 16 : -16)));
  });
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
  replay.init();
  tips();
  // On the app and the overlay rather than the document: Safari on a phone turns a tap into a click only where something
  // below the document listens for clicks.
  for (const el of [$('#app'), $('#overlay')]) el.addEventListener('click', onClick);
  new MutationObserver(watchPictures).observe(document.body, {childList: true, subtree: true});
  document.addEventListener('keydown', onKey);
  addEventListener('hashchange', route);
  PHONE.addEventListener('change', () => route());
  COARSE.addEventListener('change', () => side.build());
  await route();
  follow();
}

(async function boot() {
  showTheme();
  grips();  // before anything is drawn, so a saved width shows at once
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
