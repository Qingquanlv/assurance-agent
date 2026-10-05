"""Build the eight family codegen and codegen-review ops from one template."""

from __future__ import annotations

from typing import Any, Protocol, cast

from agent_runtime_contracts.ops import Agent, AgentOp, Dir, Finalize, InputError, Out, Prepare
from agent_runtime_contracts.qa_paths import qa_route

from assurance_generation.contracts.agent import CodegenBoundInputV1, CodegenInputV1
from assurance_generation.operations.resolve_inputs import open_reviewed_case
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_generation.contracts.codegen import CodegenAuthoringV1, CodegenResultV1
from assurance_generation.contracts.families import GENERATION_FAMILIES, LayerName
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.ops import router


class _CodegenHooks(Protocol):
    def before(self, ctx: object, business: CodegenInputV1) -> CodegenInputV1: ...

    def request(self, *args: object, **kwargs: object) -> object: ...

    def after(self, *args: object, **kwargs: object) -> object: ...


class _ReviewHooks(Protocol):
    def request(self, *args: object, **kwargs: object) -> object: ...

    def after(self, *args: object, **kwargs: object) -> object: ...


def _family(family: str) -> LayerName:
    if family not in GENERATION_FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return cast(LayerName, family)


def _loaded(ctx: object, business: CodegenBoundInputV1, error: type[Exception]) -> CodegenInputV1:
    payload = business.model_dump(mode="json")
    raw_ref = payload.pop("reviewed_case_ref")
    if raw_ref is None:
        payload["reviewed_case"] = None
    else:
        root = ctx.project_root  # type: ignore[attr-defined]
        opened = open_reviewed_case(root, EvidenceArtifactRefV1.model_validate(raw_ref), error)
        payload["reviewed_case"] = opened.model_dump(mode="json")
    return CodegenInputV1.model_validate(payload)


def build_codegen(
    family: str, hooks: object
) -> AgentOp[CodegenBoundInputV1, CodegenAuthoringV1, CodegenResultV1]:
    name = _family(family)
    typed = cast(_CodegenHooks, hooks)

    def before(ctx: object, business: CodegenBoundInputV1) -> CodegenInputV1:
        return typed.before(ctx, _loaded(ctx, business, InputError))

    return router.agent(
        f"{name}.codegen",
        input=CodegenBoundInputV1,
        prepare=Prepare(
            hook=cast(Any, before),
            writes=("qa/tests",),
            request=cast(Any, typed.request),
        ),
        agent=Agent(
            profile="assurance-v1-test-author",
            skill=f"aa-{name}-codegen",
            result=CodegenAuthoringV1,
            writes=(
                Out(f"{name}-summary", f"qa/results/codegen/{name}-codegen-summary.md"),
                Out(f"{name}-files", f"qa/results/codegen/{name}-generated-files.json"),
                Dir("qa/tests", name=f"{name}-tests"),
            ),
        ),
        finalize=Finalize(hook=cast(Any, typed.after)),
        output=CodegenResultV1,
    )


def build_codegen_review(
    family: str, hooks: object
) -> AgentOp[CodegenBoundInputV1, PlanReviewAuthoring, PlanReview]:
    name = _family(family)
    typed = cast(_ReviewHooks, hooks)

    def before(ctx: object, business: CodegenBoundInputV1) -> CodegenInputV1:
        return _loaded(ctx, business, InputError)

    return router.agent(
        f"{name}.codegen-review",
        input=CodegenBoundInputV1,
        prepare=Prepare(hook=cast(Any, before), request=cast(Any, typed.request)),
        agent=Agent(
            profile="assurance-v1-reviewer",
            skill=f"aa-{name}-codegen-reviewer",
            result=PlanReviewAuthoring,
            writes=(
                Out(f"{name}-review", f"qa/results/review/{name}-codegen-review.json"),
                Out(f"{name}-review-summary", f"qa/results/review/{name}-codegen-review-summary.md"),
            ),
        ),
        finalize=Finalize(hook=cast(Any, typed.after), writes=qa_route(f"codegen/{name}/reviews")),
        output=PlanReview,
    )


__all__ = ["build_codegen", "build_codegen_review"]
