---
name: token-efficient-workflows
description: "Use when a multi-step task risks excessive model context."
version: 1.0.0
author: Hermes Agent
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [tokens, context, workflow, gitlab, retrieval, efficiency]
    related_skills: [codebase-inspection, self-hosted-git-repository-management]
---

# Token-Efficient Workflows

## Overview

Minimize model tokens without sacrificing correctness. Route each fact to the cheapest authoritative source, retrieve only the necessary slice, process bulky data outside the model context, and keep every durable non-secret workflow artifact in GitLab.

Token efficiency is not “use fewer tools.” A short targeted file read is usually cheaper and more reliable than reconstructing exact state from conversation history. Conversely, rereading an entire repository or long roadmap when one section is needed wastes context. Optimize total context consumed across the task, including retries caused by stale or guessed information.

## When to use

Use this skill when:

- a task spans several files, tools, reviews, or deployment stages;
- exact project decisions already exist in tracked documentation;
- a long conversation risks repeated summaries or compaction loss;
- repository, test, session, or provider output could flood model context;
- the user explicitly asks to reduce token usage or cost;
- durable agent instructions must be shared through GitLab.

Do not use it to avoid a required authoritative lookup, verification, safety check, or user clarification. Never trade correctness for a smaller prompt.

## Core rule: route before retrieving

Classify information before choosing a source:

| Information needed | Preferred source | Avoid |
|---|---|---|
| Exact current code, configuration schema, tests, docs | Tracked file; search then targeted read | Recalling content from chat |
| Accepted durable product decisions | GitLab-tracked roadmap/design file | Reconstructing questionnaires from session history |
| Current branch, diff, process, service, device, date | Live tool query | Memory or tracked snapshots |
| A decision from a previous conversation not yet tracked | Session search, then promote the confirmed result to GitLab | Asking the user to repeat known history immediately |
| Stable user preference | Persistent memory | Project files containing personal profile data |
| Current task progress | Session todo list | Committing transient progress files |
| Secrets and OAuth tokens | Protected secret storage | GitLab, ordinary docs, config, logs, or memory |
| Large mechanical transformation | Script/tool with filtered output | Feeding raw input and every intermediate result to the model |

A direct source identifier wins. If the user names a file, URL, repository, service, or device, inspect that source before session history. Use memory as an index and preference layer, never as proof of current mutable state.

See `references/information-routing.md` for the full decision tree and examples.

## Tight-loop workflow

### 1. Define the artifact and completion boundary

Write a one-sentence internal target: artifact, authoritative source, allowed mutations, and verification. Use a todo list only for three or more meaningful stages. Do not narrate a long plan when tool calls can start immediately.

**Complete when:** the requested artifact and verification boundary are explicit.

### 2. Build a context budget

Before broad reading, identify:

- the likely source files or named source;
- the smallest sections needed;
- independent lookups that can run in parallel;
- bulky outputs that should be filtered by a script;
- facts that must be live rather than recalled.

Prefer one targeted search plus one range/section read over a full-file read. Prefer one batched parallel call over several independent rounds. Use `execute_code` when three or more tool calls need filtering, looping, aggregation, or conditional logic.

**Complete when:** each planned lookup has a reason and an output bound.

### 3. Search before reading

For unfamiliar or large files:

1. Search filenames or headings.
2. Search for the symbol, requirement, error, or decision marker.
3. Read only the surrounding lines or named Markdown section.
4. Expand the range only if the first slice is insufficient.

For tracked Markdown, `scripts/context_lookup.py` can print a bounded heading section or bounded query matches without loading the whole file:

```bash
python3 skills/software-development/token-efficient-workflows/scripts/context_lookup.py   docs/FUTURE_DEVELOPMENT.md --heading "Spotify Now Playing module" --max-lines 100

python3 skills/software-development/token-efficient-workflows/scripts/context_lookup.py   config_store.py --query "scrolling_text_duration" --context 3 --max-matches 8
```

`--query` is deliberately case-insensitive literal text rather than a regular expression, preventing regex denial of service from untrusted input. Use the native `search_files` and `read_file` tools when available; the script is for reproducible shell/cron/agent workflows. It accepts only regular Git-index entries, resolves literal filenames safely, reads the exact staged blob, rejects control-bearing/non-UTF-8 or oversized sources, escapes reflected values, and applies both line and character output caps. This prevents unstaged replacements, hardlink mutations, terminal-control injection, and single-line context floods.

**Complete when:** retrieved context is sufficient to act and contains no unrelated bulk.

### 4. Act from the source, not a duplicate summary

Patch the authoritative file directly. Do not create a second handoff document that restates existing requirements. Add an index or link when discoverability is the problem; do not duplicate the content.

Keep code comments focused on invariants that cannot be inferred from code. Keep commit/MR text concise and point to tests or tracked decisions instead of pasting large outputs.

**Complete when:** one source of truth owns every durable fact changed by the task.

### 5. Externalize mechanical work

Use code or command-line tools to:

- count, hash, sort, filter, deduplicate, or compare;
- inspect many files and return only anomalies;
- parse JSON/API responses and print only decision fields;
- run validation and report pass/fail plus bounded failure detail;
- poll with a limit and return only final state or material transitions.

Do not put raw secrets, entire logs, full dependency trees, or repetitive passing output into model context. Preserve full logs as GitLab artifacts only when they are durable, sanitized, and actually useful; otherwise keep them ephemeral and delete them after the task.

**Complete when:** the model sees decisions and anomalies rather than mechanical noise.

### 6. Verify with fresh evidence

After writing, verify the actual artifact—not the preceding explanation. Use focused checks first, then the project’s required full suite. Re-read only changed sections, inspect `git diff --check`, validate changed-file scope, and prove remote/deployed parity from hashes or commit ancestry.

Do not reread every source file after a focused documentation edit. Do not rerun broad tests when a narrower check is authoritative unless the repository policy requires the full suite.

**Complete when:** every user requirement has observed evidence and no unsupported success claim remains.

### 7. Publish durable artifacts through GitLab

All durable non-secret files created by this workflow must be tracked in the project’s GitLab repository through its normal branch/MR policy. This includes skills, references, templates, scripts, plans intended for reuse, and accepted product decisions.

Local-only files are allowed only when they are runtime state, protected credentials, generated caches, build/test output, or disposable scratch material. Scratch files must be deleted during cleanup and must never become the sole copy of useful knowledge. Secrets must remain outside GitLab even though other durable artifacts belong there.

See `references/gitlab-artifact-policy.md` for classification and publication checks.

**Complete when:** the GitLab merge contains every durable non-secret artifact and no secret/runtime state.

### 8. Leave a compact continuation point

At the end of a bounded stage, report only:

- what changed;
- verification result;
- immutable handle such as MR, commit, path, or checksum;
- genuine blocker or remaining step count.

Do not save completed-work logs to persistent memory. GitLab history and session history already preserve them. Promote reusable procedure to this skill or a reference rather than growing global memory.

**Complete when:** another session can continue by reading the GitLab artifact instead of reconstructing chat.

## Token-saving patterns

### File over recall

Read from a file when exact wording, current values, ordering, schema, or completeness matters. Recall is acceptable only for low-risk stable preferences already stored as memory or when the user asks for an informal conceptual answer.

### Index over duplicate

Maintain a compact context map containing pointers, ownership, and retrieval hints—not copies of requirements. Start from `templates/context-map.md`. Update it only when ownership paths change.

### Delta over snapshot

Review the diff, changed symbols, or changed sections instead of repeatedly loading whole files. Compare immutable commits for review and deployment.

### Anomalies over full output

Return failing tests, unresolved discussions, changed API fields, or mismatched hashes. Summarize passing bulk with exact counts and preserve a command or artifact handle for reproducibility.

### Bounded polling over narration

Poll inside a script with timeout/backoff and print only state changes or final status. Do not alternate model turns with repetitive “still pending” output.

### Parallel reads over round trips

Batch independent searches, reads, and status queries. Serialize only genuine dependencies.

### Progressive disclosure over giant skills

Keep universal rules in `SKILL.md`; put detailed branches in `references/`. Load a reference only when its branch applies.

## GitLab-backed context map

For a new project, copy `templates/context-map.md` into a tracked location such as `docs/AGENT_CONTEXT.md`, fill only verified pointers, and merge it. The map should identify:

- requirements authority;
- architecture/schema authority;
- tests and verification commands;
- deployment authority;
- generated/runtime directories that are not sources;
- where secrets are referenced without recording values.

Do not update the map for ordinary implementation details. The repository tree, symbol search, and Git history already cover those.

## Common pitfalls

1. **“Recall is free.”** Reconstructing exact mutable facts from conversation context can consume more tokens and cause rework. Read the authoritative slice.
2. **Whole-file reflex.** Reading 1,000 lines to answer a one-section question wastes context. Search headings/symbols first.
3. **Summary sediment.** Repeated handoff files drift from source. Keep pointers and one owner.
4. **Tool-call minimalism.** Avoiding a cheap lookup can produce an expensive wrong implementation. Optimize total task tokens, not call count.
5. **Raw-output flooding.** Filter API payloads, logs, and large test output before model ingestion.
6. **Stale tracked state.** GitLab is authoritative for durable artifacts, but live services and branches still require live checks.
7. **Secrets in “all files.”** The GitLab rule never overrides secret handling. Keep credentials in protected storage and commit only setup instructions or variable names.
8. **Untracked useful scratch.** If scratch contains reusable knowledge, promote and review it before cleanup; otherwise delete it.
9. **Over-compression.** A concise context slice that omits a governing invariant is false economy. Expand until the action is safe.
10. **Unverified savings.** Smaller output is not success if retries, regressions, or user corrections increase total usage.

## Verification checklist

- [ ] Direct source inspected before memory/session reconstruction
- [ ] Mutable facts obtained from live tools or current tracked files
- [ ] Search/heading discovery performed before large reads
- [ ] Read ranges, match counts, loops, and polling bounded
- [ ] Independent lookups batched
- [ ] Mechanical bulk filtered outside model context
- [ ] One durable source of truth per fact
- [ ] Every durable non-secret file tracked in GitLab
- [ ] No secrets, credentials, runtime state, or caches committed
- [ ] Scratch files deleted or promoted to tracked artifacts
- [ ] Focused and required full verification completed
- [ ] Final report contains evidence and handles, not repeated bulk
