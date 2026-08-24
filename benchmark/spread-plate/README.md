# ArtifactsBench Spread Plate Experiment

This experiment adapts public ArtifactsBench task 231 into a harness comparison that can be run repeatedly in Agent Canvas.

The original task asks an agent to create an interactive virtual biology experiment. The local version preserves that open-ended design problem and adds a narrow behavior contract for deterministic checking. Visual and educational quality remain a separate human or vision-model rubric.

The runner creates an empty Git repository for each harness, sends `task.md`, and verifies the result with `verify_spread_plate.py`. Generated application workspaces are stored under the workshop `runs` directory.

Run one comparison from the harness-suite directory:

```bash
python run_suite.py \
  --run-id 20260822-artifactsbench-spread-plate-v1 \
  --task artifactsbench-spread-plate \
  --include-codex
```

Install Playwright and its Chromium browser before running the verifier. The EC2 benchmark host keeps this dependency outside the generated workspaces.
