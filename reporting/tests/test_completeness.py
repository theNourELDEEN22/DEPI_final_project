
from reporting.completeness import (
    check_data_completeness,
    wait_for_data_completeness,
)


ALL_SOURCES_AVAILABLE = {
    "sales": True,
    "returns": True,
    "products": True,
    "prices": True,
    "cost": True,
    "inventory_movements": True,
}


def test_all_sources_available():
    status = check_data_completeness(ALL_SOURCES_AVAILABLE)

    assert status.is_complete is True
    assert status.missing_sources == ()


def test_missing_sales_source():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "sales": False,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("sales",)


def test_multiple_missing_sources():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "returns": False,
        "products": False,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("returns", "products")


def test_unspecified_source_is_missing():
    sources = {
        "sales": True,
        "returns": True,
        "prices": True,
        "cost": True,
        "inventory_movements": True,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("products",)


def test_missing_prices_source():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "prices": False,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("prices",)


def test_missing_cost_source():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "cost": False,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("cost",)


def test_missing_inventory_movements_source():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "inventory_movements": False,
    }

    status = check_data_completeness(sources)

    assert status.is_complete is False
    assert status.missing_sources == ("inventory_movements",)


def test_wait_returns_when_sources_become_available():
    responses = [
        {
            **ALL_SOURCES_AVAILABLE,
            "sales": False,
        },
        ALL_SOURCES_AVAILABLE,
    ]

    calls = {"count": 0}

    def availability_reader():
        result = responses[calls["count"]]
        calls["count"] += 1
        return result

    status = wait_for_data_completeness(
        availability_reader,
        timeout_seconds=2,
        retry_interval_seconds=0.01,
    )

    assert status.is_complete is True
    assert status.missing_sources == ()
    assert calls["count"] == 2


def test_wait_returns_partial_after_deadline():
    sources = {
        **ALL_SOURCES_AVAILABLE,
        "returns": False,
    }

    status = wait_for_data_completeness(
        lambda: sources,
        timeout_seconds=0,
        retry_interval_seconds=0.01,
    )

    assert status.is_complete is False
    assert status.missing_sources == ("returns",)
    assert status.cutoff_time is not None


def test_wait_rejects_negative_timeout():
    import pytest

    with pytest.raises(ValueError):
        wait_for_data_completeness(
            lambda: ALL_SOURCES_AVAILABLE,
            timeout_seconds=-1,
        )


def test_wait_rejects_non_positive_retry_interval():
    import pytest

    with pytest.raises(ValueError):
        wait_for_data_completeness(
            lambda: ALL_SOURCES_AVAILABLE,
            retry_interval_seconds=0,
        )
