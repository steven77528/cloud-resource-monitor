# Project Maintenance

- This directory is the complete, canonical cloud-resource-monitor project.
- Make future changes directly here. Do not create update ZIPs, incremental
  release directories, or another project copy. Historical packages outside
  this directory are not runtime dependencies and must not overwrite current code.
- Keep source, configuration, deployment files, documentation, tests, and
  generated notification previews self-contained within this project.
- Preserve existing credentials and account/job/channel enablement unless the
  user requests a change. Never print secrets from `.env`, `config/config.yaml`,
  or `config/notifications/notifications.yaml`.
- The deployment uses Python, Docker Compose, MySQL, and an independent
  notification gateway. Keep notification provider changes separate from cloud
  collection. RDS monitoring uses cloud APIs without new business DB accounts.
- Keep the original MySQL deadlock rule reserved separately from row-lock alerts.
- Run offline checks by default. Do not send real notifications or start the
  production scheduler during verification. MySQL integration tests must use an
  isolated temporary database server, never an existing business database.
- Update the Chinese README and relevant docs when behavior or deployment changes.
- Monitor-to-relay requests may use HTTP with a literal IP, explicit port and
  `/v1/monitor/collect` path as authorized by the user. Keep relay Bearer tokens
  and identity checks. Provider upstream calls still require verified HTTPS;
  HTTPS relay URLs continue to verify certificates and hostnames.
