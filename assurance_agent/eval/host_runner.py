"""Host-side Eval runner factory for commands and Improvement delivery ops.

Kept in ``assurance_agent.eval`` so the real suite runner wiring lives with the
Eval domain. Workflow delivery imports this as an intentional seam (see
``.importlinter``); CLI JSON params cannot carry callables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from assurance_agent.eval.fake_adapter import FixtureBackedFakeAdapter
from assurance_agent.workflow.graph.agent_api import AgentInvoker


_FakeAdapter = FixtureBackedFakeAdapter


def _resolve_adapter_factory(*, use_fake: bool, sut: Path):
    if use_fake:

        def fake_factory(**_: object) -> AgentInvoker:
            return _FakeAdapter()

        return fake_factory

    from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter

    agent_cmd = os.environ.get("AA_EVAL_AGENT_CMD", "cursor-agent")

    def real_factory(*, sut_dir: Path | None = None, **_: object) -> AgentInvoker:
        return HeadlessAdapter(agent_cmd=agent_cmd, cwd=sut_dir or sut)

    return real_factory


def build_eval_runner(engine_root: Path, sut_root: Path):
    """Build the shared real ``eval_runner`` used by memory Improvement evaluate.

    Explicit ``sut_dir`` / ``engine_root`` call kwargs win over the bound roots.
    """

    data_root = Path(engine_root)
    bound_sut = Path(sut_root)

    def eval_runner(
        *,
        suite: str,
        sut_dir: Path | None = None,
        engine_root: Path | None = None,
        extra_memory_dir: Path | None = None,
        source_change_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        from assurance_agent.eval.baseline import read_baseline, read_run_manifest
        from assurance_agent.eval.metrics import read_metrics
        from assurance_agent.eval.paths import run_dir as run_dir_for
        from assurance_agent.eval.plan import load_suite
        from assurance_agent.eval.runner import run_suite

        resolved_sut = Path(sut_dir) if sut_dir is not None else bound_sut
        resolved_engine = Path(engine_root) if engine_root is not None else data_root
        suite_obj, suite_file = load_suite(resolved_engine, suite)
        baseline_entry = read_baseline(resolved_engine).get(suite)

        # Real validation: use the real agent adapter (cursor-agent) so the
        # candidate memory overlay actually influences generation. Fake stays
        # available as an explicit opt-in (AA_EVAL_FAKE_ADAPTER) for CI/tests
        # and for deterministic suites where memory has no effect.
        use_fake = bool(os.environ.get("AA_EVAL_FAKE_ADAPTER"))
        adapter_factory = _resolve_adapter_factory(use_fake=use_fake, sut=resolved_sut)

        run_id, gate = run_suite(
            suite_file=suite_file,
            project_root=resolved_engine,
            sut_dir=resolved_sut,
            adapter_factory=adapter_factory,
            fixtures_root=resolved_sut / "eval-fixtures",
            extra_memory_dir=extra_memory_dir,
            repeat=suite_obj.regression.repeat if suite_obj.regression is not None else 1,
            change_ids=source_change_ids,
        )
        manifest = read_run_manifest(run_dir_for(resolved_sut, run_id))
        metrics: dict[str, Any] = {}
        try:
            metrics = read_metrics(run_dir_for(resolved_sut, run_id)).metrics
        except Exception:
            pass
        return {
            "run_id": run_id,
            "verdict": gate.verdict,
            "metrics": metrics,
            "hard_gate_failures": list(gate.hard_gate_failures),
            "suite_contract": suite_obj.model_dump(mode="json"),
            "baseline_metrics": baseline_entry.metrics if baseline_entry is not None else None,
            "suite_version": manifest.suite_version,
            "repeat": manifest.repeat,
            "regression_policy_sha256": manifest.regression_policy_sha256,
            "baseline_suite_version": (baseline_entry.suite_version if baseline_entry is not None else None),
            "baseline_repeat": baseline_entry.repeat if baseline_entry is not None else None,
            "baseline_regression_policy_sha256": (
                baseline_entry.regression_policy_sha256 if baseline_entry is not None else None
            ),
        }

    return eval_runner


__all__ = ["build_eval_runner"]
