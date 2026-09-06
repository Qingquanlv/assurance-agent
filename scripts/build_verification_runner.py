"""Explicit, network-enabled preparation of the pinned verification runner.

Normal execution reads the resulting record and uses --pull=never. This command
alone installs dependencies/builds the OCI image; no sample image IDs are trusted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from assurance_execution.operations.verified_process import runner_source_inputs

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmark/assurance-product/fixtures/user-oracle"


def run(argv: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(argv, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def main() -> None:
    lock = json.loads((FIXTURE / "runner-lock.json").read_bytes())
    output = ROOT / lock["qualification_path"]
    output.parent.mkdir(parents=True, exist_ok=True)
    # Fail before building anything when Docker cannot provide real qualification.
    run(["docker", "info", "--format", "{{.ServerVersion}}"], capture=True)
    source_inputs = runner_source_inputs(ROOT)
    wheels = output.parent / "wheels"
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir()
    # Reuse the wheel smoke script's exact package build list and build flags.
    smoke = (ROOT / "scripts/assurance_product_wheel_smoke_test.sh").read_text()
    match = re.search(r"for package in \\\n(.*?)\ndo", smoke, re.S)
    if match is None:
        raise ValueError("wheel smoke build list is missing")
    packages = match.group(1).replace("\\", "").split()
    for package in packages:
        run(
            [
                "uv",
                "build",
                "--offline",
                "--wheel",
                "--no-sources",
                "--python",
                "3.11",
                "--package",
                package,
                "--out-dir",
                str(wheels),
            ]
        )
    requirements = output.parent / "runner-requirements.txt"
    run(
        [
            "uv",
            "export",
            "--locked",
            "--all-packages",
            "--no-dev",
            "--group",
            "verification-runner",
            "--no-emit-workspace",
            "--no-annotate",
            "--output-file",
            str(requirements),
        ],
        capture=True,
    )
    run(["docker", "pull", "--platform", lock["platform"], lock["base_reference"]])
    base = json.loads(run(["docker", "image", "inspect", lock["base_reference"]], capture=True))[0]
    base_digest = next(item for item in base["RepoDigests"] if item.startswith("python@sha256:"))
    wheel_digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(wheels.glob("*.whl"))
    }
    execution = [name for name in wheel_digests if name.startswith("assurance_execution-")]
    if len(execution) != 1:
        raise ValueError("exactly one actual execution wheel is required")
    record = {
        "schema_version": "1",
        "source_inputs": source_inputs,
        "platform": lock["platform"],
        "base_image_digest": base_digest,
        "wheels": wheel_digests,
        "execution_wheel": execution[0],
        "dependency_lock_digest": hashlib.sha256(requirements.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["build_input_digest"] = hashlib.sha256(encoded).hexdigest()
    image_id_file = output.parent / "image-id.txt"
    run(
        [
            "docker",
            "build",
            "--pull=false",
            "--platform",
            lock["platform"],
            "--file",
            str(FIXTURE / "runner.Dockerfile"),
            "--build-arg",
            "BASE_IMAGE=" + base_digest,
            "--build-arg",
            "BUILD_INPUT_DIGEST=" + record["build_input_digest"],
            "--iidfile",
            str(image_id_file),
            str(output.parent),
        ]
    )
    record["image_id"] = image_id_file.read_text().strip()
    if source_inputs != runner_source_inputs(ROOT):
        raise ValueError("source changed during build; rebuild before qualification")
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    from assurance_execution.operations.verified_process import DockerVerificationHost

    DockerVerificationHost(source_root=ROOT, qualification_path=output).preflight()
    print(output)


if __name__ == "__main__":
    main()
