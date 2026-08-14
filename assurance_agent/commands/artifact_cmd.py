"""Bounded model-authored artifact materialization commands."""

import base64
import binascii
from pathlib import Path

import click

from assurance_agent.risk.safety import RiskSafetyError, assert_inside_project


_MAX_PAYLOAD_CHARS = 1_400_000


@click.group("artifact")
def artifact_group() -> None:
    """Materialize workflow artifacts through a structured command."""


@artifact_group.command("write")
@click.option("--path", "artifact_path", required=True, help="Artifact path inside the project.")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
@click.option("--payload-base64", default=None, help="Base64-encoded UTF-8 file content.")
@click.option("--content", default=None, help="Literal UTF-8 file content.")
def artifact_write(
    artifact_path: str,
    project_dir: str | None,
    payload_base64: str | None,
    content: str | None,
) -> None:
    """Write one UTF-8 artifact after containment and payload validation.

    The OpenCode boundary plugin supplies the agent-specific path allowlist. This
    CLI layer independently guarantees project containment and rejects workflow
    state, which always remains orchestrator-owned.
    """
    try:
        project_root = Path(project_dir).resolve() if project_dir else Path.cwd().resolve()
        if not project_root.is_dir():
            raise RiskSafetyError(f"--project-dir is not a directory: {project_root}")
        if (payload_base64 is None) == (content is None):
            raise RiskSafetyError("provide exactly one of --payload-base64 or --content")
        payload = payload_base64 if payload_base64 is not None else content
        assert payload is not None
        if not artifact_path.strip() or not payload or len(payload) > _MAX_PAYLOAD_CHARS:
            raise RiskSafetyError("artifact path and payload must be non-empty and within size limits")
        target = Path(artifact_path)
        target = target.resolve() if target.is_absolute() else (project_root / target).resolve()
        assert_inside_project(project_root, target)
        if target.name == "workflow-state.yaml":
            raise RiskSafetyError("workflow-state.yaml is orchestrator-owned")
        if payload_base64 is not None:
            try:
                content = base64.b64decode(payload_base64, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise RiskSafetyError("--payload-base64 must encode valid UTF-8") from exc
        assert content is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        click.secho(f"Wrote {target}", fg="green")
    except RiskSafetyError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
