from api.diagnostics import summarize_values


def test_repeated_measurement_statistics_are_not_single_run_values():
    result = summarize_values([10, 20, 30, 40, 50])
    assert result["count"] == 5
    assert result["mean"] == 30
    assert result["median"] == 30
    assert result["min"] == 10
    assert result["max"] == 50
    assert result["p50"] == 30
    assert result["p95"] == 48
    assert result["std"] > 0


def test_missing_measurements_remain_empty_not_zero():
    result = summarize_values([])
    assert result["count"] == 0
    assert result["mean"] is None
    assert result["p95"] is None
