## Summary

<!-- 1-3 sentences. What does this PR do and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor (no behavior change)
- [ ] Documentation
- [ ] CI / tooling

## Checklist

- [ ] Commits are [Signed-off-by](https://developercertificate.org/) (DCO) — `git commit -s`
- [ ] [Conventional Commit](https://www.conventionalcommits.org/) prefix in commit and PR title
- [ ] `ruff format` runs clean
- [ ] `poetry run pytest` passes
- [ ] Respects the `--workers 1` invariant (no assumption of multiple processes for background tasks)
- [ ] If this changes the manager↔greffer contract, the corresponding [manager](https://github.com/greffon/manager) PR is linked below

## Notes for reviewer

<!-- Anything tricky, anything not obvious from the diff. -->

## Related

<!-- Closes #N · manager PR: greffon/manager#N -->
