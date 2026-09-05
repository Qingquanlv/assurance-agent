# ACG initial-selection paired evaluation

Status: preregistered; provider-backed experiment not yet executed.

The experiment compares `select` with `all_admissible`. Each pair starts from
the same clean SUT revision and reuses the same committed Explore document,
quality-goal baseline, policy, budgets, model, tools, and environment. The two
arms use distinct invocations and workspaces. The baseline build differs only
in the pure family chooser and validates its own frozen plan.

The representative workload must include API-only, API plus E2E, and a policy
that requires fuzz or performance coverage. Run at least five pairs per
workload. Record the complete plan and reference, terminal outcome, Inspect
metrics, family conflicts, coverage and healing rounds, plus first-round and
total token, tool, duration, and cost values. Missing cost values remain
unknown.

The selection arm qualifies for a later default-enablement decision only if it
preserves every required obligation, has no worse achieved rate, introduces no
new family conflict, and lowers median first-round cost by at least 10% without
raising median total cost. Any blocked run is reported separately and cannot be
counted as a cost saving. These thresholds are evaluation criteria, not product
behavior or a claimed result.

Use `acg_comparison.py` on the collected JSON rows. The current implementation
and deterministic tests establish comparability checks and reporting only;
they do not provide evidence that initial selection reduces real provider cost.
