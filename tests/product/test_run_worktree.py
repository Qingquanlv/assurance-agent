from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_git_sut(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("ok\n", encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / ".keep").write_text("keep\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "init")
    return root


def test_ensure_run_worktree_is_the_first_checkout_outside_the_sut(tmp_path: Path) -> None:
    from assurance_product.sut_worktree import ensure_run_worktree

    sut = _make_git_sut(tmp_path / "vue-fastapi-admin")
    leftover = sut / "qa" / ".qa.yaml"
    leftover.parent.mkdir()
    leftover.write_text("change:\n  change_id: OLD\n", encoding="utf-8")

    worktree = ensure_run_worktree(sut, "BOOT-new-1")

    assert worktree == (tmp_path / ".worktrees" / "vue-fastapi-admin" / "BOOT-new-1").resolve()
    assert worktree.is_dir()
    assert not worktree.is_relative_to(sut.resolve())
    assert (worktree / "app" / "main.py").is_file()
    assert not (worktree / "qa").exists()
    assert leftover.read_text(encoding="utf-8") == "change:\n  change_id: OLD\n"
    assert (worktree / ".opencode" / "plugins" / "assurance-boundary.mjs").is_file()
    listed = _git(sut, "worktree", "list", "--porcelain").stdout
    assert str(worktree) in listed
    _git(sut, "worktree", "remove", "--force", str(worktree))


def test_ensure_run_worktree_reuses_when_already_inside_that_checkout(tmp_path: Path) -> None:
    from assurance_product.sut_worktree import ensure_run_worktree

    sut = _make_git_sut(tmp_path / "vue-fastapi-admin")
    first = ensure_run_worktree(sut, "BOOT-inside")
    again = ensure_run_worktree(first, "BOOT-inside")
    assert again == first
    _git(sut, "worktree", "remove", "--force", str(first))


def test_ensure_run_worktree_reuses_the_same_change_checkout(tmp_path: Path) -> None:
    from assurance_product.sut_worktree import ensure_run_worktree

    sut = _make_git_sut(tmp_path / "vue-fastapi-admin")
    first = ensure_run_worktree(sut, "BOOT-reuse")
    second = ensure_run_worktree(sut, "BOOT-reuse")
    assert second == first
    _git(sut, "worktree", "remove", "--force", str(first))


def test_ensure_run_worktree_refuses_a_nested_path_that_is_not_the_git_root(
    tmp_path: Path,
) -> None:
    from assurance_product.sut_worktree import ensure_run_worktree

    parent = tmp_path / "aa"
    sut = parent / "benchmark"
    sut.mkdir(parents=True)
    (sut / "app").mkdir()
    (sut / "app" / "main.py").write_text("ok\n", encoding="utf-8")
    _git(parent, "init")
    _git(parent, "add", "-A")
    _git(parent, "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "parent")

    with pytest.raises(ValueError, match="refuse to worktree the parent repo"):
        ensure_run_worktree(sut, "BOOT-nested")


def test_ensure_run_worktree_stays_in_place_when_the_sut_is_not_git(tmp_path: Path) -> None:
    from assurance_product.sut_worktree import ensure_run_worktree

    sut = tmp_path / "plain"
    sut.mkdir()
    (sut / "app").mkdir()
    assert ensure_run_worktree(sut, "BOOT-plain") == sut.resolve()
    assert not (tmp_path / ".worktrees").exists()


def test_run_bootstrap_uses_the_new_worktree_before_preflight(tmp_path: Path) -> None:
    from assurance_product.bootstrap.contracts import OpenCodeHandleV1
    from assurance_product.bootstrap.driver import run_bootstrap
    from tests.product.test_bootstrap_contracts import _spec

    sut = _make_git_sut(tmp_path / "vue-fastapi-admin")
    aa = sut / ".aa"
    aa.mkdir()
    (aa / "policy.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text("version: 1\n", encoding="utf-8")
    _git(sut, "add", "-A")
    _git(sut, "-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "aa")
    leftover = sut / "qa" / ".qa.yaml"
    leftover.parent.mkdir()
    leftover.write_text("change:\n  change_id: OLD\n", encoding="utf-8")
    seen: dict[str, Path] = {}
    handle = OpenCodeHandleV1(endpoint="http://127.0.0.1:4101", pid=1)

    def start_serve(**kwargs):
        seen["opencode_dir"] = Path(kwargs["project_dir"]).resolve()
        return handle

    def prepare(**kwargs):
        seen["prepare_dir"] = Path(kwargs["project_dir"]).resolve()
        run_dir = kwargs["run_dir"]
        return {
            "product": "assurance-opencode",
            "binding_dist": "assurance-product-bindings-test",
            "binding_declaration": "pkg/assurance-deployment-plugin.json",
            "config_tree": run_dir / "config-tree",
            "input_path": run_dir / "product-input.json",
        }

    def run_inv(**kwargs):
        seen["run_dir"] = Path(kwargs["project_dir"]).resolve()
        return ({"status": "succeeded"}, "completed")

    status = run_bootstrap(
        project_dir=sut,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-boot-1",
        environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
        start_opencode_serve=start_serve,
        stop_opencode=lambda _handle: None,
        prepare_composition=prepare,
        start_invocation=lambda **kwargs: {"invocation_id": kwargs["invocation_id"]},
        run_invocation=run_inv,
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda url, timeout: None,
    )

    expected = (tmp_path / ".worktrees" / "vue-fastapi-admin" / "BOOT-boot-1").resolve()
    assert status.phase == "terminal"
    assert seen["opencode_dir"] == expected
    assert seen["prepare_dir"] == expected
    assert seen["run_dir"] == expected
    assert leftover.read_text(encoding="utf-8") == "change:\n  change_id: OLD\n"
    assert not (expected / "qa" / ".qa.yaml").exists()
    _git(sut, "worktree", "remove", "--force", str(expected))


def test_run_invocation_uses_the_new_worktree_as_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product import cli
    from assurance_product.cli import _run_invocation

    sut = _make_git_sut(tmp_path / "vue-fastapi-admin")
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "_resolve_and_audit",
        lambda **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(cli, "_authorize_secrets", lambda *_args, **_kwargs: object())

    def _bind(project_dir, change_id, *, create):
        seen["bind_dir"] = Path(project_dir).resolve()
        seen["change_id"] = change_id
        seen["create"] = create
        return object()

    monkeypatch.setattr(cli, "_bind_workspace", _bind)

    class _App:
        def run(self, **kwargs):
            seen["run_dir"] = Path(kwargs["project_dir"]).resolve()
            return (object(), "completed", 0)

    monkeypatch.setattr(cli, "AssuranceProductApplication", lambda: _App())

    _run_invocation(
        project_dir=sut,
        change_id="BOOT-run-1",
        invocation_id="BOOT-run-1",
        product="assurance-opencode",
        binding_dist="dist",
        binding_entrypoint="entry",
        binding_declaration="decl",
        config_tree="tree",
        entrypoint="full",
        input_path=None,
        secrets=(),
    )

    expected = (tmp_path / ".worktrees" / "vue-fastapi-admin" / "BOOT-run-1").resolve()
    assert seen["bind_dir"] == expected
    assert seen["run_dir"] == expected
    assert seen["create"] is True
    _git(sut, "worktree", "remove", "--force", str(expected))
