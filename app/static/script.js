const librarySearch = document.getElementById('library-search');
if (librarySearch) {
  librarySearch.addEventListener('input', () => {
    const q = librarySearch.value.toLowerCase();
    const cards = document.querySelectorAll('#library-grid .card');
    let visible = 0;
    cards.forEach(card => {
      const title = card.querySelector('.title')?.textContent.toLowerCase() || '';
      const sub   = card.querySelector('.title-sub')?.textContent.toLowerCase() || '';
      const tags  = [...card.querySelectorAll('.genre, .personal-tag')]
                      .map(t => t.textContent.toLowerCase()).join(' ');
      const match = !q || title.includes(q) || sub.includes(q) || tags.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    const noResults = document.getElementById('no-filter-results');
    if (noResults) noResults.style.display = (q && visible === 0) ? '' : 'none';
  });
}

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
      } catch {
        currentScore = prevScore;
        widget.dataset.score = prevScore;
        setFilled(prevScore);
      }
    });
  });
});

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
