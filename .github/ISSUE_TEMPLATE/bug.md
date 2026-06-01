---
name: Bug report
about: Report a defect in the v4.x containerized skill
labels: bug
---

## Description

<!-- What's wrong? Concrete observation, not speculation. -->

## Reproduction

1. ...
2. ...
3. ...

## Expected vs actual

**Expected:**

**Actual:**

## Environment

- Host OS:
- Docker version (`docker --version`):
- mnemos-ic-runtime version (commit / tag):
- ic-engine image tag (`docker inspect ic-engine | grep Image`):
- Agent runtime + version (Claude Code 2.x, openclaw, zeroclaw, hermes):
- Portfolio size (approximate symbol count):

## Logs / output

```
docker compose logs ic-engine | tail -50
curl -sS http://127.0.0.1:18090/healthz
```

<!-- Paste relevant output. Scrub API keys, account numbers. -->

## Have you tried?

- [ ] `docker compose down -v && docker compose up -d` (reset cache + state)
- [ ] `mkdir -p portfolios` before `docker compose up -d` (the
  bind-mount UID quirk — see SKILL.md install snippet)
- [ ] Verified `init_state` reaches `ready` via
  `curl http://127.0.0.1:18090/api/portfolio/initialize/status`

## Security-sensitive?

If this is a security report, **do not file here**. Email
&lt;jperlow@gmail.com&gt; per [SECURITY.md](../../SECURITY.md).
