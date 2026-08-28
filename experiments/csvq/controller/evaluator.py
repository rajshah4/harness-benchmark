#!/usr/bin/env python3
"""External evaluator: run the hidden suite against a candidate binary.

Generates expected outputs by running the oracle at grade time, then
runs the candidate against every case and compares.

Usage:
    python3 evaluator.py <candidate_binary> [--task-dir <dir>]

The task-dir must contain:
  - oracle-bin-linux (the reference binary)
  - fixtures/ (input files)
  - hidden-suite/cases.json (case definitions, no expected outputs)
"""
import json
import os
import subprocess
import sys
import argparse


def normalize_stdout(s: str) -> str:
    """Strip trailing whitespace from each line, per comparison rules."""
    return "\n".join(line.rstrip() for line in s.split("\n"))


def run_binary(binary, args, stdin_data):
    """Run a binary and capture exit code, stdout, stderr."""
    try:
        proc = subprocess.run(
            [binary] + args,
            input=stdin_data,
            capture_output=True,
            timeout=15,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", errors="replace"),
            "stderr": proc.stderr.decode("utf-8", errors="replace"),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "TIMEOUT", "timeout": True}
    except FileNotFoundError:
        return {"exit_code": -127, "stdout": "", "stderr": f"binary not found: {binary}"}


def get_stdin_data(case, fixtures_dir):
    """Resolve stdin for a case."""
    stdin_name = case.get("stdin")
    if not stdin_name:
        return None
    if stdin_name == "people":
        path = os.path.join(fixtures_dir, "people.csv")
        with open(path, "rb") as f:
            return f.read()
    if stdin_name == "empty":
        return b""
    return None


def run_case(oracle_bin, candidate_bin, case, fixtures_dir):
    """Run oracle and candidate for a case, compare, return result."""
    # Substitute $FIXTURE placeholder in args with absolute fixture paths
    args = [a.replace("$FIXTURE", fixtures_dir) if isinstance(a, str) else a
            for a in case["args"]]
    stdin_data = get_stdin_data(case, fixtures_dir)

    expected = run_binary(oracle_bin, args, stdin_data)
    actual = run_binary(candidate_bin, args, stdin_data)

    exit_match = actual["exit_code"] == expected["exit_code"]
    stdout_match = normalize_stdout(actual["stdout"]) == normalize_stdout(expected["stdout"])

    passed = exit_match and stdout_match

    return {
        "passed": passed,
        "weight": case["weight"],
        "case_id": case["id"],
        "exit_match": exit_match,
        "stdout_match": stdout_match,
        "actual_exit": actual["exit_code"],
        "expected_exit": expected["exit_code"],
        "reason": "" if passed else (
            "exit_mismatch" if not exit_match else "stdout_mismatch"
        ),
    }


def grade(candidate_bin, task_dir, oracle_bin=None):
    if oracle_bin is None:
        # Prefer the Linux binary; fall back to the macOS binary for local testing
        linux_bin = os.path.join(task_dir, "oracle-bin-linux")
        mac_bin = os.path.join(task_dir, "oracle-bin")
        if os.path.exists(linux_bin):
            oracle_bin = linux_bin
        elif os.path.exists(mac_bin):
            oracle_bin = mac_bin
        else:
            raise FileNotFoundError(f"no oracle binary found in {task_dir}")
    if not os.path.exists(oracle_bin):
        raise FileNotFoundError(f"oracle binary not found: {oracle_bin}")
    os.chmod(oracle_bin, 0o755)

    cases_path = os.path.join(task_dir, "hidden-suite", "cases.json")
    with open(cases_path) as f:
        data = json.load(f)
    cases = data["cases"] if "cases" in data else data

    fixtures_dir = os.path.join(task_dir, "fixtures")
    results = []
    total_weight = 0
    passed_weight = 0

    for case in cases:
        result = run_case(oracle_bin, candidate_bin, case, fixtures_dir)
        results.append(result)
        total_weight += case["weight"]
        if result["passed"]:
            passed_weight += case["weight"]

    score = (passed_weight / total_weight * 100) if total_weight > 0 else 0
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed

    return {
        "score": round(score, 1),
        "passed_cases": passed,
        "failed_cases": failed,
        "total_cases": len(results),
        "passed_weight": passed_weight,
        "total_weight": total_weight,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Grade a candidate binary against the oracle")
    parser.add_argument("binary", help="Path to the candidate binary")
    parser.add_argument("--task-dir", default=None, help="Path to the task directory")
    parser.add_argument("--oracle", default=None, help="Override oracle binary path")
    args = parser.parse_args()

    task_dir = args.task_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "tasks", "csvq"
    )
    task_dir = os.path.abspath(task_dir)

    if not os.path.exists(args.binary):
        print(f"ERROR: candidate binary not found: {args.binary}", file=sys.stderr)
        sys.exit(1)
    os.chmod(args.binary, 0o755)

    report = grade(args.binary, task_dir, oracle_bin=args.oracle)
    print(f"\n{'='*60}")
    print(f"Score: {report['score']}% ({report['passed_cases']}/{report['total_cases']} cases)")
    print(f"Weighted: {report['passed_weight']}/{report['total_weight']}")
    print(f"{'='*60}")

    if report["failed_cases"] > 0:
        print(f"\nFailed cases:")
        for r in report["results"]:
            if not r["passed"]:
                print(f"  [{r['weight']}] {r['case_id']}: {r['reason']}")

    report_path = os.path.join(task_dir, "candidate", "grade_report.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report: {report_path}")
    print(f"\nSCORE:{report['score']}")

    return report


if __name__ == "__main__":
    main()
