<!-- Keep pull requests reviewable: one logical change, ideally under 2,000 changed lines. Split larger
     work by track or theme (CLAUDE.md §3) and link the pieces. -->

## What and why

<!-- One paragraph. Name the SDD section this implements or amends (e.g. "SDD §9.3"), the PRD
     requirement (FR-xx) or the audit / issue it closes. -->

Closes #

## How it was verified

<!-- Paste the real output: the test names that failed first and now pass, the suite summary line,
     lint. For deployment or image changes: `make deploy-runtime-check`. -->

```
```

## Checklist

- [ ] Tests were written first and the named tests now pass; the touched component's full suite is green (QASuite §14).
- [ ] Behaviour matches the SDD; an SDD change is in its **own** commit citing the section.
- [ ] Docs that mention the behaviour are updated (README, docs/, CHANGELOG.rst for a `fix(collection)`).
- [ ] No secrets, customer data, real hostnames/IPs or vendor logos; `gitleaks dir -c .gitleaks-tree.toml .` is clean.
- [ ] Only files of my track are touched (CLAUDE.md §3), or the controller agreed.
- [ ] Commits follow Conventional Commits and are signed off (`git commit -s`).
- [ ] AI assistance is disclosed below (tool and what it was used for); a Jev gate result is pasted when one was run.

## AI assistance / review notes

<!-- e.g. "Claude Code drafted the tests and the fix; I reviewed every line." Paste the jev_gate verdict
     if the change went through the agent workflow. -->
