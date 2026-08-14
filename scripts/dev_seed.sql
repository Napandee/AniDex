-- Seeds a handful of fake anime + library_entries rows for local UI testing, so
-- there's something to click on without needing real AniList/Crunchyroll credentials.
--
-- Usage (after registering the first account, which auto-bootstraps as admin):
--   psql "$DATABASE_URL" -v email="'you@example.com'" -f scripts/dev_seed.sql
--
-- Safe to re-run: anime rows upsert by id, library_entries are keyed by
-- (user_id, anime_id) per schema.sql's UNIQUE constraint.

INSERT INTO anime (id, title_romaji, title_english, format, status, episodes, cover_image_url)
VALUES
    (98707, 'Clockwork Planet', 'Clockwork Planet', 'TV', 'FINISHED', 12, NULL),
    (101922, 'Kimetsu no Yaiba', 'Demon Slayer', 'TV', 'FINISHED', 26, NULL),
    (108725, 'SPY x FAMILY', 'SPY x FAMILY', 'TV', 'RELEASING', NULL, NULL),
    (21, 'ONE PIECE', 'One Piece', 'TV', 'RELEASING', NULL, NULL)
ON CONFLICT (id) DO NOTHING;

INSERT INTO library_entries (user_id, anime_id, status, progress)
SELECT u.id, v.anime_id, v.status, v.progress
FROM users u
CROSS JOIN (VALUES
    (98707,  'WATCHING'::text, 4),
    (101922, 'COMPLETED'::text, 26),
    (108725, 'PLANNING'::text, 0),
    (21,     'PAUSED'::text, 120)
) AS v(anime_id, status, progress)
WHERE u.email = :email
ON CONFLICT (user_id, anime_id) DO NOTHING;
