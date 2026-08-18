# CLAUDE.md

`AGENTS.md` in this directory is the authoritative contributor instruction file for
this repository, and it governs Claude Code exactly as it governs every other agent.
Read it before changing anything. This file exists only because Claude Code loads
`CLAUDE.md` by name; it deliberately does not restate `AGENTS.md`, so that the two
cannot drift apart.

## GitHub account

Use the GitHub account `ks2002119` for every `gh` command and every authenticated
remote Git operation in this repository — fetch, push, pull request, workflow, and
repository administration.

Switch and verify in the **same shell invocation** as the remote commands
themselves, because some execution environments restore a different active account
between invocations:

```sh
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
```

An account without access to this private repository reports `Repository not found`
(git) or `Could not resolve to a Repository` (gh). Both read as though the
repository was renamed or deleted. Treat either as an authentication symptom first:
never conclude that a repository, branch, or pull request is missing until you have
switched accounts in the same shell invocation and retried.

See the `GitHub Account` section of `AGENTS.md` for the governing statement.
