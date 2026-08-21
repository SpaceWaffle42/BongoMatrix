# GitLab-tracked Hermes skills

This directory contains durable project-owned Hermes skill packages. GitLab is the source of truth; do not maintain copied skill files under `~/.hermes/skills`.

Configure Hermes to discover this checkout as an external, read-only skill source:

```bash
hermes config set skills.external_dirs /absolute/path/to/checkout/skills
```

For this repository, verify discovery in a fresh session with:

```bash
hermes skills list
```

Local Hermes configuration is machine/runtime state and is not copied into the repository. Every skill body, reference, template, script, and test must be committed through the normal branch and merge-request workflow. Credentials, tokens, caches, generated output, and mutable runtime state must not be committed.

No public-use license is asserted by individual skill metadata; repository licensing remains a project-level decision.
