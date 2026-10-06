---
name: security-review-skill
description: Check a code change for concrete security regressions.
license: MIT
---

# Security code review

1. Identify the changed entry points, trust boundaries, and data flows.
2. Check whether untrusted input can reach a sensitive operation without validation or authorization.
3. Reproduce each likely issue with a focused case when feasible.
4. Report only evidence-backed findings, affected paths, impact, and the smallest safe fix.
