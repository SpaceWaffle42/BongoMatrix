# GitLab-backed artifact policy

Use this reference whenever token-efficient work creates files or durable knowledge.

## Must be tracked in GitLab

- skills and their references, templates, scripts, and tests;
- accepted product requirements and design decisions;
- reusable operational procedures;
- source, tests, schemas, migrations, and deployment definitions;
- sanitized context maps and contributor documentation;
- durable review reports explicitly requested by the user.

Publish through the repository’s normal feature-branch and merge-request workflow. Verify the remote commit and, where applicable, deployed-client parity.

## Must not be tracked

- private keys, tokens, cookies, passwords, refresh tokens, client secrets;
- mutable runtime configuration containing credentials or private user data;
- uploaded user media unless explicitly approved as bundled project assets;
- caches, virtual environments, bytecode, coverage, build output, locks, and PID files;
- temporary API responses or raw logs containing sensitive data;
- local session databases and agent memory stores.

“All files in GitLab” means every durable non-secret project/workflow artifact. It never means committing credentials or mutable runtime state.

## Temporary files

Avoid a temporary file when a bounded in-memory operation or tracked script can do the work. When a temporary file is necessary:

1. Create it in the operating system’s scratch area.
2. Give sensitive temporary material restrictive permissions.
3. Use it only as an intermediate artifact.
4. Delete it before completion.
5. If it contains reusable knowledge, sanitize and promote that knowledge into a reviewed tracked file first.

A temporary file must never be the only surviving copy of an accepted requirement, reusable procedure, or verification method.

## Context-map policy

A context map contains pointers, not duplicated content. Track:

- relative path;
- authority/ownership;
- when to read it;
- useful headings or symbols;
- whether the source is durable or live.

Do not track local absolute paths, internal hostnames, credential locations, account identifiers, or secret values in a public repository.

## Publication gate

Before merge:

- `git diff --check` passes;
- every changed file is intentional;
- skill frontmatter and scripts validate;
- secret/private-infrastructure scan passes;
- no generated cache or scratch file is staged;
- branch/MR commit is immutable and reviewed;
- GitLab contains all durable non-secret files;
- local activation points to the tracked package rather than a copied fork.
- retrieval helpers read Git object/index content rather than trusting that a tracked pathname still contains tracked bytes.
