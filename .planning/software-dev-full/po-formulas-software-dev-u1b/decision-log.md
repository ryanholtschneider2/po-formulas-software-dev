- **Decision**: Reused the existing `wts-<sanitized-id>` branch and `<rig>.wt-<sanitized-id>/` path logic for epics.
  **Why**: The plan chose this naming to preserve the existing worktree contract while changing lifecycle ownership from child to epic.
  **Alternatives considered**: A separate `wts-epic-<id>` prefix, which would add a second naming convention without a current collision case.

- **Decision**: Let `epic_wts` own shared worktree setup and `epic_finalize` own merge/cleanup, while `software_dev_full` only reuses stamped context.
  **Why**: This keeps standalone child runs unchanged and gives failed epics a single preserved forensic worktree.
  **Alternatives considered**: Creating the worktree inside `epic_run`, which would make that graph wrapper responsible for WTS-specific lifecycle details.

- **Decision**: Retry detection requires `epic_id`, `work_dir`, and `branch` metadata before reusing a worktree.
  **Why**: A standalone bead can also have branch/work_dir-like metadata; requiring `epic_id` avoids guessing and falls back to legacy standalone behavior when metadata is incomplete.
  **Alternatives considered**: Reusing any existing `metadata.work_dir`, which could route standalone retries into the wrong checkout.

- **Decision**: Moved the epic-level documenter step before remote CI instead of running CI twice.
  **Why**: The build critic correctly found that `merge_worktree()` commits pending documenter changes before merge; running docs before CI keeps the pushed/check-passed branch byte-for-byte aligned with the branch that finalize merges.
  **Alternatives considered**: Leaving docs after CI and rerunning remote CI, which would be slower and would still make the success order harder to reason about.
