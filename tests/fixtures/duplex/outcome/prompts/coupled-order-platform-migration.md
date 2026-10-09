Prepare an implementation-ready migration package for the order-event platform in `/workspace/project`.

The package will be handed to separate engineering and operations teams, so it must be internally consistent and specific enough to implement without another architecture meeting. You may create and coordinate a team if useful. Decide the team structure, responsibilities and communication pattern yourself. Parallelize useful work, but keep one coherent final decision across every deliverable.

Read these inputs before finalizing the package:

- `/workspace/project/inputs/current-system.md`
- `/workspace/project/inputs/target-slo.md`
- `/workspace/project/inputs/security-and-operations.md`
- the target-environment inventory available through the environment inspection tools

The current-system document describes what exists today. It is evidence, not permission to assume that the same infrastructure is available in the target environment. If the inputs conflict or later evidence invalidates work already assigned, reconcile the conflict and update every affected deliverable. Preserve useful work that does not depend on the invalidated assumption.

Create exactly these submitted files:

1. `/workspace/project/output/architecture.json`
   - `backend`
   - `decision_reasons`
   - `rejected_alternatives`
   - `delivery_guarantee`
   - `ordering_scope`
2. `/workspace/project/output/producer.json`
   - `backend`
   - `connection`
   - `serialization`
   - `idempotency`
   - `retry_policy`
3. `/workspace/project/output/consumer.json`
   - `backend`
   - `connection`
   - `consumer_model`
   - `checkpointing`
   - `failure_recovery`
4. `/workspace/project/output/deployment.json`
   - `backend`
   - `required_services`
   - `network_dependencies`
   - `health_checks`
   - `rollback`
5. `/workspace/project/output/validation.json`
   - `cross_file_backend_consistent`
   - `target_environment_supported`
   - `slo_covered`
   - `security_constraints_covered`
   - `evidence`

Acceptance requirements:

- All four design files must select the same backend.
- Connections, service names, health checks and rollback steps must exist in the inspected target environment; do not invent infrastructure.
- The design must meet the throughput, ordering, recovery and security requirements in the supplied inputs.
- Producer and consumer behavior must match the delivery guarantee and ordering scope in `architecture.json`.
- `deployment.json` must contain every service required by producer and consumer configurations and no unavailable service.
- `validation.json` must cite the source file or inspected environment evidence used for each conclusion.
- Do not hide contradictory findings. Resolve them in the submitted design and record rejected alternatives.
- Do not mark validation successful until the submitted files have been checked together after the last relevant finding.

Use tools to inspect the environment and write the files. Findings that affect another active workstream should be communicated promptly with concrete evidence. The final response should summarize the selected architecture, the evidence that determined it, and the validation performed.
