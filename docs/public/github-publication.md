# Preparing a public GitHub repository

1. Run `python scripts/audit_repository.py` and `python scripts/audit_git_history.py --report var/history-audit.json` on the original workspace. Reports contain reasons and locations, never credential values. A clean worktree audit does not clear old history.
2. Export to a new directory: `python scripts/public_release.py export --destination ../dataset-platform-public`. The destination must not already exist. The exporter uses explicit source roots and curated public docs; it excludes old Git metadata, nested usermanage, account exports, datasets, weights, private deployment/recovery notes, chat and resume drafts. It does not overwrite or delete the original repository.
3. Inspect the candidate: `python scripts/public_release.py check --root ../dataset-platform-public --verify-manifest`. Run its sandbox and isolated checks. The manifest records file hashes without embedding the owner's machine paths or original commit history.
4. In the **candidate directory**, initialize a new Git repository (`git init -b main`), stage reviewed files, run `python scripts/audit_repository.py --staged` and commit using your own configured identity. Run the history check again. Do not use the original repository as the remote/push source.
5. Create the intended GitHub repository and publish only after reviewing the exact candidate, ownership/license and visibility. This preparation command never creates a remote, pushes, changes visibility or sends files to another account.

On GitHub, enable private vulnerability reporting and secret-scanning/push protection where available. Protect main and require CI/review. Review dependency PRs and publish a tagged release only after its checks pass. Never upload raw failure logs, account files or real dataset images as issue attachments.

MIT applies to the contributors' own project code. Preserve third-party licenses and obtain dataset/model redistribution rights separately. The public export is not a promise that every external service or model has the same license.

If credentials were already public, revoke them before attempting history cleanup. Merely deleting a file or making a repository private does not recall forks or previous clones. See [GitHub's sensitive-data guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
