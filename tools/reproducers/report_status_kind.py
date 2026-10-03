"""Reproduce stale ReportStatus writes inside the dedicated local Kind cluster.

    python3 tools/reproducers/report_status_kind.py --keep-namespace

Requires Go, Docker, kind, kubectl, and jumpstarter-lease-verification with the
Jumpstarter CRDs installed. Builds a local image and loads it into Kind. Two
Jobs run the original and fixed handlers against the real Kubernetes API.
An original-handler failure and a fixed-handler pass are the expected result.

The fixture runs the production ReportStatus handler and generated gRPC client
over TCP inside each pod. Authentication and LeaseRef assignment are controlled
by the fixture. A barrier and detached server cancellation inject the ambiguous
timeout deterministically. Neither report requests lease release.

The baseline uses the original controller_service.go via a temporary Go overlay,
retaining the new wire schema so both binaries receive identical requests. The
original handler ignores lease_name. The checkout stays intact. Every kubectl
call uses an explicit kubeconfig/context; Job RBAC is namespace-scoped.
Logs and manifests remain in --output. By default the namespace is deleted.
"""

import argparse
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = "jumpstarter-lease-verification"
CONTEXT = f"kind-{CLUSTER}"
BASELINE = "59af604d59abbad93b65055146a5716572ca3ca6"
TEST = "^TestReportStatusLateHandlerInCluster$/^EXPORTER_STATUS_(AVAILABLE|LEASE_READY)$/^before-write$"


def capture(command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs)


def logged(command, path, **kwargs):
    with path.open("w") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed; see {path}")


def manifest(namespace, image):
    metadata = {"namespace": namespace}
    resources = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {**metadata, "name": "reproducer"}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {**metadata, "name": "reproducer"}, "rules": [{
             "apiGroups": ["jumpstarter.dev"],
             "resources": ["exporters", "exporters/status", "leases", "leases/status"],
             "verbs": ["create", "get", "update", "patch", "delete"],
         }]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {**metadata, "name": "reproducer"},
         "subjects": [{"kind": "ServiceAccount", "name": "reproducer", "namespace": namespace}],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "reproducer"}},
    ]
    for version in ["baseline", "fixed"]:
        resources.append({
            "apiVersion": "batch/v1", "kind": "Job", "metadata": {**metadata, "name": version},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 120, "template": {
                "metadata": {"labels": {"app": "report-status-reproducer", "version": version}},
                "spec": {
                    "serviceAccountName": "reproducer", "restartPolicy": "Never",
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "fsGroup": 65532,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}],
                    "containers": [{
                        "name": "reproducer", "image": image, "imagePullPolicy": "Never",
                        "command": [f"/{version}.test", "-test.v", "-test.timeout=90s", f"-test.run={TEST}"],
                        "env": [{"name": "REPORT_STATUS_KIND_NAMESPACE", "value": namespace}],
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                      "limits": {"memory": "512Mi"}},
                    }],
                },
            }},
        })
    return resources


def wait_job(kubectl, namespace, name, output):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        job = json.loads(capture([*kubectl, "-n", namespace, "get", "job", name, "-o", "json"]))
        state = job.get("status", {})
        if state.get("succeeded") or state.get("failed"):
            log = capture([*kubectl, "-n", namespace, "logs", f"job/{name}"])
            (output / f"{name}.log").write_text(log)
            (output / f"{name}-job.json").write_text(json.dumps(job, indent=2) + "\n")
            return bool(state.get("succeeded")), log
        time.sleep(1)
    logged([*kubectl, "-n", namespace, "describe", "pods"], output / f"{name}-timeout.log")
    raise TimeoutError(f"{name}: see {output / f'{name}-timeout.log'}")


def verify_cluster(kubectl):
    config = json.loads(capture([*kubectl, "config", "view", "--minify", "-o", "json"]))
    server = config["clusters"][0]["cluster"]["server"]
    if config["current-context"] != CONTEXT or not server.startswith("https://127.0.0.1:"):
        raise RuntimeError(f"refusing non-local test cluster: {server}")
    capture([*kubectl, "get", "crd", "exporters.jumpstarter.dev", "leases.jumpstarter.dev"])
    nodes = json.loads(capture([*kubectl, "get", "nodes", "-o", "json"]))
    architectures = {node["status"]["nodeInfo"]["architecture"] for node in nodes["items"]}
    if len(architectures) != 1 or not architectures.issubset({"amd64", "arm64"}):
        raise RuntimeError(f"unsupported Kind node architectures: {architectures}")
    return architectures.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=BASELINE, help="local Git revision supplying the original service")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-namespace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="report-status-kind-"))
    output.mkdir(parents=True, exist_ok=True)
    print(f"Evidence: {output}", flush=True)
    environment = {**os.environ, "KIND_EXPERIMENTAL_PROVIDER": "docker"}
    kubeconfig = output / "kubeconfig"
    kubeconfig.write_text(capture(["kind", "get", "kubeconfig", "--name", CLUSTER], env=environment))
    kubeconfig.chmod(0o600)
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT]
    architecture = verify_cluster(kubectl)
    build = output / "image"
    build.mkdir(exist_ok=True)
    source = "controller/internal/service/controller_service.go"
    baseline_sha = capture(["git", "rev-parse", args.baseline], cwd=ROOT).strip()
    baseline_source = output / "baseline_controller_service.go"
    baseline_source.write_text(capture(["git", "show", f"{baseline_sha}:{source}"], cwd=ROOT))
    overlay = output / "baseline-overlay.json"
    overlay.write_text(json.dumps({"Replace": {str(ROOT / source): str(baseline_source)}}))
    build_env = {**environment, "GOOS": "linux", "GOARCH": architecture, "CGO_ENABLED": "0"}
    for version in ["baseline", "fixed"]:
        print(f"Building {version} handler for linux/{architecture}", flush=True)
        command = ["go", "test", "-c", "-o", str(build / f"{version}.test")]
        if version == "baseline":
            command.extend(["-overlay", str(overlay)])
        logged([*command, "./internal/service"], output / f"{version}-build.log",
               cwd=ROOT / "controller", env=build_env, timeout=600)
    (build / "Dockerfile").write_text("FROM scratch\nCOPY baseline.test fixed.test /\n")
    suffix = secrets.token_hex(4)
    namespace = f"report-status-repro-{suffix}"
    image = f"jumpstarter-report-status-repro:{suffix}"
    print(f"Loading {image} into {CLUSTER}", flush=True)
    logged(["docker", "build", "--network=none", "--platform", f"linux/{architecture}", "-t", image, str(build)],
           output / "image-build.log", env=environment, timeout=120)
    logged(["kind", "load", "docker-image", image, "--name", CLUSTER], output / "image-load.log",
           env=environment, timeout=180)
    resources = manifest(namespace, image)
    # Run sequentially because both fixtures use the same object names.
    support, jobs = resources[:3], resources[3:]
    (output / "resources.json").write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}, indent=2)
    )
    capture([*kubectl, "create", "namespace", namespace])
    print(f"Kind namespace: {namespace}", flush=True)
    try:
        subprocess.run([*kubectl, "create", "-f", "-"],
                       input=json.dumps({"apiVersion": "v1", "kind": "List", "items": support}),
                       text=True, check=True, stdout=subprocess.DEVNULL)
        results = {}
        for job in jobs:
            name = job["metadata"]["name"]
            subprocess.run([*kubectl, "create", "-f", "-"], input=json.dumps(job), text=True,
                           check=True, stdout=subprocess.DEVNULL)
            passed, log = wait_job(kubectl, namespace, name, output)
            for line in log.splitlines():
                if any(marker in line for marker in ["client deadline", "B accepted:", "A completed:"]):
                    print(f"{name}: {line.strip()}", flush=True)
            if name == "baseline":
                expected = not passed and log.count("late A report changed B:") == 2
            else:
                expected = passed and log.count("A completed: code=FailedPrecondition") == 2
            results[name] = {"passed": passed, "expected_outcome": expected}
            if not expected:
                raise RuntimeError(f"unexpected {name} outcome; see {output / f'{name}.log'}")
        (output / "result.json").write_text(json.dumps({"cluster": CLUSTER, "namespace": namespace,
                                                        "image": image, "baseline": baseline_sha,
                                                        "results": results}, indent=2) + "\n")
        print("REPRODUCED: original handler overwrote B in both cases; fixed handler rejected both stale writes.",
              flush=True)
    finally:
        if args.keep_namespace:
            print(f"Retained namespace: {namespace}", flush=True)
        else:
            subprocess.run([*kubectl, "delete", "namespace", namespace, "--wait=false"], check=True)


if __name__ == "__main__":
    main()
