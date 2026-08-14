from anilist_sync_common import is_plausible_match


def test_movie_watch_against_tv_entry_is_implausible():
    # Live-action Death Note (movie) vs the anime (TV, 37 eps) — the collision
    # case is_plausible_match exists to catch.
    entry = {"format": "TV", "total_episodes": 37}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is False


def test_tv_watch_against_tv_entry_is_plausible():
    entry = {"format": "TV", "total_episodes": 24}
    assert is_plausible_match(entry, watched_format="TV", watched_episode_count=5) is True


def test_movie_watch_against_movie_entry_is_plausible():
    entry = {"format": "MOVIE", "total_episodes": 1}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is True


def test_episode_count_overrunning_anilist_total_is_implausible():
    entry = {"format": "TV", "total_episodes": 24}
    assert is_plausible_match(entry, watched_format="TV", watched_episode_count=99) is False


def test_missing_format_or_count_info_defaults_to_plausible():
    # Can't rule anything out without data to compare — must not false-positive-reject.
    entry = {"format": None, "total_episodes": None}
    assert is_plausible_match(entry, watched_format=None, watched_episode_count=None) is True


def test_ova_entry_is_not_treated_as_a_movie():
    entry = {"format": "OVA", "total_episodes": 3}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is False
