# Controller operations

Production Controller runs locally; de is only the Nginx edge. Follow the
`de-first-deployment` skill for changes to the service and CI infrastructure.

## SQLite backups

- Do not run ad hoc `sqlite3.Connection.backup()` loops via `docker exec` on
  production databases. Killing the Docker CLI does not cancel the container's
  exec process. Never delete an output while a backup still has it open.
- Use the host unit `mygithub-sqlite-backup@<unique-id>.service`. Its committed
  implementation is `services/github-action-service/scripts/sqlite_backup.py`;
  installation and recovery instructions are in
  `docs/MYGITHUB12_SQLITE_BACKUP_SAFETY.md`.
- Keep the fixed read snapshot, source lock, worker deadline, no-clobber atomic
  publication, integrity verification and service cgroup I/O limits enabled.
- A timeout is a failed backup. Verify the unit's cgroup is empty before
  removing a partial file. Do not automatically retry or proceed with repairs
  requiring a backup unless a verified backup completed.
