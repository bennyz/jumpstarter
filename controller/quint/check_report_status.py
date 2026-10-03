"""Exhaustively check ReportStatus fencing and require three negative controls.

Requires Quint 0.33.0, its Apalache 0.62.1 compiler runtime, Java 17+, and
TLC 1.7.4. Supply existing local runtimes; this script does not download tools.
Example (paths depend on your installation):

    python3 controller/quint/check_report_status.py \
        --quint /tmp/quint/node_modules/.bin/quint \
        --quint-home /tmp/quint/runtime --java /path/to/java \
        --tlc /path/to/tla2tools.jar --output /tmp/report-status-model

The Fenced model must pass TypeOK and NoStaleWrite. Legacy (omitted identity),
NoResourceVersion, and NoRetryCheck must each violate NoStaleWrite. These are
finite safety checks, not a proof of transport delivery or lifecycle liveness.
"""

import argparse
import hashlib
import os
import subprocess
from pathlib import Path

TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quint", type=Path, required=True)
    parser.add_argument("--quint-home", type=Path, required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--tlc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    quint = str(args.quint.resolve())
    version = subprocess.check_output([quint, "--version"], text=True).strip()
    if version != "0.33.0":
        raise RuntimeError(f"expected Quint 0.33.0, got {version}")
    if hashlib.sha256(args.tlc.read_bytes()).hexdigest() != TLC_SHA256:
        raise RuntimeError("expected TLC 1.7.4 with the pinned checksum")
    apalache = args.quint_home.resolve() / "apalache-dist-0.62.1/apalache/lib/apalache.jar"
    if not apalache.is_file():
        raise FileNotFoundError(apalache)
    source = Path(__file__).resolve().with_name("ReportStatus.qnt")
    environment = {**os.environ, "QUINT_HOME": str(args.quint_home.resolve())}
    if args.java != "java":
        environment["JAVA_HOME"] = str(Path(args.java).resolve().parent.parent)
        environment["PATH"] = str(Path(args.java).resolve().parent) + os.pathsep + os.environ["PATH"]
    subprocess.run([quint, "test", str(source), "--main=Fenced", "--backend=typescript", "--max-samples=1"],
                   check=True, env=environment)
    for module in ["Fenced", "Legacy", "NoResourceVersion", "NoRetryCheck"]:
        work = args.output.resolve() / module
        work.mkdir(parents=True, exist_ok=True)
        invariant = "TypeOK,NoStaleWrite" if module == "Fenced" else "NoStaleWrite"
        with (work / "model.tla").open("w") as output:
            subprocess.run([quint, "compile", str(source), f"--main={module}", "--target=tlaplus",
                            "--init=init", "--step=step", f"--invariant={invariant}", "--verbosity=0",
                            "--apalache-version=0.62.1"], cwd=work, stdout=output, check=True, env=environment)
        (work / "model.cfg").write_text("INIT q_init\nNEXT q_step\nINVARIANT q_inv\n")
        # The generated module is named after --main, irrespective of its file path.
        (work / "model.tla").rename(work / f"{module}.tla")
        with (work / "tlc.log").open("w") as output:
            result = subprocess.run([args.java, "-Xmx2g", "-XX:+UseParallelGC", "-cp",
                                     os.pathsep.join([str(args.tlc.resolve()), str(apalache)]),
                                     "tlc2.TLC", "-deadlock", "-workers", "1", "-config", "model.cfg",
                                     f"{module}.tla"], cwd=work, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=240, check=False)
        log = (work / "tlc.log").read_text()
        expected = "No error has been found" if module == "Fenced" else "Invariant q_inv is violated"
        if expected not in log or (result.returncode == 0) != (module == "Fenced"):
            raise RuntimeError(f"{module}: unexpected result; see {work / 'tlc.log'}")
        print(f"{module}: {expected}", flush=True)
        for line in log.splitlines():
            if "distinct states found" in line:
                print(line, flush=True)


if __name__ == "__main__":
    main()
