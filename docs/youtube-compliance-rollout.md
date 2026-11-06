# YouTube compliance: enabling enforcement

The YouTube data-retention rules (`apps/social_accounts/youtube_compliance.py`) ship
switched off: `YOUTUBE_COMPLIANCE_ENFORCEMENT=false`. Explicit deletion requests and
confirmed permanent grant rejection still trigger immediate cleanup. Age-based
enforcement only runs once the flag is on. Follow these steps before turning it on.

Staging and production are separate releases. Do every step in staging first, then
repeat steps 1–5 in production before enabling its flag.

1. **Deploy with the flag off.** Apply migrations and build static assets. Check that
   existing YouTube connections keep their grants, statuses and nullable authorizers.
   No migration marks them for reconnect.
2. **Review the counts (read-only).** Run `python manage.py youtube_compliance_report`,
   ideally with a read-only database role. It makes no API calls and deletes nothing.
   Review connection and status counts, overdue authorizations and comments, old
   snapshots and retained videos. Unknown authorizers are informational only.
3. **Inventory the OAuth projects.** Run
   `python manage.py youtube_project_inventory --environment <staging|production>`.
   It exports fingerprints and source resolution, never tokens or secrets. Match the
   fingerprints to the real Cloud OAuth credentials and project numbers, for example:
   `gcloud projects list --format='table(projectId,projectNumber,name)'`.
   Environment credentials take precedence over organization credentials. Don't
   assume one project serves every deployment.
4. **Backfill native reports.** For each YouTube connection, run
   `python manage.py backfill_analytics --account-id <account-UUID> --days 180`.
   Wait for the worker to finish and confirm the cache rows exist. After that, the
   hourly analytics schedule refreshes the cache once per Pacific report date.
5. **Confirm the worker runs the compliance tasks.** Both tasks use the
   `youtube-compliance` queue: the daily verifier and the hourly revocation retry.
   They run in the general lane of `run_studio_worker`, and `post_migrate` registers
   them. Check in the database that the tasks actually ran and that their next runs
   are scheduled. Registration alone is not enough.
6. **Enable the flag in staging.** Set `YOUTUBE_COMPLIANCE_ENFORCEMENT=true`. Using
   disposable accounts, exercise the retention boundaries (25, 29 and 180 days),
   explicit deletion, transient failures and renewal races. Review the actual deletion
   counts and retry results.
7. **Enable the flag in production.** After enabling it, monitor failed-verification
   ages, stale copy counts, task failures and revocation outbox expiry.

The reviewer response and screenshot evidence prepared for the Google audit
(October 2026) live in git history, in
`development_specs/youtube-audit-remediation.md` and `output/youtube-review/`.
Commit `a25108b` is the last one that still has them.

Policy references: [Developer Policies](https://developers.google.com/youtube/terms/developer-policies),
[policy guide](https://developers.google.com/youtube/terms/developer-policies-guide),
[API branding](https://developers.google.com/youtube/terms/branding-guidelines),
[channel reports](https://developers.google.com/youtube/analytics/channel_reports),
[metrics](https://developers.google.com/youtube/analytics/metrics).
