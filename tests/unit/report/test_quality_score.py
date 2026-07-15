from assurance_agent.workflow.report.quality_score import ScoreDimension, compute_quality_score


def dims(**kw: ScoreDimension) -> dict:
    base = {
        "functional": ScoreDimension(active=False, ratio=0.0, weight=0),
        "coverage": ScoreDimension(active=False, ratio=0.0, weight=0),
        "fuzz": ScoreDimension(active=False, ratio=0.0, weight=0),
        "performance": ScoreDimension(active=False, ratio=0.0, weight=0),
    }
    base.update(kw)
    return base


def test_m1_functional_only_full_pass_is_100() -> None:
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=1.0, weight=70),
    ))
    assert score == 100
    assert bd.functional == 100.0
    assert bd.coverage == "N/A"


def test_m1_functional_and_coverage_hand_computed() -> None:
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=0.8, weight=70),
        coverage=ScoreDimension(active=True, ratio=1.0, weight=30),
    ))
    assert score == 86
    assert bd.functional == 56.0
    assert bd.coverage == 30.0
    assert bd.fuzz == "N/A"


def test_m3_all_active_partial_functional() -> None:
    score, bd = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=0.5, weight=50),
        coverage=ScoreDimension(active=True, ratio=1.0, weight=20),
        fuzz=ScoreDimension(active=True, ratio=1.0, weight=15),
        performance=ScoreDimension(active=True, ratio=1.0, weight=15),
    ))
    assert score == 75
    assert bd.functional == 25.0
    assert bd.performance == 15.0


def test_no_active_dimension_is_zero() -> None:
    score, bd = compute_quality_score(dims())
    assert score == 0
    assert bd.functional == "N/A"


def test_ratio_is_clamped() -> None:
    score, _ = compute_quality_score(dims(
        functional=ScoreDimension(active=True, ratio=5.0, weight=70),
    ))
    assert score == 100
