# Fresh Experiment acceptance cases

These fixtures are deliberately new acceptance scenarios, not aliases for the
historic fixture suite.  Each supplies a minimal upstream-style research plan and
frozen preregistration so Experiment can be exercised without spending turns in
Hypothesis.

Run each with a unique `--project-id`.  Cases 02, 04, 05, and 07 require real local
GPU/software/cluster resources.  Absence of a declared resource is an expected,
auditable blocked or infeasible outcome; it is not a skipped test and must never be
converted into a success by a CPU or synthetic substitute.
