"""Focused tests for the PageViewSummary dataclass in models.py."""

from hyqs.pipeline.models import PageViewSummary


def _make_summary() -> PageViewSummary:
    return PageViewSummary(
        totals={
            "views": 42,
            "unique_visitors": 10,
            "days_with_traffic": 5,
            "first_seen": "2026-08-01T00:00:00",
            "last_seen": "2026-09-06T00:00:00",
        },
        by_day=[{"day": "2026-09-06", "views": 12, "unique_visitors": 4}],
        by_country=[{"country": "US", "views": 20, "unique_visitors": 6}],
        by_city=[{"country": "US", "city": "NYC", "views": 8}],
        by_ref=[{"ref": "hn", "views": 15, "unique_visitors": 5}],
        by_referrer=[{"referrer_host": "news.ycombinator.com", "views": 15}],
        by_device=[
            {"os_family": "macOS", "ua_family": "Chrome", "views": 30, "unique_visitors": 8}
        ],
        recent=[
            {
                "ts": "2026-09-06T00:00:00",
                "path": "/how-it-works",
                "ref": "hn",
                "country": "US",
                "city": "NYC",
                "os_family": "macOS",
                "ua_family": "Chrome",
                "lang": "en-US",
                "tz": "America/New_York",
                "screen": "1920x1080",
            }
        ],
        by_path=[{"path": "/how-it-works", "views": 12, "unique_visitors": 4}],
        by_viewport=[{"viewport": "desktop", "views": 12, "unique_visitors": 4}],
        by_color_scheme=[{"color_scheme": "dark", "views": 12}],
        by_lang=[{"lang": "en-US", "views": 12, "unique_visitors": 4}],
        by_hour=[{"hour": hour, "views": 12 if hour == 9 else 0} for hour in range(24)],
        by_os_version=[{"os_family": "macOS", "os_version": "15", "views": 12}],
        pages_per_visitor=[{"pages": 1, "visitors": 3}, {"pages": 2, "visitors": 1}],
        returning={"single_day": 3, "multi_day": 1},
    )


def test_to_dict_returns_exactly_the_expected_top_level_keys():
    summary = _make_summary()

    assert set(summary.to_dict().keys()) == {
        "totals",
        "by_day",
        "by_country",
        "by_city",
        "by_ref",
        "by_referrer",
        "by_device",
        "recent",
        "by_path",
        "by_viewport",
        "by_color_scheme",
        "by_lang",
        "by_hour",
        "by_os_version",
        "pages_per_visitor",
        "returning",
    }


def test_to_dict_preserves_field_shapes():
    summary = _make_summary()

    data = summary.to_dict()

    assert data["totals"] == summary.totals
    assert data["by_day"] == summary.by_day
    assert data["by_country"] == summary.by_country
    assert data["by_city"] == summary.by_city
    assert data["by_ref"] == summary.by_ref
    assert data["by_referrer"] == summary.by_referrer
    assert data["by_device"] == summary.by_device
    assert data["recent"] == summary.recent
    assert data["by_path"] == summary.by_path
    assert data["by_viewport"] == summary.by_viewport
    assert data["by_color_scheme"] == summary.by_color_scheme
    assert data["by_lang"] == summary.by_lang
    assert data["by_hour"] == summary.by_hour
    assert data["by_os_version"] == summary.by_os_version
    assert data["pages_per_visitor"] == summary.pages_per_visitor
    assert data["returning"] == summary.returning


def test_new_fields_have_compatibility_defaults():
    summary = PageViewSummary(
        totals={},
        by_day=[],
        by_country=[],
        by_city=[],
        by_ref=[],
        by_referrer=[],
        by_device=[],
        recent=[],
    )

    assert summary.by_path == []
    assert summary.by_viewport == []
    assert summary.by_color_scheme == []
    assert summary.by_lang == []
    assert summary.by_hour == []
    assert summary.by_os_version == []
    assert summary.pages_per_visitor == []
    assert summary.returning == {"single_day": 0, "multi_day": 0}


def test_to_dict_never_exposes_visitor_hash():
    summary = _make_summary()

    data = summary.to_dict()

    assert "visitor_hash" not in data
    for entry in data["recent"]:
        assert "visitor_hash" not in entry
