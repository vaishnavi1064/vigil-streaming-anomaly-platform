"""Statically validate the deployment layer: k8s manifests, Terraform, monitoring configs.

    python scripts/validate_deploy.py
    python scripts/validate_deploy.py --report docs/results/deploy-validation.txt

**This validates configuration. It deploys nothing and reaches no cluster.** That distinction
is the whole point of the script: `docs/EVALUATION.md` section 9 claims these artifacts are
valid, and this is the command behind that claim, so the claim can be re-checked rather than
believed.

Tools are expected in `$VIGIL_TOOLS` (default `E:/tools` on this machine, since that is where
they were installed deliberately outside the system path). A missing tool is reported as
skipped rather than passed -- a validation suite that silently skips is how an unvalidated
file ends up described as validated.

One tool is deliberately absent from the list: `kubectl apply --dry-run=client`. It cannot
run offline. Even with `--validate=false` it performs API discovery against a live server, so
on a machine with no cluster it fails for a reason that has nothing to do with the manifests.
kubeconform is what actually validates them against the Kubernetes JSON schemas.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = Path(os.environ.get("VIGIL_TOOLS", "E:/tools"))
K8S_VERSION = "1.31.0"


class Result:
    def __init__(self, name: str, status: str, detail: str = "") -> None:
        self.name = name
        self.status = status  # pass | fail | skip
        self.detail = detail.strip()


def tool(name: str) -> Path | None:
    path = TOOLS / f"{name}.exe"
    if path.exists():
        return path
    path = TOOLS / name
    return path if path.exists() else None


def run(name: str, binary: str, args: list[str], cwd: Path | None = None) -> Result:
    exe = tool(binary)
    if exe is None:
        return Result(name, "skip", f"{binary} not found in {TOOLS}")
    proc = subprocess.run(
        [str(exe), *args], cwd=cwd or REPO, capture_output=True, text=True, check=False
    )
    output = (proc.stdout + proc.stderr).strip()
    lines = output.splitlines()
    # `kubectl kustomize` prints the whole rendered manifest set, which is thousands of
    # lines and would bury every other result. Summarise a success; keep a failure whole,
    # because that is the case where the detail is the point.
    if proc.returncode == 0 and len(lines) > 12:
        kinds = sum(1 for line in lines if line.startswith("kind: "))
        summary = f"{len(lines)} lines of output"
        if kinds:
            summary = f"rendered {kinds} resources ({len(lines)} lines)"
        output = summary
    return Result(name, "pass" if proc.returncode == 0 else "fail", output)


def manifest_files() -> list[str]:
    # kustomization.yaml is not a Kubernetes resource and kubeconform would reject it.
    return [str(p) for p in sorted((REPO / "k8s").glob("*.yaml")) if p.name != "kustomization.yaml"]


def check_dashboard_promql() -> list[Result]:
    """Parse every dashboard expression with promtool, by wrapping each as a recording rule.

    Grafana ships no offline validator, so the JSON is checked for well-formedness here and
    its PromQL is handed to the one parser that can actually judge it.
    """
    dashboard = REPO / "monitoring" / "grafana" / "vigil-dashboard.json"
    try:
        parsed = json.loads(dashboard.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Result("grafana dashboard JSON", "fail", str(exc))]

    exprs: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("expr"), str):
                exprs.append(node["expr"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(parsed)
    panels = sum(1 for p in parsed.get("panels", []) if p.get("type") != "row")
    results = [
        Result("grafana dashboard JSON", "pass", f"{panels} panels, {len(exprs)} expressions")
    ]

    rules = "groups:\n  - name: dashboard\n    rules:\n" + "".join(
        f"      - record: dashboard:expr{i}\n        expr: {expr!r}\n"
        for i, expr in enumerate(exprs)
    )
    scratch = REPO / ".dashboard-exprs.yml"
    scratch.write_text(rules, encoding="utf-8")
    try:
        results.append(
            run("grafana dashboard PromQL", "promtool", ["check", "rules", str(scratch)])
        )
    finally:
        scratch.unlink(missing_ok=True)
    return results


def check_metric_names() -> Result:
    """Every vigil_* metric the dashboard and rules reference must be one the API exports.

    This is the check that catches a dashboard panel querying a metric nobody emits, which
    renders as an empty graph rather than as an error and is therefore easy to ship.
    """
    referenced: set[str] = set()
    for relative in (
        "monitoring/grafana/vigil-dashboard.json",
        "monitoring/prometheus/rules/vigil.yml",
    ):
        referenced |= set(
            re.findall(r"\bvigil_[a-z_]+", (REPO / relative).read_text(encoding="utf-8"))
        )

    try:
        sys.path.insert(0, str(REPO / "src"))
        from vigil.api.metrics import collect

        exported = {
            line.split("{")[0].split(" ")[0]
            for line in collect().decode().splitlines()
            if line and not line.startswith("#")
        }
    except Exception as exc:  # noqa: BLE001 - the stores may not be running
        return Result("metric names", "skip", f"could not read the exporter: {exc}")

    missing = sorted(referenced - exported)
    if missing:
        return Result(
            "metric names", "fail", "referenced but never exported: " + ", ".join(missing)
        )
    return Result("metric names", "pass", f"{len(referenced)} referenced, all exported")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None, help="write the transcript here")
    args = parser.parse_args(argv)

    results: list[Result] = []

    results.append(
        run(
            "kubeconform (manifests)",
            "kubeconform",
            ["-strict", "-summary", "-kubernetes-version", K8S_VERSION, *manifest_files()],
        )
    )
    results.append(run("kustomize build", "kubectl", ["kustomize", "k8s"]))
    results.append(run("terraform fmt", "terraform", ["fmt", "-check", "-list", "-recursive"]))
    results.append(run("terraform validate", "terraform", ["validate"], cwd=REPO / "terraform"))
    results.append(
        run(
            "promtool (rules)",
            "promtool",
            ["check", "rules", "monitoring/prometheus/rules/vigil.yml"],
        )
    )
    results.append(
        run(
            "promtool (config)",
            "promtool",
            ["check", "config", "monitoring/prometheus/prometheus.yml"],
        )
    )
    results.append(
        run(
            "amtool (alertmanager)",
            "amtool",
            ["check-config", "monitoring/alertmanager/alertmanager.yml"],
        )
    )
    results.extend(check_dashboard_promql())
    results.append(check_metric_names())

    lines = ["VIGIL DEPLOYMENT VALIDATION", "=" * 78, ""]
    lines.append("Static validation only. Nothing here was applied to a cluster.")
    lines.append("")
    width = max(len(r.name) for r in results)
    for result in results:
        lines.append(f"  {result.status.upper():<5} {result.name.ljust(width)}")
        for line in result.detail.splitlines():
            lines.append(f"        {line}")
    failed = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    lines += [
        "",
        f"{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped",
    ]

    report = "\n".join(lines)
    print(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n", encoding="utf-8")
        print(f"\nreport written to {args.report}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
