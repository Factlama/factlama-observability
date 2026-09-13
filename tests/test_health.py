from operations.health import CheckResult, HealthCheck, HealthState, aggregate_health


def test_all_up_checks_aggregate_to_up() -> None:
    checks = [
        HealthCheck("a", lambda: CheckResult(HealthState.UP)),
        HealthCheck("b", lambda: CheckResult(HealthState.UP)),
    ]

    result = aggregate_health(checks)

    assert result.state is HealthState.UP
    assert set(result.checks) == {"a", "b"}


def test_one_down_check_aggregates_to_down() -> None:
    checks = [
        HealthCheck("a", lambda: CheckResult(HealthState.UP)),
        HealthCheck("b", lambda: CheckResult(HealthState.DOWN, detail="db unreachable")),
    ]

    result = aggregate_health(checks)

    assert result.state is HealthState.DOWN
    assert result.checks["b"].detail == "db unreachable"


def test_empty_check_list_aggregates_to_up() -> None:
    result = aggregate_health([])

    assert result.state is HealthState.UP
    assert result.checks == {}
