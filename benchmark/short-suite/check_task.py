from __future__ import annotations

import argparse
import ast
import importlib
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_pytest(repo: Path, *args: str, repeat: int = 1) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    command = [sys.executable, "-m", "pytest", "-q", *args]
    for _ in range(repeat):
        result = subprocess.run(command, cwd=repo, env=env, text=True)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


def assert_docstring(repo: Path, module_path: str, function_names: set[str]) -> None:
    tree = ast.parse(read(repo / module_path))
    found = {
        node.name: ast.get_docstring(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    }
    missing = sorted(name for name in function_names if not found.get(name))
    if missing:
        raise AssertionError(f"Missing docstrings: {', '.join(missing)}")


def max_function_lines(path: Path) -> int:
    tree = ast.parse(read(path))
    lengths: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            lengths.append(end - node.lineno + 1)
    return max(lengths, default=0)


def assert_contains_all(path: Path, words: list[str]) -> None:
    content = read(path).lower()
    missing = [word for word in words if word.lower() not in content]
    if missing:
        raise AssertionError(f"{path.name} missing required terms: {', '.join(missing)}")


def assert_heading(path: Path, heading: str) -> None:
    content = read(path).lower()
    if f"## {heading.lower()}" not in content and f"### {heading.lower()}" not in content:
        raise AssertionError(f"{path.name} missing heading: {heading}")


def assert_minimum_word_count(path: Path, minimum: int) -> None:
    count = len(read(path).split())
    if count < minimum:
        raise AssertionError(f"{path.name} is too short: {count} words, expected at least {minimum}")


def import_from_repo(repo: Path, module_name: str):
    for loaded in list(sys.modules):
        if loaded == "toyapp" or loaded.startswith("toyapp."):
            sys.modules.pop(loaded, None)
    repo_text = str(repo)
    sys.path.insert(0, repo_text)
    try:
        return importlib.import_module(module_name)
    finally:
        try:
            sys.path.remove(repo_text)
        except ValueError:
            pass


class ManualClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeAPIClient:
    def __init__(self, user_type, delay: float = 0.0) -> None:
        self.user_type = user_type
        self.delay = delay
        self.lock = threading.Lock()
        self.user_calls = 0
        self.summary_calls = 0
        self.fail_user = False
        self.fail_summary = False
        self.plan = "free"
        self.features = ["tickets", "reports"]

    def fetch_user(self, user_id: str):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.user_calls += 1
            call_number = self.user_calls
            fail = self.fail_user
            plan = self.plan
        if fail:
            raise RuntimeError("user backend unavailable")
        return self.user_type(
            user_id=user_id,
            email=f"{user_id}-{call_number}@example.com",
            plan=plan,
        )

    def fetch_account_summary(self, user_id: str) -> dict[str, object]:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.summary_calls += 1
            fail = self.fail_summary
            features = list(self.features)
        if fail:
            raise RuntimeError("summary backend unavailable")
        user = self.fetch_user(user_id)
        return {
            "user_id": user.user_id,
            "email": user.email,
            "plan": user.plan,
            "features": features,
        }


def cache_metrics(cache) -> dict[str, object]:
    if not callable(getattr(cache, "get_metrics", None)):
        raise AssertionError("CachingAPIClient should expose get_metrics()")
    metrics = cache.get_metrics()
    if not isinstance(metrics, dict):
        raise AssertionError("get_metrics() should return a dict")
    return metrics


def metric_number(metrics: dict[str, object], name: str) -> float:
    if name not in metrics:
        raise AssertionError(f"metrics missing {name}")
    value = metrics[name]
    if isinstance(value, dict):
        value = sum(item for item in value.values() if isinstance(item, (int, float)))
    if not isinstance(value, (int, float)):
        raise AssertionError(f"metric {name} should be numeric or a numeric dict")
    return float(value)


def check_caching_layer(repo: Path) -> None:
    if not (repo / "toyapp/cache.py").exists():
        raise AssertionError("toyapp/cache.py was not created")

    api_module = import_from_repo(repo, "toyapp.api")
    cache_module = import_from_repo(repo, "toyapp.cache")
    client_type = getattr(cache_module, "CachingAPIClient", None)
    if client_type is None:
        raise AssertionError("toyapp.cache.CachingAPIClient is missing")

    try:
        default_cache = client_type()
    except TypeError as exc:
        raise AssertionError("CachingAPIClient() should create a default APIClient wrapper") from exc
    if not callable(getattr(default_cache, "fetch_user", None)):
        raise AssertionError("CachingAPIClient should preserve fetch_user(user_id)")
    if not callable(getattr(default_cache, "fetch_account_summary", None)):
        raise AssertionError("CachingAPIClient should preserve fetch_account_summary(user_id)")

    clock = ManualClock()
    fake = FakeAPIClient(api_module.User)
    cache = client_type(
        fake,
        clock=clock,
        user_ttl=10.0,
        account_summary_ttl=5.0,
    )

    first = cache.fetch_user("ada")
    second = cache.fetch_user("ada")
    if first != second or fake.user_calls != 1:
        raise AssertionError("fetch_user should use a read-through cache before TTL expiry")

    clock.advance(11.0)
    fake.plan = "pro"
    refreshed = cache.fetch_user("ada")
    if refreshed.plan != "pro" or fake.user_calls != 2:
        raise AssertionError("fetch_user should refresh after user_ttl expires")

    clock.advance(11.0)
    fake.fail_user = True
    stale = cache.fetch_user("ada")
    if stale != refreshed:
        raise AssertionError("fetch_user should fail open with stale cached data when refresh fails")
    fake.fail_user = False
    fake.plan = "enterprise"
    retried_after_stale = cache.fetch_user("ada")
    if retried_after_stale.plan != "enterprise" or fake.user_calls != 4:
        raise AssertionError("failed user refresh should not mark stale data fresh")

    summary = cache.fetch_account_summary("ada")
    again = cache.fetch_account_summary("ada")
    if summary != again or fake.summary_calls != 1:
        raise AssertionError("fetch_account_summary should use a read-through cache before TTL expiry")

    clock.advance(6.0)
    fake.features = ["tickets", "reports", "billing"]
    refreshed_summary = cache.fetch_account_summary("ada")
    if "billing" not in refreshed_summary["features"] or fake.summary_calls != 2:
        raise AssertionError("fetch_account_summary should refresh after account_summary_ttl expires")

    fake.features = ["tickets"]
    cache.invalidate_user("ada")
    invalidated_summary = cache.fetch_account_summary("ada")
    if invalidated_summary["features"] != ["tickets"] or fake.summary_calls != 3:
        raise AssertionError("invalidate_user should also invalidate the dependent account summary")

    cache.invalidate_account_summary("ada")
    fake.features = ["tickets", "exports"]
    after_direct_invalidation = cache.fetch_account_summary("ada")
    if after_direct_invalidation["features"] != ["tickets", "exports"] or fake.summary_calls != 4:
        raise AssertionError("invalidate_account_summary should invalidate only the summary cache entry")

    same_key_fake = FakeAPIClient(api_module.User, delay=0.05)
    same_key_cache = client_type(same_key_fake, clock=ManualClock(), user_ttl=10.0, account_summary_ttl=5.0)
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as executor:
        users = list(executor.map(same_key_cache.fetch_user, ["sam"] * 8))
    same_key_elapsed = time.perf_counter() - start
    if len({user.email for user in users}) != 1 or same_key_fake.user_calls != 1:
        raise AssertionError("concurrent fetch_user calls for one key should be coalesced into one backend call")
    if same_key_elapsed > 0.16:
        raise AssertionError("same-key coalescing took too long")
    same_key_misses = metric_number(cache_metrics(same_key_cache), "cache_miss_total")
    if same_key_misses != 1:
        raise AssertionError("cache_miss_total should count one backend fill for coalesced same-key callers")

    multi_key_fake = FakeAPIClient(api_module.User, delay=0.05)
    multi_key_cache = client_type(multi_key_fake, clock=ManualClock(), user_ttl=10.0, account_summary_ttl=5.0)
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(multi_key_cache.fetch_user, ["u1", "u2", "u3", "u4"]))
    multi_key_elapsed = time.perf_counter() - start
    if multi_key_fake.user_calls != 4:
        raise AssertionError("different user keys should each call the backend once")
    if multi_key_elapsed > 0.16:
        raise AssertionError("different keys should not be serialized behind one global backend lock")
    multi_key_misses = metric_number(cache_metrics(multi_key_cache), "cache_miss_total")
    if multi_key_misses != 4:
        raise AssertionError("cache_miss_total should count one backend fill per distinct missed key")

    failed_fill_fake = FakeAPIClient(api_module.User, delay=0.05)
    failed_fill_fake.fail_user = True
    failed_fill_cache = client_type(failed_fill_fake, clock=ManualClock(), user_ttl=10.0, account_summary_ttl=5.0)

    def fetch_missing_user() -> str:
        try:
            failed_fill_cache.fetch_user("missing")
        except RuntimeError as exc:
            return str(exc)
        return "no error"

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        errors = list(executor.map(lambda _: fetch_missing_user(), range(4)))
    failed_fill_elapsed = time.perf_counter() - start
    if failed_fill_fake.user_calls != 1:
        raise AssertionError("same-key failed fills should be coalesced into one backend call")
    if any(error != "user backend unavailable" for error in errors):
        raise AssertionError("waiters on a failed fill should receive the backend error")
    if failed_fill_elapsed > 0.16:
        raise AssertionError("waiters on a failed fill should be released together")

    failed_fill_fake.fail_user = False
    retried = failed_fill_cache.fetch_user("missing")
    if retried.user_id != "missing" or failed_fill_fake.user_calls != 2:
        raise AssertionError("a failed fill should be cleared so a later retry can succeed")

    metrics = cache_metrics(cache)
    for name in [
        "cache_hit_total",
        "cache_miss_total",
        "cache_fill_seconds",
        "cache_invalidation_total",
    ]:
        metric_number(metrics, name)
    if metric_number(metrics, "cache_hit_total") < 2:
        raise AssertionError("cache_hit_total should count cache hits")
    if metric_number(metrics, "cache_miss_total") < 4:
        raise AssertionError("cache_miss_total should count cache misses")
    if metric_number(metrics, "cache_invalidation_total") < 2:
        raise AssertionError("cache_invalidation_total should count invalidations")


def check_task(repo: Path, task_id: str) -> None:
    if task_id == "p09-task-01":
        content = read(repo / "toyapp/pagination.py")
        if "raw_items" in content:
            raise AssertionError("raw_items is still present")
        if "source_items" not in content:
            raise AssertionError("source_items was not added")
        return

    if task_id == "p09-task-02":
        assert_docstring(
            repo,
            "toyapp/dates.py",
            {"parse_date", "parse_date_range", "normalize_timezone"},
        )
        return

    if task_id == "p09-task-03":
        run_pytest(repo, "tests/test_cli_assertion.py::test_format_greeting")
        return

    if task_id == "p09-task-04":
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo)
        result = subprocess.run(
            [sys.executable, "-m", "toyapp.cli", "--name", "Ada", "--verbose"],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        output = result.stdout.lower()
        if "verbose" not in output or "hello, ada." not in output:
            raise AssertionError("verbose CLI output did not include expected lines")
        return

    if task_id == "p09-task-05":
        tests = read(repo / "tests/test_dates.py")
        parsed = ast.parse(tests)
        test_names = [
            node.name
            for node in parsed.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        if len(test_names) < 5:
            raise AssertionError("tests/test_dates.py should contain at least 5 tests")
        lowered = tests.lower()
        for marker in ["invalid", "range", "timezone", "2024-02-29", "slash"]:
            if marker not in lowered:
                raise AssertionError(f"missing date test coverage marker: {marker}")
        run_pytest(repo, "tests/test_dates.py")
        return

    if task_id == "p09-task-06":
        run_pytest(repo, "tests/test_pagination.py")
        return

    if task_id == "p09-task-07":
        if max_function_lines(repo / "toyapp/reports.py") > 90:
            raise AssertionError("reports.py still has a function longer than 90 lines")
        helper_modules = list((repo / "toyapp").glob("report_*.py"))
        if not helper_modules:
            raise AssertionError("expected at least one toyapp/report_*.py helper module")
        run_pytest(repo, "tests/test_reports.py")
        return

    if task_id == "p09-task-08":
        run_pytest(repo, "tests/test_async_jobs.py", repeat=5)
        return

    if task_id == "p09-task-09":
        review = repo / "SECURITY_REVIEW.md"
        if not review.exists():
            raise AssertionError("SECURITY_REVIEW.md was not created")
        assert_contains_all(
            review,
            ["authorization", "bearer", "debug", "timing", "fix"],
        )
        return

    if task_id == "p09-task-10":
        check_caching_layer(repo)
        return

    raise AssertionError(f"unknown task id: {task_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"repo does not exist: {repo}")
    check_task(repo, args.task)
    print(f"{args.task}: PASS")


if __name__ == "__main__":
    main()
