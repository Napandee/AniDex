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
    const show    = textOk && fmtOk;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const noResults = document.getElementById('no-filter-results');
  if (noResults) noResults.style.display = ((q || activeFormat) && visible === 0) ? '' : 'none';
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

// ── Inline notes panel ────────────────────────────────────────────────────────
document.querySelectorAll('.notes-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    const animeId = btn.dataset.animeId;
    const panel = document.getElementById(`notes-panel-${animeId}`);
    if (!panel) return;

    if (!panel.hidden) {
      panel.hidden = true;
      return;
    }

    panel.querySelector('[name=drop_reason]').value  = btn.dataset.drop || '';
    panel.querySelector('[name=personal_tags]').value = btn.dataset.tags || '';
    panel.querySelector('[name=notes]').value         = btn.dataset.notes || '';
    panel.querySelector('[name=watch_next_priority]').value = btn.dataset.priority || '';
    panel.hidden = false;
  });
});

document.querySelectorAll('.notes-cancel').forEach(btn => {
  btn.addEventListener('click', () => {
    document.getElementById(`notes-panel-${btn.dataset.animeId}`).hidden = true;
  });
});

document.querySelectorAll('.notes-save').forEach(btn => {
  btn.addEventListener('click', async () => {
    const animeId = btn.dataset.animeId;
    const panel = document.getElementById(`notes-panel-${animeId}`);
    const payload = {
      drop_reason:        panel.querySelector('[name=drop_reason]').value,
      personal_tags:      panel.querySelector('[name=personal_tags]').value,
      notes:              panel.querySelector('[name=notes]').value,
      watch_next_priority: panel.querySelector('[name=watch_next_priority]').value,
    };

    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      const resp = await fetch(`/api/anime/${animeId}/notes`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error('request failed');

      const toggle = document.querySelector(`.notes-toggle[data-anime-id="${animeId}"]`);
      if (toggle) {
        toggle.dataset.drop     = payload.drop_reason;
        toggle.dataset.tags     = payload.personal_tags;
        toggle.dataset.notes    = payload.notes;
        toggle.dataset.priority = payload.watch_next_priority;
        const hasContent = payload.drop_reason || payload.personal_tags || payload.notes;
        toggle.textContent = hasContent ? '✏ Edit notes' : '+ Add notes';
      }
      panel.hidden = true;
    } catch {
      btn.textContent = 'Error — retry';
    } finally {
      btn.disabled = false;
      if (btn.textContent === 'Saving…') btn.textContent = 'Save';
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

// ── Keyboard shortcut: / to focus search ─────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
    e.preventDefault();
    librarySearch?.focus();
  }
});
