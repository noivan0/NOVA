# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in NOVA, please report it via GitHub Security Advisories:

1. Go to [https://github.com/noivan0/NOVA/security/advisories/new](https://github.com/noivan0/NOVA/security/advisories/new)
2. Describe the vulnerability and its potential impact
3. We will respond within 72 hours

Please do **not** open a public issue for security vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.9.x   | Yes       |
| < 1.9   | No — see Fixed Vulnerabilities below |

## Fixed Vulnerabilities

### v1.9.0 (2026-08-28) — Shell command injection via LLM phase output (High)

`Orchestrator._exec_shell()` interpolated harness `context` values —
including the raw output of a previous `executor: llm` phase — directly
into shell command strings before running them with `shell=True`. If an
LLM's output contained shell metacharacters (via prompt injection in
content it processed), it could break out of its intended string context
and execute arbitrary commands. Fixed by removing `{{key}}` string
interpolation from shell command assembly entirely; context values are now
passed exclusively via `NOVA_CTX_<KEY>` environment variables, which the
shell cannot re-parse. See `CHANGELOG.md` v1.9.0 for the full writeup,
including the specific injection payloads reproduced and confirmed
blocked. If you run harnesses whose `executor: shell` phases relied on
`{{key}}` templates in the `command` field (no built-in harness did),
update them to read `$NOVA_CTX_<KEY>` instead.

## Known History Notes

Git history prior to v1.3.0 (commits before `35db9d8`) contains references to internal
development filesystem paths in documentation files that were added during early development.
These are **filesystem paths only** — no credentials, API keys, tokens, or sensitive data are present.

The current HEAD (`main` branch, v1.3.0+) contains none of these internal paths.
All tracked files have been verified clean.

## Secrets Handling

NOVA never stores secrets in code. All credentials are passed via environment variables:

| Secret | Variable |
|--------|----------|
| LLM API key | `NOVA_LLM_API_KEY` |
| Notifier token (Telegram) | `NOVA_NOTIFIER_TOKEN` |
| Publisher credentials | `NOVA_PUBLISHER_API_KEY` |

See `.env.example` for the full list.

## Dependencies

NOVA's core has a single required dependency (`pyyaml`). All LLM SDK dependencies are
optional extras. Keep optional dependencies updated:

```bash
pip install -e ".[all]" --upgrade
```
