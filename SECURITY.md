# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.**

Instead, please report them privately using one of the following channels:

1. **GitHub Private Vulnerability Reporting (preferred):**
   Go to the [Security tab](https://github.com/cypr0/k8s-ops/security) of this repository and click on
   **"Report a vulnerability"**.
2. **Direct contact:** If you cannot use GitHub's reporting feature, contact the maintainer
   [@cypr0](https://github.com/cypr0) via GitHub and ask for a private way to share the details.

### What to include

To help me triage your report quickly, please include as much of the following as possible:

- A description of the vulnerability and its potential impact
- The affected version(s), component(s), or file(s)
- Steps to reproduce, or a proof of concept
- Relevant configuration details (e.g. Kubernetes version, manifests, Helm values)
- Any suggested fix or mitigation, if available

### What to expect

| Step                         | Timeframe                                   |
| ---------------------------- | ------------------------------------------- |
| Acknowledgement of report    | within **3 business days**                  |
| Initial assessment / triage  | within **7 days**                           |
| Status updates               | at least every **14 days** until resolution |
| Fix or mitigation (accepted) | depends on severity, targeted within **90 days** |

### If the vulnerability is accepted

- I will confirm the issue and determine its severity and affected versions.
- I will work on a fix and keep you informed about the progress.
- A patched release and a security advisory (including a CVE where applicable) will be published.
- With your consent, I will credit you in the advisory and release notes.
- I kindly ask you to keep the details confidential until a fix has been released (coordinated disclosure).

### If the vulnerability is declined

- I will explain why the report is not considered a vulnerability (e.g. not reproducible,
  intended behavior, or out of scope).
- You are welcome to provide additional information, and I will gladly re-evaluate the report.

Thank you for helping to keep k8s-ops safe!
