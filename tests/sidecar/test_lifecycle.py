import pytest

from internal.application.lifecycle import ApplicationLifecycle, Resource


def test_partial_start_is_rolled_back_in_reverse_order():
    calls = []

    def fail():
        calls.append("start-b")
        raise ValueError("failed")

    lifecycle = ApplicationLifecycle(
        [
            Resource("a", lambda: calls.append("start-a"), lambda: calls.append("stop-a")),
            Resource("b", fail, lambda: calls.append("stop-b")),
            Resource("c", lambda: calls.append("start-c"), lambda: calls.append("stop-c")),
        ]
    )
    with pytest.raises(ValueError):
        lifecycle.start()
    lifecycle.stop()
    assert calls == ["start-a", "start-b", "stop-b", "stop-a"]
    assert lifecycle.state == "stopped"


def test_stop_keeps_releasing_after_resource_failure():
    calls = []

    def fail_stop():
        raise OSError("failed")

    lifecycle = ApplicationLifecycle(
        [
            Resource("a", lambda: None, lambda: calls.append("stopped")),
            Resource("b", lambda: None, fail_stop),
        ]
    )
    lifecycle.start()
    lifecycle.start()
    lifecycle.stop()
    lifecycle.stop()
    assert calls == ["stopped"]
    with pytest.raises(RuntimeError):
        lifecycle.start()


def test_incomplete_producer_shutdown_retains_dependencies_until_retry():
    calls = []
    results = iter([False, True])

    def stop_network():
        calls.append("network")
        return next(results)

    lifecycle = ApplicationLifecycle([
        Resource("lock", lambda: None, lambda: calls.append("lock")),
        Resource("database", lambda: None, lambda: calls.append("database")),
        Resource("network", lambda: None, stop_network, blocks_dependencies=True),
    ])
    lifecycle.start()
    assert lifecycle.stop() is False
    assert lifecycle.state == "stopping"
    assert calls == ["network"]
    assert lifecycle.stop() is True
    assert calls == ["network", "network", "database", "lock"]
    assert lifecycle.stop() is True


def test_critical_shutdown_exception_retains_dependency_ownership():
    calls = []

    def fail():
        raise OSError("shutdown failed")

    lifecycle = ApplicationLifecycle([
        Resource("database", lambda: None, lambda: calls.append("closed")),
        Resource("network", lambda: None, fail, blocks_dependencies=True),
    ])
    lifecycle.start()
    assert lifecycle.stop() is False
    assert lifecycle.state == "stopping"
    assert calls == []
