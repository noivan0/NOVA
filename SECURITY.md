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
| 1.3.x   | Yes       |
| < 1.3   | No        |

## Known History Notes

Git history prior to v1.3.0 (commits before `35db9d8`) contains references to internal
development paths (e.g. `$NOVA_HOME`) in documentation files that
were added during early development. These are **filesystem paths only** — no credentials,
API keys, tokens, or sensitive data are present.

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
