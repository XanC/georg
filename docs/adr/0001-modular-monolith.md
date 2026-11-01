# 0001. Modular monolith with SQLite and a built-in job queue

**Status:** Accepted

## Context

Georg serves small congregations that typically process one recording per week. It must be cheap to host, easy to deploy, and still keep inputs, processors, and outputs interchangeable.

## Decision

Georg is a single Python application shipped as one container image, with two process roles (web and worker) from the same codebase. State lives in SQLite (WAL mode) on local disk; background work uses a job table in that database rather than a message broker. Modules are ordinary Python packages implementing typed interfaces in `georg.core.contracts`; only the core touches persistence.

## Consequences

- One deployment unit, no broker or separate services to operate.
- SQLite must live on local block storage, not a network filesystem; this rules out Fargate/Lambda-style hosting for now.
- If load ever demands it, the job queue and database can be replaced behind the core without changing module contracts.
