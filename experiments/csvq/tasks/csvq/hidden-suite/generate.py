#!/usr/bin/env python3
"""Generate the hidden-suite cases.json for csvq.

This defines the test cases (args + stdin + weight) WITHOUT expected outputs.
Expected outputs are generated at grade time by running the oracle binary,
so they are never stored in the repository the agent can see.
"""
import json
import os

TASK_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(TASK_DIR, "..", "fixtures")

PEOPLE = os.path.abspath(os.path.join(FIXTURES, "people.csv"))
DEPTS = os.path.abspath(os.path.join(FIXTURES, "departments.csv"))
PRODUCTS = os.path.abspath(os.path.join(FIXTURES, "products.csv"))
EDGE = os.path.abspath(os.path.join(FIXTURES, "edgecases.csv"))

# At generation time on macOS the paths are absolute. At grade time inside the
# sandbox, the evaluator passes fixture paths relative to the task dir, so we
# store paths relative to the fixtures dir and the evaluator resolves them.
def rel(path):
    return os.path.relpath(path, FIXTURES)

# (case_id, weight, args, stdin_label)
# stdin_label: None, "people", or "empty"
# args use "$FIXTURE/<name>" as a placeholder the evaluator substitutes
CASES = [
    # --- select ---
    ("select-basic-2col", 1, ["select", "name,age", "$FIXTURE/people.csv"], None),
    ("select-basic-3col", 1, ["select", "name,age,city", "$FIXTURE/people.csv"], None),
    ("select-reorder", 2, ["select", "city,name", "$FIXTURE/people.csv"], None),
    ("select-single", 1, ["select", "name", "$FIXTURE/people.csv"], None),
    ("select-all", 1, ["select", "name,age,city,salary", "$FIXTURE/people.csv"], None),
    ("select-stdin", 1, ["select", "name,city"], "people"),
    ("select-case-insensitive", 2, ["select", "NAME,AGE", "$FIXTURE/people.csv"], None),
    ("select-nonexistent", 3, ["select", "foobar", "$FIXTURE/people.csv"], None),
    ("select-products", 1, ["select", "product,price", "$FIXTURE/products.csv"], None),
    ("select-edge-quoted", 3, ["select", "name,description", "$FIXTURE/edgecases.csv"], None),

    # --- filter ---
    ("filter-num-gt", 1, ["filter", "age", ">", "28", "$FIXTURE/people.csv"], None),
    ("filter-num-lt", 1, ["filter", "age", "<", "30", "$FIXTURE/people.csv"], None),
    ("filter-num-eq", 1, ["filter", "age", "=", "30", "$FIXTURE/people.csv"], None),
    ("filter-num-ge", 2, ["filter", "age", ">=", "30", "$FIXTURE/people.csv"], None),
    ("filter-num-le", 2, ["filter", "age", "<=", "30", "$FIXTURE/people.csv"], None),
    ("filter-num-ne", 2, ["filter", "age", "!=", "30", "$FIXTURE/people.csv"], None),
    ("filter-str-eq", 1, ["filter", "city", "=", "NYC", "$FIXTURE/people.csv"], None),
    ("filter-str-ne", 2, ["filter", "city", "!=", "NYC", "$FIXTURE/people.csv"], None),
    ("filter-contains", 2, ["filter", "city", "contains", "an", "$FIXTURE/people.csv"], None),
    ("filter-tilde", 2, ["filter", "city", "~", "an", "$FIXTURE/people.csv"], None),
    ("filter-str-gt", 3, ["filter", "name", ">", "D", "$FIXTURE/people.csv"], None),
    ("filter-stdin", 1, ["filter", "age", ">", "28"], "people"),
    ("filter-no-match", 2, ["filter", "age", ">", "1000", "$FIXTURE/people.csv"], None),
    ("filter-products", 1, ["filter", "category", "=", "Hardware", "$FIXTURE/products.csv"], None),
    ("filter-products-price", 2, ["filter", "price", ">=", "19.99", "$FIXTURE/products.csv"], None),
    ("filter-edge-quoted", 3, ["filter", "name", "contains", "Deluxe", "$FIXTURE/edgecases.csv"], None),

    # --- sort ---
    ("sort-name", 1, ["sort", "name", "$FIXTURE/people.csv"], None),
    ("sort-age", 1, ["sort", "age", "$FIXTURE/people.csv"], None),
    ("sort-age-reverse", 2, ["sort", "age", "--reverse", "$FIXTURE/people.csv"], None),
    ("sort-age-reverse-short", 2, ["sort", "age", "-r", "$FIXTURE/people.csv"], None),
    ("sort-salary", 1, ["sort", "salary", "$FIXTURE/people.csv"], None),
    ("sort-city", 1, ["sort", "city", "$FIXTURE/people.csv"], None),
    ("sort-stdin", 1, ["sort", "age"], "people"),
    ("sort-products-price", 2, ["sort", "price", "$FIXTURE/products.csv"], None),
    ("sort-products-price-rev", 2, ["sort", "price", "--reverse", "$FIXTURE/products.csv"], None),

    # --- stats ---
    ("stats-age", 1, ["stats", "age", "$FIXTURE/people.csv"], None),
    ("stats-salary", 1, ["stats", "salary", "$FIXTURE/people.csv"], None),
    ("stats-price", 1, ["stats", "price", "$FIXTURE/products.csv"], None),
    ("stats-quantity", 1, ["stats", "quantity", "$FIXTURE/products.csv"], None),
    ("stats-stdin", 1, ["stats", "age"], "people"),
    ("stats-nonexistent", 3, ["stats", "foobar", "$FIXTURE/people.csv"], None),

    # --- join ---
    ("join-basic", 1, ["join", "name", "manager", "$FIXTURE/people.csv", "$FIXTURE/departments.csv"], None),
    ("join-reverse", 2, ["join", "manager", "name", "$FIXTURE/departments.csv", "$FIXTURE/people.csv"], None),
    ("join-no-match", 3, ["join", "city", "dept", "$FIXTURE/people.csv", "$FIXTURE/departments.csv"], None),

    # --- edge cases ---
    ("edge-empty-input", 3, ["select", "name", "$FIXTURE/empty.csv"], "empty"),
    ("edge-no-args", 3, [], None),
    ("edge-help", 1, ["--help"], None),
    ("edge-unknown-cmd", 3, ["foobar"], None),
    ("edge-missing-file", 3, ["select", "name", "/nonexistent/file.csv"], None),
]

# empty.csv is a zero-byte file
open(os.path.join(FIXTURES, "empty.csv"), "w").close()

cases = []
for case_id, weight, args, stdin_label in CASES:
    cases.append({
        "id": case_id,
        "weight": weight,
        "args": args,
        "stdin": stdin_label,
    })

manifest = {
    "total_cases": len(cases),
    "comparison_rules": {
        "channels": ["exit_code", "stdout"],
        "stdout_normalization": "strip trailing whitespace from each line",
        "stderr": "not compared (error messages may differ)",
    },
    "cases": cases,
}

out_path = os.path.join(TASK_DIR, "cases.json")
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Generated {len(cases)} cases -> {out_path}")
print(f"Total weight: {sum(c['weight'] for c in cases)}")

