# Job #3181: Create parallel DAG branches when oversized plans split

**Date:** 2026-08-02

The diff refactors plan splitting from forcing all stories into a single serial chain to computing a minimal dependency DAG that allows independent branches to execute concurrently. Two new functions (`_split_dag_predecessors` and `_topo_order_multi`) compute direct predecessor edges between stories only when they share target files or declare dependencies; stories with disjoint files and no declared relationship remain independent and can claim jobs in parallel. The `_supersede_with_split` function now creates each split job depending on all immediate predecessors within its component (not just the previous story), and repoints downstream dependents to only the terminal (sink) jobs in the DAG rather than all children. A new cycle-detection fallback linearizes any declared `depends_on` loops for stability. Comprehensive tests validate forks, joins, multi-file overlaps, and cycle handling.
The diff refactors plan splitting from forcing all stories into a single serial chain to computing a minimal dependency DAG that allows independent branches to execute concurrently. Two new functions (`_split_dag_predecessors` and `_topo_order_multi`) compute direct predecessor edges between stories only when they share target files or declare dependencies; stories with disjoint files and no declared relationship remain independent and can claim jobs in parallel. The `_supersede_with_split` function now creates each split job depending on all immediate predecessors within its component (not just the previous story), and repoints downstream dependents to only the terminal (sink) jobs in the DAG rather than all children. A new cycle-detection fallback linearizes any declared `depends_on` loops for stability. Comprehensive tests validate forks, joins, multi-file overlaps, and cycle handling.

## Files touched
- hyqs/pipeline/stages/plan.py
- hyqs/pipeline/store.py
- tests/test_stages_plan_build.py
- tests/test_stages_plan_scope_gate.py
- tests/test_store.py
