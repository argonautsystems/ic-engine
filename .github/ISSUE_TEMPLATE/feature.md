---
name: Feature request
about: Suggest an enhancement to the v4.x containerized skill
labels: enhancement
---

## Use case

<!-- What are you trying to do? What problem does this solve for the
end user? -->

## Proposed approach

<!-- One or two paragraphs. Don't write the code yet — describe the
shape of the solution. -->

## Which layer?

This repo is the **runtime + skill** layer (Docker image, MCP-HTTP
bridge, dashboard, agent skill files). If your feature belongs in:

- **Engine logic** (analyzers, deterministic computation, narrator
  decision rules): file at
  https://github.com/argonautsystems/ic-engine/issues
- **Schema-map / normalize / vision-extract**: file at
  https://gitlab.com/argonautsystems/clio/-/issues
- **Per-runtime install / agent integration**: this repo, OK.
- **Bridge code (MCP server wrapper, REST endpoints, key-management
  API)**: this repo, OK.
- **Dashboard UX**: this repo, OK.
- **Skill metadata / ClawHub publishing**: this repo, OK.

## Alternatives considered

<!-- What did you rule out, and why? -->

## Impact on existing surfaces

- [ ] Would change the MCP tool catalog (breaking for agents)
- [ ] Would change the SKILL.md frontmatter (ClawHub schema impact)
- [ ] Would change the install path
- [ ] Would change the per-runtime config (agent-skills/**)
- [ ] Backward-compatible

## Additional context
