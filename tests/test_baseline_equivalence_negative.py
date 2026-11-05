"""Disposable negative proof: never merge this probe-only test."""


def test_candidate_only_failure_must_not_be_baseline_equivalent():
    assert False, "probe-only candidate failure is absent from the exact upstream baseline"
