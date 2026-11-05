Throughout implementation, continuously review for the following issues:
...

---

Throughout implementation, continuously review the existing design and implementation in light of the findings and solutions in `<reference>`, focusing on issues including, but not limited to:
...

For each confirmed issue, adapt and port the relevant solution from `<reference>`, replacing the existing design or implementation rather than adding workarounds, up to a complete rewrite if warranted. Preserve required behavior and invariants, validate changes, and avoid unnecessary complexity.

---

Conduct a comprehensive, in-depth, and adversarial review of all uncommitted changes, covering areas including, but not limited to:
- scalability, extensibility, and maintainability, including algorithmic bottlenecks, unbounded work, lock contention, and architectural constraints
- over-engineering, unnecessary indirection, redundant or excessive validation, unjustified constraints or blocking, and overly defensive design
- avoidable performance and resource overhead, including cloning, allocation, I/O, IPC round trips, thread or task spawning, latency, and CPU cost
- unnecessary complexity, including unreachable defensive paths, duplicate logic, and convoluted control flow
- dead code, duplication, and maintenance burden
- UI/UX and design quality, including clarity, consistency, accessibility, responsiveness, and cognitive load; flag anything that does not clarify intent, enable action, or add meaningful value

---

Conduct a comprehensive, in-depth, and adversarial audit of `<repo>`'s architecture, codebase, UI/UX and design, and test suite, and provide prioritized MUST and SHOULD recommendations, covering areas including, but not limited to:
...

---

Conduct a comprehensive, in-depth, and adversarial simplification audit of `<scope>`:
- find unused or support-only surfaces, abandoned-feature residue, duplication, speculative generality, pass-through abstractions, needless nesting, redundant lifecycle machinery, misplaced defenses, and hand-rolled infrastructure replaceable by simpler established mechanisms.
- reject changes that merely move complexity, reduce clarity, or alter required behavior
- preserve trust-boundary validation, security and accessibility controls, data safety, resource cleanup, and intentional seams

---

read `<file>`. verify and fix substantive issues; selectively adopt improvement suggestions based on their expected benefit

---

resolve CI failures and conduct a comprehensive, in-depth, and adversarial audit of the CI workflows, focusing on simplicity, efficiency, and eliminating redundancy and unnecessary complexity
