document.addEventListener('DOMContentLoaded', () => {
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
});
