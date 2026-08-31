# Security Policy

## Reporting a vulnerability

restory is a security tool, so I take reports seriously — and some findings
shouldn't be public before they're understood.

**If you find a bypass or vulnerability that could put users at risk, please do
NOT open a public issue.** Instead, report it privately:

- Use GitHub's [private vulnerability reporting](https://github.com/DarkAxiom93/Restory/security/advisories/new) (Security → Report a vulnerability), or
- Email me directly at restory.security@gmail.com.

Please include the payload or command that wasn't caught, what effect it has, and
which agent you were using. I'll acknowledge as quickly as I can.

## Scope and expectations

restory is best-effort defense-in-depth, not a security boundary — static
command inspection can be bypassed by design (`bash -c`, scripts, encoded
commands). Known bypasses are documented in `scripts/selfcheck.py`. Reports that
extend the classifier's coverage or reveal a new class of bypass are very
welcome.

For a hard boundary, restory is meant to be used alongside an OS sandbox or
container — not as a replacement.