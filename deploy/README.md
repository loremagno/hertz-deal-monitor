# Enabling the scheduled run

`monitor-workflow.yml` belongs at `.github/workflows/monitor.yml`, but the
GitHub credential stored on this machine carries `repo` scope and not
`workflow` scope, so it cannot push into `.github/workflows/`. Two ways to
finish, either is fine:

**Web UI, about thirty seconds.** In the repo: *Add file → Create new file*,
name it `.github/workflows/monitor.yml`, paste the contents of
`deploy/monitor-workflow.yml`, commit to `main`.

**Or widen the token.** Re-authorise Git Credential Manager with the
`workflow` scope, then:

```bash
git mv deploy/monitor-workflow.yml .github/workflows/monitor.yml
git commit -m "Enable scheduled monitor" && git push
```

The four repository secrets are already set:
`HERTZ_SMTP_USER`, `HERTZ_SMTP_PASSWORD`, `HERTZ_EMAIL_TO`, `HERTZ_NTFY_TOPIC`.

Once the workflow is in place, trigger the first run by hand from the
**Actions** tab (*Run workflow*) rather than waiting for the hour, so you can
watch it and confirm the runner is not blocked by Akamai.
