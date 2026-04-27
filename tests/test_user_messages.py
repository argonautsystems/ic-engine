from ic_engine.config import user_messages


def test_wait_partial_refresh_formats_section_and_eta():
    rendered = user_messages.WAIT_PARTIAL_REFRESH.format(section="news", eta_seconds=30)
    assert "news data was stale" in rendered
    assert "(30s)" in rendered


def test_cache_hit_banner_formats_hash():
    rendered = user_messages.CACHE_HIT_BANNER.format(
        age_seconds=12,
        envelope_hmac_short="abcdef123456",
    )
    assert "12s ago" in rendered
    assert "abcdef123456" in rendered


def test_narrator_refusal_formats_missing_data_class():
    rendered = user_messages.NARRATOR_FABRICATION_REFUSAL.format(
        missing_data_class="risk-adjusted return"
    )
    assert "risk-adjusted return" in rendered
    assert "without making up numbers" in rendered

