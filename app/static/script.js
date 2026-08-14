// ── Library search ────────────────────────────────────────────────────────────
const librarySearch = document.getElementById('library-search');
if (librarySearch) {
  librarySearch.addEventListener('input', applyLibraryFilters);
}

// ── Library format filter ─────────────────────────────────────────────────────
let activeFormat = '';
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
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
    const label = season.charAt(0) + season.slice(1).toLowerCase() + ' ' + year;
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
    const show    = textOk && fmtOk && seasonOk && scoreOk;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const noResults = document.getElementById('no-filter-results');
  if (noResults) noResults.style.display = ((q || activeFormat || activeSeason) && visible === 0) ? '' : 'none';
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

// ── Progress stepper ──────────────────────────────────────────────────────────
document.querySelectorAll('.progress-stepper').forEach(stepper => {
  const animeId = stepper.dataset.animeId;
  const maxEp = stepper.dataset.episodes ? parseInt(stepper.dataset.episodes) : Infinity;
  const valEl = stepper.querySelector('.prog-val');
  const bar = stepper.closest('.card')?.querySelector('.progress-bar');
  let current = parseInt(valEl.textContent) || 0;
  let committed = current;
  let saveTimer = null;

  function updateBar(val) {
    if (bar && maxEp !== Infinity) {
      bar.style.width = Math.min((val / maxEp) * 100, 100) + '%';
    }
  }

  async function save() {
    const target = current;
    try {
      const resp = await fetch(`/api/anime/${animeId}/progress`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({progress: target}),
      });
      if (!resp.ok) throw new Error('request failed');
      committed = target;
      stepper.closest('.card')?.setAttribute('data-progress', target);
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
      current = next;
      valEl.textContent = current;
      updateBar(current);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 600);
    });
  });
});

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

// ── Recs genre filter ─────────────────────────────────────────────────────────
const recFilterEl = document.getElementById('rec-genre-filter');
if (recFilterEl) {
  const cards = [...document.querySelectorAll('.rec-card')];
  const genreSet = new Set();
  cards.forEach(c => (c.dataset.genres || '').split(',').forEach(g => { if (g.trim()) genreSet.add(g.trim()); }));
  const genres = [...genreSet].sort();

  let activeGenre = '';
  const allBtn = document.createElement('button');
  allBtn.className = 'filter-btn active';
  allBtn.textContent = 'All';
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
    cards.forEach(c => {
      const genres = (c.dataset.genres || '').split(',').map(g => g.trim());
      c.style.display = (!genre || genres.includes(genre)) ? '' : 'none';
    });
  }
}

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
      if (progressEl) progressEl.textContent = `You're at episode ${newProgress}`;
    } catch {
      btn.disabled = false;
      btn.textContent = '+1 ep';
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
  modalSave.textContent    = isDrop ? 'Drop' : 'Save';
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
      openNotesModal(card);
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
    modalSave.textContent = 'Saving…';
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

      closeNotesModal();
    } catch {
      modalSave.textContent = 'Error — retry';
    } finally {
      modalSave.disabled = false;
      if (modalSave.textContent === 'Saving…') modalSave.textContent = isDrop ? 'Drop' : 'Save';
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
    if (!confirm(`Remove "${title}" from your library and AniList? This can't be undone here.`)) return;

    btn.disabled = true;
    try {
      const resp = await fetch(`/api/anime/${animeId}/delete`, { method: 'POST' });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        alert(data.error || 'Delete failed.');
        btn.disabled = false;
        return;
      }
      if (card) {
        card.style.transition = 'opacity 0.3s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 300);
      }
    } catch {
      alert('Delete failed — check connection.');
      btn.disabled = false;
    }
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
      btn.textContent = '✓ Watched';
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
      btn.textContent = '+ Planning';
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
