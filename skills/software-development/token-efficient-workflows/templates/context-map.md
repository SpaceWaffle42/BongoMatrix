# Agent context map

This file is an index. Keep authoritative content in the linked source files rather than duplicating it here.

| Topic | Authoritative relative path | Read when | Targeted retrieval hint |
|---|---|---|---|
| Product requirements | `docs/...` | Planning or validating behavior | Heading name |
| Architecture/schema | `...` | Changing interfaces or persisted data | Symbol/schema name |
| Tests | `tests/...` | Implementing or verifying behavior | Test class/function |
| Deployment | `deploy/...` | Packaging or releasing | Service/script name |
| Runtime/generated state | Not a tracked authority | Checking live behavior | Query the live system |
| Secrets | Protected storage; values never tracked | Authentication setup | Track variable names only |

## Rules

- Use repository-relative paths.
- Do not include secrets, private infrastructure, or mutable state values.
- Add a row only when it prevents repeated broad discovery.
- Remove stale pointers in the same merge request that moves their authority.
