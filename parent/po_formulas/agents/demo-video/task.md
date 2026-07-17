You are the **releaser** recording a demo video for issue `{{issue_id}}`.

Record a 30–60 second demo of the new feature in action. Use Playwright + ffmpeg or the `browser` skill. Save to `{{run_dir}}/demo.mp4` and copy to `{{run_dir}}/review-artifacts/demo.mp4`.

The code under test is the worker checkout at `{{pack_path}}`. Start or inspect the app from that path, never from the seed/root checkout.

If recording fails for infrastructure reasons, log to `{{run_dir}}/demo-skipped.md` and continue. Don't block on demo failures.

Reply with one line: `demo recorded` or `demo skipped: <reason>`.
