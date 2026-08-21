// ── PWA service worker registration (#12) ───────────────────────────────────
// Minimal, no-op-fetch service worker — registered purely so browsers satisfy
// their install-prompt criteria (Chrome/Edge require an active SW with a fetch
// handler). Not used for offline caching, see service-worker.js's own comment.
// Registered from /service-worker.js (a dedicated root-scoped route in
// app/main.py), not /static/service-worker.js — a script served under /static/
// can only ever get a default scope of /static/*, which doesn't cover the
// app's actual pages and fails the installability check.
//
// Runs on every page base.html renders, including the pre-auth /auth/login
// page — that's intentional, not an oversight. auth_login.html only overrides
// the nav_extra block, not <head>, so the manifest link and this script load
// there too. The install prompt (and the SW itself, a no-op passthrough on an
// already-unauthenticated route) should be available before a first-time user
// even logs in, not gated behind auth.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch(err => {
      console.warn('Service worker registration failed:', err);
    });
  });
}

// ── i18n lookup (#147) ───────────────────────────────────────────────────────
// window.I18N is set inline in base.html's <head>, before this deferred script
// runs — the full key->string map for the request's locale, English-fallback
// already applied server-side (see app/i18n.py's all_strings()), so this never
// needs its own fallback chain. Mirrors app/i18n.py's translator()'s {kwarg}
// substitution, just with .split/.join instead of Python's str.format.
function t(key, vars) {
  let s = (window.I18N && window.I18N[key]) || key;
  if (vars) {
    for (const k in vars) s = s.split('{' + k + '}').join(vars[k]);
  }
  return s;
}

// ── Shared date formatting for <time datetime="..."> elements ──────────────────
// Used by both settings.html (sync-ts) and admin.html (next-daily-ts, next-rec-ts,
// moved there by #96) — kept here since both pages need the exact same formatting.
function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function formatTimeElements(ids) {
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = fmtDate(el.getAttribute('datetime'));
  });
}

// ── Settings/Admin page tabs (#95, #96) ─────────────────────────────────────────
// Shared client-side tab switching for any page using .settings-tabs/.settings-tab-panel
// (Settings, Admin) — no reload, no per-tab route. Reflects the active tab in the URL
// (?tab=) via replaceState so a reload or bookmark preserves it. A page can pass a
// resolveInitialTab(params) callback to map its own redirect params (e.g. settings.html's
// saved=credentials, admin.html's saved=schedule) to the tab that shows that message;
// an explicit ?tab= always wins over that.
//
// This file loads via <script defer>, so it executes after the document is parsed —
// after any inline <script> in the page body. A caller must invoke this from a
// DOMContentLoaded listener, not directly at the bottom of an inline block, or
// initSettingsTabs won't be defined yet when that inline code runs.
function initSettingsTabs(resolveInitialTab) {
  const tabs = Array.from(document.querySelectorAll('.settings-tabs .tab'));
  const panels = Array.from(document.querySelectorAll('.settings-tab-panel'));
  if (!tabs.length) return;
  const validTabs = tabs.map(t => t.dataset.tab);

  function activate(tab, pushState) {
    if (!validTabs.includes(tab)) tab = validTabs[0];
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    panels.forEach(p => { p.hidden = p.dataset.tabPanel !== tab; });
    if (pushState) {
      const url = new URL(window.location);
      url.searchParams.set('tab', tab);
      history.replaceState(null, '', url);
    }
  }

  tabs.forEach(t => t.addEventListener('click', () => activate(t.dataset.tab, true)));

  const params = new URLSearchParams(window.location.search);
  const initial = params.get('tab') || (resolveInitialTab && resolveInitialTab(params)) || validTabs[0];
  activate(initial, false);
}

// ── Help disclosures (#141) ──────────────────────────────────────────────────
// The small "i" button next to a settings-section-title/form-label — click/tap
// toggles the .help-disclosure-panel that follows it. One shared handler for both
// settings.html and admin.html; new instances need no JS of their own, just the
// button + panel markup with matching id/aria-controls.
document.querySelectorAll('.help-disclosure-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const panel = document.getElementById(btn.getAttribute('aria-controls'));
    if (!panel) return;
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!open));
    panel.classList.toggle('open', !open);
  });
});

// ── Library search ────────────────────────────────────────────────────────────
const librarySearch = document.getElementById('library-search');
if (librarySearch) {
  librarySearch.addEventListener('input', applyLibraryFilters);
}

// ── Library format filter ─────────────────────────────────────────────────────
// Scoped to `.filter-btn[data-format]` (issue #200 fix), not the bare `.filter-btn`
// class — score-filter-group, rewatch-filter-group, and #bulk-toggle all share that
// same class for styling, so the old unscoped selector meant clicking a score or
// rewatch button (or even Select) stripped the format button's "active" highlight
// on every click, and vice versa. Filtering itself still worked either way (each
// group tracks its own activeX variable), but the visual state was misleadingly
// wrong — most visible now that Collections replays several of these clicks in a
// row to reapply a saved filter combination.
let activeFormat = '';
document.querySelectorAll('.filter-btn[data-format]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn[data-format]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeFormat = btn.dataset.format;
    applyLibraryFilters();
  });
});

// ── Season filter ─────────────────────────────────────────────────────────────
let activeSeason = '';
(function buildSeasonFilter() {
  const sel = document.getElementById('season-filter');
  if (!sel) return;
  const grid = document.getElementById('library-grid');
  if (!grid) return;

  // Collect all unique season+year combos from the cards
  const SEASON_ORDER = { WINTER: 1, SPRING: 2, SUMMER: 3, FALL: 4 };
  const seen = new Set();
  grid.querySelectorAll('.card').forEach(card => {
    const s = card.dataset.season;
    const y = card.dataset.seasonYear;
    if (s && y) seen.add(`${y}|${s}`);
  });

  // Sort: most recent year first, then season desc within year
  const sorted = [...seen].sort((a, b) => {
    const [ay, as_] = a.split('|');
    const [by, bs] = b.split('|');
    if (by !== ay) return parseInt(by) - parseInt(ay);
    return (SEASON_ORDER[bs] || 0) - (SEASON_ORDER[as_] || 0);
  });

  sorted.forEach(key => {
    const [year, season] = key.split('|');
    const label = t('js_season_label', {season: t('season_' + season.toLowerCase()), year});
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = label;
    sel.appendChild(opt);
  });

  sel.addEventListener('change', () => {
    activeSeason = sel.value;
    applyLibraryFilters();
  });
}());

// ── Tag filter ─────────────────────────────────────────────────────────────────
let activeTag = '';

// Rebuilds the tag dropdown from tags actually in use on the current cards — no
// manual tag registry to maintain. Dedupes case-insensitively (matches personal_tags
// dedup in /api/anime/bulk-tags), keeping the first-seen casing for display. Re-run
// after any in-place tag edit (see notes-modal save handler below) so a newly-added
// or newly-orphaned tag stays in sync without a full page reload.
function refreshTagFilterOptions() {
  const sel = document.getElementById('tag-filter');
  const grid = document.getElementById('library-grid');
  if (!sel || !grid) return;

  const seen = new Map();
  grid.querySelectorAll('.card').forEach(card => {
    (card.dataset.tags || '').split(',').forEach(t => {
      const tag = t.trim();
      if (tag && !seen.has(tag.toLowerCase())) seen.set(tag.toLowerCase(), tag);
    });
  });

  const current = sel.value;
  sel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
  [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0])).forEach(([key, label]) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = label;
    sel.appendChild(opt);
  });

  sel.value = seen.has(current) ? current : '';
  activeTag = sel.value;
}

(function () {
  const sel = document.getElementById('tag-filter');
  if (!sel) return;
  refreshTagFilterOptions();
  sel.addEventListener('change', () => {
    activeTag = sel.value;
    applyLibraryFilters();
  });
}());

// ── Collections (#200) ───────────────────────────────────────────────────────────
// Named, saved filter combinations over the format/season/tag/score/rewatch/sort
// controls above, plus the server-rendered status tab and the free-text search box.
// This never re-implements filtering itself — saving reads the *current* UI state,
// and applying replays it through the exact same click/change handlers a user
// would trigger by hand, so it can never drift from what those controls actually do.
(function () {
  const sel = document.getElementById('collections-select');
  if (!sel) return; // library page only

  const saveBtn = document.getElementById('collections-save-btn');
  const manageBtn = document.getElementById('collections-manage-btn');
  const saveModal = document.getElementById('collection-save-modal');
  const saveNameInput = document.getElementById('collection-save-name');
  const saveConfirmBtn = document.getElementById('collection-save-confirm');
  const saveCancelBtn = document.getElementById('collection-save-cancel');
  const manageModal = document.getElementById('collections-manage-modal');
  const manageList = document.getElementById('collections-manage-list');
  const manageEmpty = document.getElementById('collections-manage-empty');
  const manageCloseBtn = document.getElementById('collections-manage-close');

  let collections = Array.isArray(window.COLLECTIONS) ? window.COLLECTIONS.slice() : [];

  function currentActiveStatus() {
    return (new URLSearchParams(location.search).get('status') || 'WATCHING').toUpperCase();
  }

  function currentFilterState() {
    return {
      status: currentActiveStatus(),
      format: document.querySelector('.filter-btn[data-format].active')?.dataset.format || '',
      season: document.getElementById('season-filter')?.value || '',
      tag: document.getElementById('tag-filter')?.value || '',
      score: document.querySelector('.filter-btn[data-score-filter].active')?.dataset.scoreFilter || '',
      rewatch: document.querySelector('.filter-btn[data-rewatch-filter].active')?.dataset.rewatchFilter || '',
      sort: document.querySelector('.sort-btn.active')?.dataset.sort || 'score',
      q: librarySearch?.value || '',
    };
  }

  function clickFormat(value) {
    document.querySelector(`.filter-btn[data-format="${CSS.escape(value || '')}"]`)?.click();
  }
  function clickAttr(attr, value) {
    document.querySelector(`[${attr}="${CSS.escape(value || '')}"]`)?.click();
  }

  // Replays a saved filters object through the real controls — never touches
  // applyLibraryFilters/sortLibrary/applyScoreFilter/applyRewatchFilter directly,
  // each control's own handler does, exactly as if a user had clicked it.
  function applyFilterValues(filters) {
    if (librarySearch) {
      librarySearch.value = filters.q || '';
      librarySearch.dispatchEvent(new Event('input'));
    }
    clickFormat(filters.format);
    clickAttr('data-score-filter', filters.score);
    clickAttr('data-rewatch-filter', filters.rewatch);
    clickAttr('data-sort', filters.sort || 'score');

    const seasonSel = document.getElementById('season-filter');
    if (seasonSel) {
      seasonSel.value = filters.season || '';
      seasonSel.dispatchEvent(new Event('change'));
    }
    const tagSel = document.getElementById('tag-filter');
    if (tagSel) {
      tagSel.value = filters.tag || '';
      tagSel.dispatchEvent(new Event('change'));
    }
  }

  function renderSelectOptions() {
    const current = sel.value;
    sel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
    collections.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(c => {
      const opt = document.createElement('option');
      opt.value = String(c.id);
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
    sel.value = collections.some(c => String(c.id) === current) ? current : '';
  }
  renderSelectOptions();

  function applyCollection(c) {
    const filters = c.filters || {};
    const targetStatus = (filters.status || 'WATCHING').toUpperCase();
    if (targetStatus !== currentActiveStatus()) {
      // The tag/season dropdowns are rebuilt from whatever cards render for the
      // target status, so the rest of the filters can only be safely replayed
      // *after* that navigation — carried across via query params and picked up
      // by the "apply from URL" block below, once this page's own status tab
      // (WATCHING/etc, server-side) has actually changed.
      const params = new URLSearchParams();
      params.set('status', targetStatus);
      params.set('collection', String(c.id));
      Object.entries(filters).forEach(([k, v]) => {
        if (k !== 'status' && v) params.set(k, v);
      });
      location.href = '/?' + params.toString();
      return;
    }
    applyFilterValues(filters);
  }

  sel.addEventListener('change', () => {
    const id = sel.value;
    sel.value = ''; // the dropdown is a picker, not a persistent "current collection" state
    if (!id) return;
    const c = collections.find(c => String(c.id) === id);
    if (c) applyCollection(c);
  });

  // ── Save current filters as a new collection ──────────────────────────────
  function openSaveModal() {
    saveModal.hidden = false;
    saveNameInput.value = '';
    setTimeout(() => saveNameInput.focus(), 50);
  }
  function closeSaveModal() { saveModal.hidden = true; }

  saveBtn?.addEventListener('click', openSaveModal);
  saveCancelBtn?.addEventListener('click', closeSaveModal);
  saveModal?.addEventListener('click', e => { if (e.target === saveModal) closeSaveModal(); });

  saveConfirmBtn?.addEventListener('click', async () => {
    const name = saveNameInput.value.trim();
    if (!name) { saveNameInput.focus(); return; }
    saveConfirmBtn.disabled = true;
    try {
      const resp = await fetch('/api/collections', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, filters: currentFilterState()}),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        alert(data.error || t('js_collection_save_failed'));
        return;
      }
      collections.push(data.collection);
      renderSelectOptions();
      closeSaveModal();
    } catch {
      alert(t('js_collection_save_failed'));
    } finally {
      saveConfirmBtn.disabled = false;
    }
  });

  // ── Manage (rename/delete) ─────────────────────────────────────────────────
  // Built via DOM APIs rather than innerHTML templating — a collection name is
  // free user text, and this way it's never at risk of being parsed as markup.
  function renderManageList() {
    manageList.innerHTML = '';
    manageEmpty.hidden = collections.length !== 0;
    collections.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(c => {
      const row = document.createElement('div');
      row.className = 'collections-manage-row';
      row.dataset.id = String(c.id);

      const input = document.createElement('input');
      input.className = 'notes-field collections-manage-name-input';
      input.type = 'text';
      input.maxLength = 100;
      input.value = c.name;

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'filter-btn collections-manage-rename-btn';
      renameBtn.textContent = t('lib_collections_rename_btn');

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'filter-btn collections-manage-delete-btn';
      deleteBtn.textContent = t('lib_collections_delete_btn');

      row.append(input, renameBtn, deleteBtn);
      manageList.appendChild(row);
    });
  }

  function openManageModal() {
    renderManageList();
    manageModal.hidden = false;
  }
  function closeManageModal() { manageModal.hidden = true; }

  manageBtn?.addEventListener('click', openManageModal);
  manageCloseBtn?.addEventListener('click', closeManageModal);
  manageModal?.addEventListener('click', e => { if (e.target === manageModal) closeManageModal(); });

  manageList?.addEventListener('click', async e => {
    const row = e.target.closest('.collections-manage-row');
    if (!row) return;
    const id = row.dataset.id;
    const input = row.querySelector('.collections-manage-name-input');

    if (e.target.classList.contains('collections-manage-rename-btn')) {
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      e.target.disabled = true;
      try {
        const resp = await fetch(`/api/collections/${id}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          alert(data.error || t('js_collection_rename_failed'));
          return;
        }
        const idx = collections.findIndex(c => String(c.id) === id);
        if (idx !== -1) collections[idx] = data.collection;
        renderSelectOptions();
        renderManageList();
      } catch {
        alert(t('js_collection_rename_failed'));
      } finally {
        e.target.disabled = false;
      }
      return;
    }

    if (e.target.classList.contains('collections-manage-delete-btn')) {
      const name = collections.find(c => String(c.id) === id)?.name || '';
      if (!confirm(t('js_collection_delete_confirm', {name}))) return;
      e.target.disabled = true;
      try {
        const resp = await fetch(`/api/collections/${id}`, {method: 'DELETE'});
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          alert(data.error || t('js_collection_delete_failed'));
          return;
        }
        collections = collections.filter(c => String(c.id) !== id);
        renderSelectOptions();
        renderManageList();
      } catch {
        alert(t('js_collection_delete_failed'));
        e.target.disabled = false;
      }
      return;
    }
  });

  // ── Apply filters carried in via the URL ────────────────────────────────────
  // Two callers land here: applyCollection above (?collection=ID, replaying a
  // saved combination) and, since issue #225, the /stats page's drill-down links
  // (e.g. a genre bar builds `/?status=COMPLETED&q=Isekai` directly, no saved
  // collection involved). Both just need these same format/season/tag/score/
  // rewatch/sort/q keys replayed through the real controls — same
  // applyFilterValues call either way, nothing #225-specific added here beyond
  // the trigger condition below no longer requiring `collection` to be present.
  // The season/tag dropdowns for the new status are already built by this point,
  // since those IIFEs run earlier in this same file.
  (function applyFromUrl() {
    const params = new URLSearchParams(location.search);
    const filterKeys = ['format', 'season', 'tag', 'score', 'rewatch', 'sort', 'q'];
    const hasFilterParams = filterKeys.some(k => params.has(k));
    if (!params.has('collection') && !hasFilterParams) return;
    const filters = {};
    filterKeys.forEach(k => {
      if (params.has(k)) filters[k] = params.get(k);
    });
    applyFilterValues(filters);

    const url = new URL(location.href);
    ['collection', ...filterKeys].forEach(k => url.searchParams.delete(k));
    history.replaceState(null, '', url);
  }());
}());

// ── Library sort ──────────────────────────────────────────────────────────────
let activeSort = 'score';
document.querySelectorAll('.sort-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeSort = btn.dataset.sort;
    sortLibrary();
  });
});

function applyLibraryFilters() {
  const q = (librarySearch?.value || '').toLowerCase();
  const grid = document.getElementById('library-grid');
  if (!grid) return;
  const cards = [...grid.querySelectorAll('.card')];
  let visible = 0;
  cards.forEach(card => {
    const title   = card.querySelector('.title')?.textContent.toLowerCase() || '';
    const sub     = card.querySelector('.title-sub')?.textContent.toLowerCase() || '';
    const tags    = [...card.querySelectorAll('.genre, .personal-tag')]
                      .map(t => t.textContent.toLowerCase()).join(' ');
    const format  = (card.dataset.format || '').toUpperCase();
    const textOk  = !q || title.includes(q) || sub.includes(q) || tags.includes(q);
    const fmtOk   = !activeFormat || format === activeFormat;
    const seasonKey = card.dataset.seasonYear && card.dataset.season
      ? `${card.dataset.seasonYear}|${card.dataset.season}` : '';
    const seasonOk  = !activeSeason || seasonKey === activeSeason;
    const scoreOk   = card.dataset.scoreHidden !== 'true';
    const rewatchOk = card.dataset.rewatchHidden !== 'true';
    const favoriteOk = card.dataset.favoriteHidden !== 'true';
    const cardTags  = (card.dataset.tags || '').toLowerCase().split(',').map(t => t.trim());
    const tagOk     = !activeTag || cardTags.includes(activeTag);
    const show    = textOk && fmtOk && seasonOk && scoreOk && rewatchOk && favoriteOk && tagOk;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const noResults = document.getElementById('no-filter-results');
  if (noResults) noResults.style.display = ((q || activeFormat || activeSeason || activeTag) && visible === 0) ? '' : 'none';
}

function sortLibrary() {
  const grid = document.getElementById('library-grid');
  if (!grid) return;
  const cards = [...grid.querySelectorAll('.card')];
  cards.sort((a, b) => {
    if (activeSort === 'score') {
      return parseFloat(b.dataset.score || 0) - parseFloat(a.dataset.score || 0);
    }
    if (activeSort === 'title') {
      return (a.dataset.title || '').localeCompare(b.dataset.title || '');
    }
    if (activeSort === 'progress') {
      return parseFloat(b.dataset.progress || 0) - parseFloat(a.dataset.progress || 0);
    }
    if (activeSort === 'updated') {
      return (b.dataset.updated || '').localeCompare(a.dataset.updated || '');
    }
    if (activeSort === 'rewatches') {
      return parseFloat(b.dataset.repeatCount || 0) - parseFloat(a.dataset.repeatCount || 0);
    }
    return 0;
  });
  cards.forEach(c => grid.appendChild(c));
}

// ── Star rating ───────────────────────────────────────────────────────────────
document.querySelectorAll('.star-rating').forEach(widget => {
  const stars = [...widget.querySelectorAll('.star')];
  const animeId = widget.dataset.animeId;
  let currentScore = parseInt(widget.dataset.score) || 0;

  function setFilled(upTo) {
    stars.forEach((s, i) => s.classList.toggle('filled', i < upTo));
  }

  stars.forEach((star, idx) => {
    const value = idx + 1;
    star.addEventListener('mouseenter', () => setFilled(value));
    star.addEventListener('mouseleave', () => setFilled(currentScore));
    star.addEventListener('click', async () => {
      const newScore = value === currentScore ? 0 : value;
      const prevScore = currentScore;
      currentScore = newScore;
      widget.dataset.score = newScore;
      setFilled(newScore);
      try {
        const resp = await fetch(`/api/anime/${animeId}/rating`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({score: newScore}),
        });
        if (!resp.ok) throw new Error('request failed');
        widget.closest('.card')?.setAttribute('data-score', newScore);
      } catch {
        currentScore = prevScore;
        widget.dataset.score = prevScore;
        setFilled(prevScore);
      }
    });
  });
});

// ── Favorite toggle (heart) ─────────────────────────────────────────────────────
// Letterboxd's heart-vs-star pattern (#219) — a nullable-boolean "liked" signal
// independent of the star-rating widget above. Shared handler for both the
// library card (.card) and notes.html's anime-detail page — the latter has no
// .card ancestor, so the card-specific dataset/filter updates below are guarded
// with a null check rather than assuming one exists.
document.querySelectorAll('.favorite-toggle').forEach(btn => {
  const animeId = btn.dataset.animeId;
  let current = btn.dataset.favorite === 'true';

  function render(fav) {
    btn.classList.toggle('active', fav);
    btn.setAttribute('aria-pressed', String(fav));
    btn.dataset.favorite = String(fav);
    const card = btn.closest('.card');
    if (card) card.dataset.favorite = String(fav);
  }

  btn.addEventListener('click', async () => {
    const newVal = !current;
    const prev = current;
    current = newVal;
    render(newVal);
    try {
      const resp = await fetch(`/api/anime/${animeId}/favorite`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({favorite: newVal}),
      });
      if (!resp.ok) throw new Error('request failed');
      if (typeof applyLibraryFilters === 'function') applyLibraryFilters();
    } catch {
      current = prev;
      render(prev);
    }
  });
});

// ── Progress stepper ──────────────────────────────────────────────────────────
document.querySelectorAll('.progress-stepper').forEach(stepper => {
  const animeId = stepper.dataset.animeId;
  const maxEp = stepper.dataset.episodes ? parseInt(stepper.dataset.episodes) : Infinity;
  const valEl = stepper.querySelector('.prog-val');
  const bar = stepper.closest('.card')?.querySelector('.progress-bar');
  let current = parseInt(valEl.textContent) || 0;
  let committed = current;
  let saveTimer = null;
  let lastDelta = 0;

  function updateBar(val) {
    if (bar && maxEp !== Infinity) {
      bar.style.width = Math.min((val / maxEp) * 100, 100) + '%';
    }
  }

  async function save() {
    const target = current;
    const wasIncrement = lastDelta > 0;
    try {
      const resp = await fetch(`/api/anime/${animeId}/progress`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({progress: target}),
      });
      if (!resp.ok) throw new Error('request failed');
      committed = target;
      stepper.closest('.card')?.setAttribute('data-progress', target);
      // Auto-suggest an episode note (issue #210) — only after a confirmed
      // increment (never a decrement), and only once the save has actually
      // succeeded, so this can never delay or interfere with the progress
      // update itself. suggestEpisodeNote is defined further down in this
      // file; the typeof guard is just defensive against future refactors.
      if (wasIncrement && typeof suggestEpisodeNote === 'function') {
        suggestEpisodeNote(animeId, target);
      }
    } catch {
      current = committed;
      valEl.textContent = current;
      updateBar(current);
    }
  }

  stepper.querySelectorAll('.prog-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const delta = parseInt(btn.dataset.delta);
      const next = Math.max(0, Math.min(current + delta, maxEp));
      if (next === current) return;
      lastDelta = delta;
      current = next;
      valEl.textContent = current;
      updateBar(current);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 600);
    });
  });
});

// ── Episode notes (#210) ────────────────────────────────────────────────────
// One shared modal (#ep-note-modal) reused for every card's note icon AND for
// the auto-suggest prompt fired from the progress-stepper block above on a
// plain increment — see the .ep-note-btn comment in style.css for why this is
// a single shared modal rather than a per-card popover (a popover positioned
// off the icon would live inside .card, which is `overflow: hidden`, and would
// get silently clipped past the card's edge on the narrow mobile grid).
(function () {
  const modal    = document.getElementById('ep-note-modal');
  if (!modal) return;

  const titleEl  = document.getElementById('ep-note-modal-title');
  const hintEl   = document.getElementById('ep-note-hint');
  const fieldEl  = document.getElementById('ep-note-field');
  const quoteEl  = document.getElementById('ep-note-quote-field');
  const saveBtn  = document.getElementById('ep-note-save');
  const delBtn   = document.getElementById('ep-note-delete');
  const cancelBtn = document.getElementById('ep-note-cancel');

  let activeAnimeId  = null;
  let activeEpisode  = null;
  let activeSuggested = false;

  function dismissKey(animeId, episode) {
    return `epNoteDismissed:${animeId}:${episode}`;
  }
  function isDismissed(animeId, episode) {
    try { return localStorage.getItem(dismissKey(animeId, episode)) === '1'; }
    catch { return false; }
  }
  function markDismissed(animeId, episode) {
    try { localStorage.setItem(dismissKey(animeId, episode), '1'); } catch { /* no-op */ }
  }

  function updateIcon(animeId, hasNote) {
    document.querySelectorAll(`.ep-note-btn[data-anime-id="${animeId}"]`).forEach(btn => {
      btn.classList.toggle('has-note', hasNote);
    });
  }

  async function openModal(animeId, episode, { suggested = false, cardTitle = '' } = {}) {
    activeAnimeId = animeId;
    activeEpisode = episode;
    activeSuggested = suggested;
    titleEl.textContent = t('lib_ep_note_modal_title', {ep: episode, title: cardTitle});
    hintEl.hidden = !suggested;
    if (suggested) hintEl.textContent = t('lib_ep_note_prompt_text', {ep: episode});
    fieldEl.value = '';
    quoteEl.value = '';
    fieldEl.disabled = true;
    quoteEl.disabled = true;
    modal.hidden = false;
    try {
      const resp = await fetch(`/api/anime/${animeId}/episode-notes/${episode}`);
      if (resp.ok) {
        const data = await resp.json();
        fieldEl.value = data.note || '';
        quoteEl.value = data.quote || '';
      }
    } catch { /* leave blank — still editable */ }
    fieldEl.disabled = false;
    quoteEl.disabled = false;
    if (!suggested) fieldEl.focus();
  }

  function closeModal({ dismissed = false } = {}) {
    if (dismissed && activeSuggested && activeAnimeId && activeEpisode) {
      markDismissed(activeAnimeId, activeEpisode);
    }
    modal.hidden = true;
    activeAnimeId = null;
    activeEpisode = null;
    activeSuggested = false;
  }

  document.querySelectorAll('.ep-note-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const card = btn.closest('.card');
      const valEl = card?.querySelector('.prog-val');
      const episode = valEl ? (parseInt(valEl.textContent) || 0) : 0;
      if (!episode) return;
      openModal(btn.dataset.animeId, episode, { cardTitle: card?.dataset.cardTitle || '' });
    });
  });

  modal.addEventListener('click', e => {
    if (e.target === modal) closeModal({ dismissed: true });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !modal.hidden) closeModal({ dismissed: true });
  });
  cancelBtn.addEventListener('click', () => closeModal({ dismissed: true }));

  saveBtn.addEventListener('click', async () => {
    if (!activeAnimeId || !activeEpisode) return;
    const note = fieldEl.value;
    const quote = quoteEl.value;
    const original = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.textContent = t('js_saving');
    try {
      const resp = await fetch(`/api/anime/${activeAnimeId}/episode-notes/${activeEpisode}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({note, quote}),
      });
      if (!resp.ok) throw new Error('save failed');
      updateIcon(activeAnimeId, note.trim().length > 0 || quote.trim().length > 0);
      closeModal();
    } catch {
      saveBtn.textContent = t('js_error_retry');
      saveBtn.disabled = false;
      return;
    }
    saveBtn.disabled = false;
    saveBtn.textContent = original;
  });

  delBtn.addEventListener('click', async () => {
    if (!activeAnimeId || !activeEpisode) return;
    delBtn.disabled = true;
    try {
      const resp = await fetch(`/api/anime/${activeAnimeId}/episode-notes/${activeEpisode}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({note: '', quote: ''}),
      });
      if (!resp.ok) throw new Error('delete failed');
      updateIcon(activeAnimeId, false);
      closeModal();
    } catch {
      /* leave modal open on failure */
    } finally {
      delBtn.disabled = false;
    }
  });

  // Called from the progress-stepper block above after a confirmed increment.
  // Quietly no-ops if the episode already has a note, if this exact episode
  // was already dismissed before, or if the modal is already open (never
  // steals focus from something the user is already doing) — none of these
  // checks run before the progress save itself resolves, so they can't add
  // any latency to the increment the user actually asked for.
  window.suggestEpisodeNote = async function suggestEpisodeNote(animeId, episode) {
    if (!episode || !modal.hidden) return;
    if (isDismissed(animeId, episode)) return;
    try {
      const resp = await fetch(`/api/anime/${animeId}/episode-notes/${episode}`);
      if (resp.ok) {
        const data = await resp.json();
        if (data.note) return; // already has one — nothing to suggest
      }
    } catch {
      return; // can't tell — stay quiet rather than risk a wrong/duplicate prompt
    }
    const stepperEl = document.querySelector(`.progress-stepper[data-anime-id="${animeId}"]`);
    const card = stepperEl?.closest('.card');
    openModal(animeId, episode, { suggested: true, cardTitle: card?.dataset.cardTitle || '' });
  };
})();

// ── Status select ─────────────────────────────────────────────────────────────
document.querySelectorAll('.status-select').forEach(select => {
  const card = select.closest('.card');
  const originalStatus = select.dataset.original;

  select.addEventListener('change', async () => {
    const newStatus = select.value;
    const prevStatus = select.dataset.current || originalStatus;
    select.dataset.current = newStatus;

    try {
      if (newStatus === 'COMPLETED') {
        const stepper = card?.querySelector('.progress-stepper');
        const episodes = stepper?.dataset.episodes ? parseInt(stepper.dataset.episodes) : null;
        if (episodes) {
          const r1 = await fetch(`/api/anime/${select.dataset.animeId}/progress`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({progress: episodes}),
          });
          if (!r1.ok) throw new Error('progress failed');
          const valEl = stepper.querySelector('.prog-val');
          if (valEl) valEl.textContent = episodes;
          const bar = card?.querySelector('.progress-bar');
          if (bar) bar.style.width = '100%';
        }
      }

      const resp = await fetch(`/api/anime/${select.dataset.animeId}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: newStatus}),
      });
      if (!resp.ok) throw new Error('request failed');
      if (newStatus !== originalStatus) {
        card.style.transition = 'opacity 0.3s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 300);
      }
    } catch {
      select.value = prevStatus;
      select.dataset.current = prevStatus;
    }
  });
});

// ── Recs genre + source filter ──────────────────────────────────────────────────
// Source filter (issue #13 — "New this season" vs "All") lives in a separate
// #rec-source-filter row and combines (AND) with the genre filter below, rather
// than replacing it — both narrow the same card grid at once.
const recFilterEl = document.getElementById('rec-genre-filter');
const recSourceFilterEl = document.getElementById('rec-source-filter');
if (recFilterEl) {
  const cards = [...document.querySelectorAll('.rec-card')];
  const genreSet = new Set();
  cards.forEach(c => (c.dataset.genres || '').split(',').forEach(g => { if (g.trim()) genreSet.add(g.trim()); }));
  const genres = [...genreSet].sort();

  let activeGenre = '';
  let activeSource = '';

  function applyRecFilters() {
    cards.forEach(c => {
      const cardGenres = (c.dataset.genres || '').split(',').map(g => g.trim());
      const genreMatch = !activeGenre || cardGenres.includes(activeGenre);
      const sourceMatch = !activeSource || c.dataset.source === activeSource;
      c.style.display = (genreMatch && sourceMatch) ? '' : 'none';
    });
  }

  const allBtn = document.createElement('button');
  allBtn.className = 'filter-btn active';
  allBtn.textContent = t('status_all');
  allBtn.addEventListener('click', () => setRecGenre('', allBtn));
  recFilterEl.appendChild(allBtn);

  genres.forEach(g => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.textContent = g;
    btn.addEventListener('click', () => setRecGenre(g, btn));
    recFilterEl.appendChild(btn);
  });

  function setRecGenre(genre, btn) {
    activeGenre = genre;
    recFilterEl.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyRecFilters();
  }

  if (recSourceFilterEl) {
    recSourceFilterEl.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeSource = btn.dataset.source || '';
        recSourceFilterEl.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        applyRecFilters();
      });
    });
  }
}

// ── Upcoming — list / weekly grid view toggle ──────────────────────────────────
(function () {
  const listBtn = document.getElementById('upcoming-view-list-btn');
  const gridBtn = document.getElementById('upcoming-view-grid-btn');
  const listView = document.getElementById('upcoming-list-view');
  const gridView = document.getElementById('upcoming-grid-view');
  if (!listBtn || !gridBtn || !listView || !gridView) return;

  const STORAGE_KEY = 'upcoming-view';

  function setView(view) {
    const isGrid = view === 'grid';
    listView.hidden = isGrid;
    gridView.hidden = !isGrid;
    listBtn.classList.toggle('active', !isGrid);
    gridBtn.classList.toggle('active', isGrid);
  }

  listBtn.addEventListener('click', () => {
    setView('list');
    localStorage.setItem(STORAGE_KEY, 'list');
  });
  gridBtn.addEventListener('click', () => {
    setView('grid');
    localStorage.setItem(STORAGE_KEY, 'grid');
  });

  const saved = localStorage.getItem(STORAGE_KEY);
  setView(saved === 'grid' ? 'grid' : 'list');
})();

// ── Upcoming — mark episode seen ──────────────────────────────────────────────
document.querySelectorAll('.btn-mark-seen').forEach(btn => {
  btn.addEventListener('click', async () => {
    const animeId = btn.dataset.animeId;
    const newProgress = parseInt(btn.dataset.progress) + 1;

    btn.disabled = true;
    btn.textContent = '…';

    try {
      const resp = await fetch(`/api/anime/${animeId}/progress`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({progress: newProgress}),
      });
      if (!resp.ok) throw new Error('request failed');
      btn.dataset.progress = newProgress;
      btn.textContent = '✓';
      const progressEl = btn.closest('.upcoming-item')?.querySelector('.upcoming-progress');
      if (progressEl) progressEl.textContent = t('upcoming_progress', {num: newProgress});
    } catch {
      btn.disabled = false;
      btn.textContent = t('upcoming_mark_seen_btn');
    }
  });
});

// ── Notes modal ───────────────────────────────────────────────────────────────
const notesModal    = document.getElementById('notes-modal');
const modalTitle    = document.getElementById('modal-title');
const modalDropHint = document.getElementById('modal-drop-hint');
const modalDrop     = document.getElementById('modal-drop');
const modalTags     = document.getElementById('modal-tags');
const modalNotes    = document.getElementById('modal-notes');
const modalPriority = document.getElementById('modal-priority');
const modalSave     = document.getElementById('modal-save');
const modalCancel   = document.getElementById('modal-cancel');
let activeCardEl    = null;
let dropMode        = false;

function openNotesModal(card, isDrop = false) {
  activeCardEl = card;
  dropMode = isDrop;
  modalTitle.textContent   = card.dataset.cardTitle || '';
  modalDrop.value          = card.dataset.drop || '';
  modalTags.value          = card.dataset.tags || '';
  modalNotes.value         = card.dataset.notes || '';
  modalPriority.value      = card.dataset.priority || '';
  if (modalDropHint) modalDropHint.hidden = !isDrop;
  modalSave.textContent    = isDrop ? t('lib_drop_btn') : t('settings_save_button');
  notesModal.hidden        = false;
  modalDrop.focus();
}

function closeNotesModal() {
  notesModal.hidden = true;
  activeCardEl = null;
}

if (notesModal) {
  document.querySelectorAll('.card').forEach(card => {
    card.addEventListener('click', e => {
      if (e.target.closest('button, select, a, input, textarea, .star')) return;
      window.location.href = `/anime/${card.dataset.animeId}/notes?back=${encodeURIComponent(card.dataset.back || 'WATCHING')}`;
    });
  });

  notesModal.addEventListener('click', e => {
    if (e.target === notesModal) closeNotesModal();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !notesModal.hidden) closeNotesModal();
  });

  modalCancel?.addEventListener('click', closeNotesModal);

  modalSave?.addEventListener('click', async () => {
    if (!activeCardEl) return;
    const animeId = activeCardEl.dataset.animeId;
    const payload = {
      drop_reason:         modalDrop.value,
      personal_tags:       modalTags.value,
      notes:               modalNotes.value,
      watch_next_priority: modalPriority.value,
    };
    const isDrop = dropMode;

    modalSave.disabled = true;
    modalSave.textContent = t('js_saving');
    let saveFailed = false;
    try {
      const r1 = await fetch(`/api/anime/${animeId}/notes`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!r1.ok) throw new Error('notes failed');

      if (isDrop) {
        const r2 = await fetch(`/api/anime/${animeId}/status`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: 'DROPPED'}),
        });
        if (!r2.ok) throw new Error('status failed');
        const droppedCardEl = activeCardEl;
        closeNotesModal();
        droppedCardEl.style.transition = 'opacity 0.3s';
        droppedCardEl.style.opacity = '0';
        setTimeout(() => droppedCardEl.remove(), 300);
        return;
      }

      activeCardEl.dataset.drop     = payload.drop_reason;
      activeCardEl.dataset.tags     = payload.personal_tags;
      activeCardEl.dataset.notes    = payload.notes;
      activeCardEl.dataset.priority = payload.watch_next_priority;

      const dropEl = activeCardEl.querySelector('.drop-note');
      const notesEl = activeCardEl.querySelector('.notes-preview');
      if (dropEl) dropEl.textContent = payload.drop_reason ? `↩ ${payload.drop_reason}` : '';
      if (notesEl) notesEl.textContent = payload.notes || '';

      if (typeof refreshTagFilterOptions === 'function') refreshTagFilterOptions();
      if (typeof applyLibraryFilters === 'function') applyLibraryFilters();

      closeNotesModal();
    } catch {
      saveFailed = true;
      modalSave.textContent = t('js_error_retry');
    } finally {
      modalSave.disabled = false;
      if (!saveFailed) modalSave.textContent = isDrop ? t('lib_drop_btn') : t('settings_save_button');
    }
  });

  document.querySelectorAll('.btn-drop').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const card = btn.closest('.card');
      if (card) openNotesModal(card, true);
    });
  });
}

// ── Delete anime ─────────────────────────────────────────────────────────────
document.querySelectorAll('.btn-delete-sm').forEach(btn => {
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    const card = btn.closest('.card');
    const animeId = btn.dataset.animeId;
    const title = card?.dataset.cardTitle || 'this anime';
    if (!confirm(t('js_confirm_delete', {title}))) return;

    btn.disabled = true;
    try {
      const resp = await fetch(`/api/anime/${animeId}/delete`, { method: 'POST' });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        alert(data.error || t('js_delete_failed'));
        btn.disabled = false;
        return;
      }
      if (card) {
        card.style.transition = 'opacity 0.3s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 300);
      }
    } catch {
      alert(t('js_delete_failed_connection'));
      btn.disabled = false;
    }
  });
});

// ── Queue filters: tag + episode count (issue #88) ──────────────────────────────
// Reuses the tag-filter dropdown pattern shipped for the library view (#74): dedupe
// tags case-insensitively from each item's data-tags, rebuild the <select> sorted,
// filter by exact (lowercased) tag match. Episode count uses a fixed bucket list
// instead of a built dropdown, since the buckets are a small fixed taxonomy rather
// than something derived from per-entry data. Both filters combine with AND, and
// only ever hide/show existing DOM nodes — the drag-reorder handler in queue.html
// still walks the full (visible + hidden) list on drop, so reordering within a
// filtered view keeps working exactly as it does unfiltered.
(function () {
  const list = document.getElementById('queue-list');
  const tagSel = document.getElementById('queue-tag-filter');
  const epSel = document.getElementById('queue-episode-filter');
  if (!list || (!tagSel && !epSel)) return;

  let activeTag = '';
  let activeEpisodeBucket = '';

  function episodeBucket(item) {
    const format = (item.dataset.format || '').toUpperCase();
    if (format === 'MOVIE') return 'movie';
    const episodes = parseInt(item.dataset.episodes, 10);
    if (!episodes) return 'ongoing';
    if (episodes <= 13) return 'short';
    if (episodes <= 26) return 'standard';
    return 'long';
  }

  function refreshTagOptions() {
    const seen = new Map();
    list.querySelectorAll('.queue-item').forEach(item => {
      (item.dataset.tags || '').split(',').forEach(t => {
        const tag = t.trim();
        if (tag && !seen.has(tag.toLowerCase())) seen.set(tag.toLowerCase(), tag);
      });
    });
    const current = tagSel.value;
    tagSel.querySelectorAll('option:not([value=""])').forEach(o => o.remove());
    [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0])).forEach(([key, label]) => {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = label;
      tagSel.appendChild(opt);
    });
    tagSel.value = seen.has(current) ? current : '';
    activeTag = tagSel.value;
  }

  function applyQueueFilters() {
    const items = [...list.querySelectorAll('.queue-item')];
    let visible = 0;
    items.forEach(item => {
      const itemTags = (item.dataset.tags || '').toLowerCase().split(',').map(t => t.trim());
      const tagOk = !activeTag || itemTags.includes(activeTag);
      const epOk = !activeEpisodeBucket || episodeBucket(item) === activeEpisodeBucket;
      const show = tagOk && epOk;
      item.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    const noResults = document.getElementById('queue-no-filter-results');
    if (noResults) {
      noResults.style.display = ((activeTag || activeEpisodeBucket) && visible === 0) ? '' : 'none';
    }
  }

  if (tagSel) {
    refreshTagOptions();
    tagSel.addEventListener('change', () => {
      activeTag = tagSel.value;
      applyQueueFilters();
    });
  }
  if (epSel) {
    epSel.addEventListener('change', () => {
      activeEpisodeBucket = epSel.value;
      applyQueueFilters();
    });
  }
}());

// ── Queue card click-through to notes (issue #259) ─────────────────────────────
// Mirrors the .card click delegation used on library.html (see the "Notes modal"
// section above) so a queue card is as obviously clickable as a library card —
// the previous approach relied solely on a small Unicode pencil link that turned
// out to render as a barely-visible mark in practice. The notes link itself (now
// styled as a legible .btn-notes-sm chip, see queue.html) stays in the DOM too,
// both as a fallback discoverable target and for keyboard/screen-reader access,
// since a bare click-delegated <li> isn't independently focusable.
document.querySelectorAll('.queue-item[data-anime-id]').forEach(item => {
  item.addEventListener('click', e => {
    if (e.target.closest('button, select, a, input, textarea, .star, .queue-drag-handle')) return;
    const back = item.dataset.notesBack || 'PLANNING';
    window.location.href = `/anime/${item.dataset.animeId}/notes?back=${encodeURIComponent(back)}`;
  });
});

// ── Mark watched (queue page) ─────────────────────────────────────────────────
document.querySelectorAll('.btn-mark-watched').forEach(btn => {
  btn.addEventListener('click', async () => {
    const animeId = btn.dataset.animeId;
    const episodes = btn.dataset.episodes ? parseInt(btn.dataset.episodes) : null;
    const item = btn.closest('.queue-item');

    btn.disabled = true;
    btn.textContent = '…';

    try {
      if (episodes) {
        const r1 = await fetch(`/api/anime/${animeId}/progress`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({progress: episodes}),
        });
        if (!r1.ok) throw new Error('progress failed');
      }

      const r2 = await fetch(`/api/anime/${animeId}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'COMPLETED'}),
      });
      if (!r2.ok) throw new Error('status failed');

      item.style.transition = 'opacity 0.3s';
      item.style.opacity = '0';
      setTimeout(() => item.remove(), 300);
    } catch {
      btn.disabled = false;
      btn.textContent = '✓ ' + t('queue_watched_btn');
    }
  });
});

// ── Start rewatch (queue page rewatch-reminder section, issue #191) ───────────
document.querySelectorAll('.btn-start-rewatch').forEach(btn => {
  btn.addEventListener('click', async () => {
    const animeId = btn.dataset.animeId;
    const item = btn.closest('.queue-item');

    btn.disabled = true;
    btn.textContent = '…';

    try {
      const resp = await fetch(`/api/anime/${animeId}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'REPEATING'}),
      });
      if (!resp.ok) throw new Error('request failed');

      item.style.transition = 'opacity 0.3s';
      item.style.opacity = '0';
      setTimeout(() => item.remove(), 300);
    } catch {
      btn.disabled = false;
      btn.textContent = t('queue_rewatch_start_btn');
    }
  });
});

// ── Move to Planning (queue page, Paused cards — issue #259) ───────────────────
// Same endpoint/pattern as .btn-mark-watched above ({status: 'PLANNING'} instead
// of 'COMPLETED'), kept as its own class rather than reusing .btn-add-planning —
// see the CSS comment above .btn-add-planning/.btn-start-rewatch for why a shared
// binding class across pages with different card-container structures silently
// double-fires and clobbers the intended status (confirmed the hard way in #191).
document.querySelectorAll('.btn-move-planning').forEach(btn => {
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    const animeId = btn.dataset.animeId;
    const item = btn.closest('.queue-item');

    btn.disabled = true;
    btn.textContent = '…';

    try {
      const resp = await fetch(`/api/anime/${animeId}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'PLANNING'}),
      });
      if (!resp.ok) throw new Error('request failed');

      item.style.transition = 'opacity 0.3s';
      item.style.opacity = '0';
      setTimeout(() => item.remove(), 300);
    } catch {
      btn.disabled = false;
      btn.textContent = '→ ' + t('queue_move_planning_btn');
    }
  });
});

// ── Add to planning (recommendations page) ────────────────────────────────────
document.querySelectorAll('.btn-add-planning').forEach(btn => {
  btn.addEventListener('click', async () => {
    const animeId = btn.dataset.animeId;
    const card = btn.closest('.rec-card');

    btn.disabled = true;
    btn.textContent = '…';

    try {
      const resp = await fetch(`/api/anime/${animeId}/status`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'PLANNING'}),
      });
      if (!resp.ok) throw new Error('request failed');

      card.style.transition = 'opacity 0.3s';
      card.style.opacity = '0';
      setTimeout(() => card.remove(), 300);
    } catch {
      btn.disabled = false;
      btn.textContent = t('rec_add_planning_btn');
    }
  });
});

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
(function () {
  const STATUS_KEYS = { '1': 'WATCHING', '2': 'COMPLETED', '3': 'DROPPED', '4': 'PLANNING', '5': 'PAUSED' };
  let focusIdx = -1;

  function isTyping() {
    const tag = document.activeElement?.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
      || document.activeElement?.isContentEditable;
  }

  function visibleCards() {
    return [...document.querySelectorAll('#library-grid .card')]
      .filter(c => c.style.display !== 'none');
  }

  function moveFocus(delta) {
    const cards = visibleCards();
    if (!cards.length) return;
    focusIdx = Math.max(0, Math.min(cards.length - 1, focusIdx + delta));
    const card = cards[focusIdx];
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    card.classList.add('kb-focus');
    cards.forEach((c, i) => { if (i !== focusIdx) c.classList.remove('kb-focus'); });
  }

  document.addEventListener('keydown', e => {
    if (isTyping()) {
      if (e.key === 'Escape') document.activeElement?.blur();
      return;
    }

    // / → focus nav search or library search
    if (e.key === '/') {
      e.preventDefault();
      (document.querySelector('.nav-search') || librarySearch)?.focus();
      return;
    }

    // Escape → close modal or clear kb focus
    if (e.key === 'Escape') {
      const modal = document.getElementById('notes-modal');
      if (modal && !modal.hidden) {
        modal.hidden = true;
        return;
      }
      document.querySelectorAll('.kb-focus').forEach(c => c.classList.remove('kb-focus'));
      focusIdx = -1;
      return;
    }

    // j/k → navigate library cards
    if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); moveFocus(+1); return; }
    if (e.key === 'k' || e.key === 'ArrowUp')   { e.preventDefault(); moveFocus(-1); return; }

    // 1-5 → jump to status tab
    if (STATUS_KEYS[e.key]) {
      const tabs = document.querySelectorAll('.status-tabs .tab');
      const target = [...tabs].find(t => t.href?.includes(`status=${STATUS_KEYS[e.key]}`));
      if (target) { e.preventDefault(); target.click(); }
      return;
    }
  });
}());
