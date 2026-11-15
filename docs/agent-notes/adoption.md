# Repository adoption

- `Adoption.author_branches` starts one `dream/<run-id>` branch per configured
  repository from its committed default branch. Deltas and generate commands run
  in private worktrees; each change gets one evidence-ID commit and a stable
  `Dreaming-Change` trailer for crash reconciliation. Existing user files stay
  untouched until adoption. Private worktree paths are checked through their
  external parent: the worktree itself necessarily contains a Git marker.
- Public output checks added text and commit messages for credential patterns,
  private names and transcript spans beyond `quote_chars`. Existing repository
  text is exempt from the transcript check. Binary baseline assets are skipped;
  new binary patches are conservatively withheld. Failed scans save mode-0600
  patches under the private home's `patches/<run>/` directory.
- Every repository's changes share one branch and one adoption group. Change
  dependencies connect repositories. One held change, fetched-content provenance,
  missing lint/test commands, dirty checkout or non-default checkout holds the
  entire linked group. No commands run while holding a SQLite writer transaction.
- A detached check worktree builds the actual merge commit with its Makefile and
  tests. All linked repositories preflight before any merge; each checkout is
  checked again just before `merge --ff-only <tested-sha>`. Durable `merging` and
  `deploying` records precede those effects. Limits cannot abandon already-authored
  model-free adoption, although an instruction replay can be held by its budget.
- Deploy failure attempts a merge revert, generate and restored deployment for
  every merged group member. `Dreaming-Revert` marks recovery failure separately
  from user rejection. Git's ordinary revert messages are detected before later
  review; a reset/manual edit is not inferred to be a revert.
- A resumed run with a possible merge does no further automatic adoption.
  `reconcile_merges` uses Git ancestry to recover a completed merge, and the
  report supplies finish, individual-change undo and whole-merge undo commands.
  An earlier run with unfinished deployment prevents a later branch in that repo.
- Worktrees are removed on exit; branches remain. Discard removes unmerged
  branches and run-owned records, restores prior retained knowledge revisions and
  resets selected read cursors for a safe reread. It reconciles incomplete merge
  intents and refuses after any completed merge, returning recovery commands.
  External configuration is never changed.
