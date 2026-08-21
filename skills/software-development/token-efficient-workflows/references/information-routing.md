# Information routing for token-efficient work

Use this reference when deciding whether to read a file, query live state, search session history, or rely on memory.

## Decision tree

1. **Did the user provide a direct source?**
   - Yes: inspect it first.
   - No: continue.
2. **Can the fact change outside this conversation?**
   - Yes: query the current file, API, repository, process, service, device, or date.
   - No: continue.
3. **Is exact wording, completeness, ordering, or schema important?**
   - Yes: read the authoritative tracked source.
   - No: continue.
4. **Was the fact established in another chat but not promoted to GitLab?**
   - Yes: search session history, confirm if ambiguous, then promote accepted durable content.
   - No: continue.
5. **Is it a stable user preference or environment convention?**
   - Yes: use compact persistent memory as an index.
   - No: ask only when tools cannot retrieve it.

## Source matrix

| Question | Cheapest reliable route |
|---|---|
| “What did we decide Spotify should display?” | Search the tracked roadmap heading, then read that section |
| “What is deployed now?” | Query Git `main`, production checksum/service/device; roadmap is not runtime proof |
| “Where did we leave the unfinished review?” | Session search because progress is transient |
| “Does the user prefer free APIs?” | Persistent user memory; verify tracked roadmap when implementing the project rule |
| “What does this function currently validate?” | Symbol search plus bounded source/test reads |
| “What was the exact test failure?” | Read bounded test output or rerun focused test; do not recall |
| “What time is it?” | Live system tool, never a file or memory |

## Read sizing

Use the smallest unit that preserves governing context:

1. Exact symbol plus nearby tests.
2. Markdown heading section.
3. Search match with 2–5 context lines.
4. File range expanded once if dependencies are unresolved.
5. Whole file only when structure-wide reasoning is necessary.

A short section is not enough if it references global acceptance criteria, schema invariants, or shared security rules. Load those specific sections too rather than the whole document.

## Retrieval economics

A file read is usually better than recall when it avoids:

- restating a long conversation;
- guessing changed values;
- a clarification round trip;
- implementing against obsolete structure;
- later review and rollback.

Memory is cheaper only for compact stable facts. Do not store source code, raw requirements, task logs, commit IDs, or provider payloads in memory.

## Output control

Before a tool call, set one or more bounds:

- file range or section heading;
- file glob;
- maximum matches;
- context lines;
- JSON fields;
- timeout and attempts;
- first/last relevant state transitions;
- anomaly-only output.

If the first bounded result is incomplete, widen deliberately. Do not start unbounded “just in case.”
