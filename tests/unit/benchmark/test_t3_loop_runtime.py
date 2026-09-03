from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path


_ROOT = Path(__file__).parents[3]
_BENCHMARK = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark"
_HELPERS = _BENCHMARK / "loop-helpers.sh"
_OPENCODE_LOOP = _BENCHMARK / "run-workflow-loop.sh"
_OPENAI_LOOP = _BENCHMARK / "run-workflow-loop-opencode-openai.sh"
_LOOPS = (_OPENCODE_LOOP, _OPENAI_LOOP)


def _chmod_exec(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_helper(
    tmp_path: Path, command: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(_HELPERS))}; {command}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _chmod_exec(path)


def test_helpers_define_t3_assurance_runtime_bootstrap() -> None:
    source = _HELPERS.read_text(encoding="utf-8")

    assert "bootstrap_assurance_runtime" in source
    assert "preflight_assurance_wheels" in source
    assert "ASSURANCE_PYTHON_PREAMBLE" in source
    assert "uv sync --project" in source
    assert "uv tool install --from" in source
    assert "assurance-agent" in source
    assert "import assurance_kernel, assurance_agent" in source
    assert "select_product(DEFAULT_PRODUCT_ID)" in source
    assert 'AA_PRODUCT="${AA_PRODUCT:-assurance}"' in source
    assert '--product "${AA_PRODUCT:-assurance}"' in source
    assert "packages/assurance-kernel/_resources" not in source


def test_all_benchmark_loops_call_t3_bootstrap_before_skill_refresh() -> None:
    for path in _LOOPS:
        source = path.read_text(encoding="utf-8")
        boot = source.index("bootstrap_assurance_runtime")
        wheels = source.index("preflight_assurance_wheels")
        refresh = source.index('"$AA_BIN" skill refresh')
        assert boot < wheels < refresh, path.name
        assert "packages/assurance-kernel/_resources" not in source
        assert 'EVAL_ENGINE_ROOT="${EVAL_ENGINE_ROOT:-$AA_REPO_ROOT}"' in source
        assert 'AA_SKILLS_ROOT="${AA_SKILLS_ROOT:-$PROJECT_ROOT/skills}"' in source


def test_opencode_loops_select_product_before_packaged_resource_imports() -> None:
    for path in (_OPENCODE_LOOP, _OPENAI_LOOP):
        source = path.read_text(encoding="utf-8")
        preamble = source.index("ASSURANCE_PYTHON_PREAMBLE")
        verify = source.index("verify_packaged_skills")
        bounded = source.index("validate_bounded_agent_server")
        skills = source.index("validate_packaged_skill_server")
        assert preamble < verify < bounded < skills, path.name
        assert source.count("${ASSURANCE_PYTHON_PREAMBLE}") >= 4, path.name


def test_openai_loop_keeps_t3_bootstrap_parity_with_opencode() -> None:
    opencode = _OPENCODE_LOOP.read_text(encoding="utf-8")
    openai = _OPENAI_LOOP.read_text(encoding="utf-8")
    marker = '"$AA_BIN" skill refresh --sync-agents --sync-opencode-user-skills --sync-opencode-user-agents'
    opencode_boot = opencode[opencode.index("bootstrap_assurance_runtime") : opencode.index(marker)]
    openai_boot = openai[openai.index("bootstrap_assurance_runtime") : openai.index(marker)]
    assert opencode_boot == openai_boot


def test_bootstrap_assurance_runtime_keeps_existing_aa_and_skips_uv(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    uv_log = tmp_path / "uv.log"
    _write_exec(fake_bin / "aa", "#!/bin/sh\nexit 0\n")
    _write_exec(fake_bin / "python", "#!/bin/sh\nexit 0\n")
    _write_exec(
        fake_bin / "uv",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(uv_log))}\nexit 99\n',
    )

    result = _run_helper(
        tmp_path,
        (
            f"export PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin; "
            "export AA_BIN=aa; "
            f"export AA_PYTHON={shlex.quote(str(fake_bin / 'python'))}; "
            "bootstrap_assurance_runtime /tmp/does-not-matter; "
            'printf "product=%s bin=%s python=%s\\n" "$AA_PRODUCT" "$AA_BIN" "$AA_PYTHON"'
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "product=assurance" in result.stdout
    assert str(fake_bin / "python") in result.stdout
    assert not uv_log.exists()


def test_bootstrap_assurance_runtime_prefers_uv_sync_workspace_venv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    venv_bin = repo / ".venv" / "bin"
    uv_log = tmp_path / "uv.log"
    _write_exec(venv_bin / "aa", "#!/bin/sh\nexit 0\n")
    _write_exec(venv_bin / "python", "#!/bin/sh\nexit 0\n")
    fake_bin = tmp_path / "fake-bin"
    _write_exec(
        fake_bin / "uv",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(uv_log))}\nexit 0\n',
    )

    result = _run_helper(
        tmp_path,
        (
            f"export PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin; "
            "export AA_BIN=aa; "
            "unset AA_PYTHON; "
            f"bootstrap_assurance_runtime {shlex.quote(str(repo))}; "
            'printf "bin=%s python=%s\\n" "$AA_BIN" "$AA_PYTHON"'
        ),
    )

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text(encoding="utf-8").strip() == f"sync --project {repo}"
    assert result.stdout.strip() == f"bin={venv_bin / 'aa'} python={venv_bin / 'python'}"


def test_bootstrap_assurance_runtime_falls_back_to_tool_install(tmp_path: Path) -> None:
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    uv_log = tmp_path / "uv.log"
    fake_bin = tmp_path / "fake-bin"
    _write_exec(
        fake_bin / "uv",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(uv_log))}\n'
        'case "$1" in\n'
        "  sync) exit 1 ;;\n"
        "  tool) exit 0 ;;\n"
        "esac\n"
        "exit 1\n",
    )

    result = _run_helper(
        tmp_path,
        (
            f"export PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin; "
            "export AA_BIN=aa; "
            "unset AA_PYTHON; "
            f"bootstrap_assurance_runtime {shlex.quote(str(repo))}; "
            "printf exit=%s\\n $?"
        ),
    )

    log = uv_log.read_text(encoding="utf-8")
    assert "sync --project" in log
    assert f"tool install --from {repo} assurance-agent" in log
    assert "exit=1" in result.stdout


def test_bootstrap_assurance_runtime_pins_python_to_aa_shebang(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    aa_python = fake_bin / "python3.11"
    other_python = fake_bin / "other-python"
    _write_exec(aa_python, "#!/bin/sh\nexit 0\n")
    _write_exec(other_python, "#!/bin/sh\nexit 0\n")
    _write_exec(fake_bin / "aa", f"#!{aa_python}\n")

    result = _run_helper(
        tmp_path,
        (
            f"export PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin; "
            "export AA_BIN=aa; "
            f"export AA_PYTHON={shlex.quote(str(other_python))}; "
            "bootstrap_assurance_runtime /tmp/unused; "
            'printf "python=%s\\n" "$AA_PYTHON"'
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"python={aa_python}"


def test_preflight_assurance_wheels_imports_both_wheels_and_pins_product(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    python_log = tmp_path / "python.log"
    aa_log = tmp_path / "aa.log"
    _write_exec(
        fake_bin / "python",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(python_log))}\nexit 0\n',
    )
    _write_exec(
        fake_bin / "aa",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(aa_log))}\nexit 0\n',
    )

    result = _run_helper(
        tmp_path,
        (
            f"export AA_BIN={shlex.quote(str(fake_bin / 'aa'))}; "
            f"export AA_PYTHON={shlex.quote(str(fake_bin / 'python'))}; "
            "export AA_PRODUCT=assurance; "
            "preflight_assurance_wheels"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "import assurance_kernel, assurance_agent" in python_log.read_text(encoding="utf-8")
    assert "--product assurance --version" in aa_log.read_text(encoding="utf-8")
