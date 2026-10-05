"""Public receipt publication is not a publish callback."""

from __future__ import annotations


def test_flow_does_not_offer_a_publish_callback() -> None:
    import graph_engine.flow as flow

    assert "Projected" not in flow.__all__
    assert not hasattr(flow, "Projected")
