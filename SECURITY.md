# Security Policy

## Supported versions

Security fixes are maintained on the default branch and included in the next
production release. Only the latest deployed release is supported; please
reproduce reports against the latest default-branch revision before submitting
them.

| Version | Supported |
| --- | --- |
| Latest production release (`/release/`) | Yes |
| Older releases | No — upgrade to the latest release |
| Default branch (unreleased) | Yes, for pre-release verification |

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, or
pull request. Use GitHub's private vulnerability reporting form under the
repository's **Security** tab so maintainers can investigate without exposing
users or deployment details.

Include the affected component, reproduction steps, impact, and a minimal proof
of concept that does not contain credentials or personal data.

## Scope

Reports are welcome for the web application, worker pipeline, ingestion and
export paths, authentication and tenant isolation, deployment manifests, CI/CD
workflows, and infrastructure-as-code in this repository.

Reports that require access to credentials, private infrastructure, or social
engineering are out of scope. Findings must be reproducible against a supported
version.

## Response expectations

Maintainers will acknowledge receipt of a report, assess its severity and
exploitability, and coordinate remediation and disclosure through the private
advisory. Critical issues affecting production data or tenant isolation are
prioritized for immediate remediation.

For ordinary defects without security impact, use the public issue tracker.
