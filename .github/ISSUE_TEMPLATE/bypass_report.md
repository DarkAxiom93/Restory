---
name: Bypass report
about: The classifier missed a dangerous command
title: '[bypass] '
labels: bypass
assignees: ''
---

Thanks for catching this — bypass reports are the most valuable kind. 🎯

**The command / payload that wasn't blocked**
The exact command restory let through, verbatim.

```
(command)
```

**What effect it has**
What this command actually does — e.g. reads a secret, deletes files outside the
repo, exfiltrates data to a remote host, nukes git history.

**Which agent**
- [ ] Claude Code
- [ ] Gemini CLI
- [ ] Other (please specify):

**Anything else**
Version (`restory --version`), OS, or context that helps us reproduce it.
