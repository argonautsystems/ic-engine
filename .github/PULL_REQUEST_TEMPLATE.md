<!--
Thanks for contributing! Please fill out this template so reviewers
can land your change quickly.
-->

## Summary

<!-- 1-3 sentences: what does this PR change, and why? -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (requires major-version bump)
- [ ] Documentation
- [ ] Refactor / cleanup
- [ ] CI / tooling

## Layer

This repo is the runtime + skill layer. Confirm the change fits here:

- [ ] Bridge code (MCP server, REST endpoints, key-management)
- [ ] Dockerfile / image build
- [ ] Dashboard
- [ ] Skill metadata (`SKILL.md`, `compose.yml`, `install.yaml`,
  `agent-skills/**`)
- [ ] Documentation (top-level *.md, `docs/**`, `CHANGELOG.md`)
- [ ] Tests / harness
- [ ] **Belongs in `argonautsystems/ic-engine` instead** (close + redirect)

## License compatibility

- [ ] Substantive code changes — Apache 2.0–compatible? (LICENSE)
- [ ] Distribution-edge artifact changes — MIT-0 preserved?
  (LICENSE-MIT-0)
- [ ] No third-party code added without explicit license review

## Tests

- [ ] Bridge unit tests pass (where applicable)
- [ ] Cobol regression baseline holds (≥ 245/250 PASS) — required for
  engine-affecting changes
- [ ] Manual verification: describe what you tested and on which agent
  runtime

## ClawHub schema impact

- [ ] No change to `SKILL.md` frontmatter
- [ ] `SKILL.md` frontmatter changed — reviewed against ClawHub
  schema; bumped skill version

## Documentation

- [ ] `CHANGELOG.md` entry added under `## [Unreleased]`
- [ ] `README.md` / `SKILL.md` updated if user-facing surface changes
- [ ] Per-runtime `agent-skills/*/SKILL.md` updated if relevant

## Commit author identity

- [ ] Author email matches a personal OSS identity (no
  employer-affiliated address)
- [ ] Conventional Commit message format
  (`feat(bridge):`, `fix(skill):`, `docs:`, etc.)
