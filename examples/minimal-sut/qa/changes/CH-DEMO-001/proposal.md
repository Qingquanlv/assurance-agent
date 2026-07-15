# greeting — QA Proposal (example seed)

> Minimal example intake seed for the deterministic `aa` workflow. The driver's
> full scope starts at `explore`; there is no interactive intake phase, so the
> requirement is materialized here for the workflow to read.

## Requirement

The backend exposes `GET /api/greeting?name=<name>` returning
`{"message": "hello <name>"}` with status 200. Verify the happy path and that a
missing `name` query parameter returns 422.

## Test Types Considered
- API: selected
- E2E: declined (example scope)
- Fuzz: declined (example scope)
- Performance: declined (example scope)

## Layer Rationale
Minimal example — API coverage only for the greeting endpoint.

generation_mode: autonomous
