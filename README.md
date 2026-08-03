# kaltura-backup

A small Python utility for creating Kaltura backups from a local configuration.

## Quick start

1. Create and activate a virtual environment:
   - `py -3.11 -m venv .venv`
   - `.venv\Scripts\activate`
2. Install the package:
   - `pip install -e .`
3. Copy the example configuration and adjust it:
   - `copy config.ini.example config.ini`
4. Run the tool:
   - `python -m kaltura_backup --config config.ini`

To preview what would happen without writing backup artifacts, use:

```bat
python -m kaltura_backup --config config.ini --dry-run
```

To retry only entries that were previously marked as failed in the saved state, use:

```bat
python -m kaltura_backup --config config.ini --retry-failed
```

## Windows Task Scheduler

The repository includes a batch launcher at [run_backup.bat](run_backup.bat) that activates the local virtual environment and starts the backup CLI from the repository root.

To run it from Task Scheduler:

1. Open Task Scheduler and create a new Basic Task.
2. Set the action to Start a program.
3. Use these values:
   - Program/script: `cmd.exe`
   - Add arguments: `/c "C:\path\to\kaltura-backup\run_backup.bat"`
   - Start in: `C:\path\to\kaltura-backup`
4. Configure the task to run with the account that has access to the backup paths and the Kaltura credentials.
5. Ensure that account can read and write to the backup destination, the log directory, and the repository folder that contains the configuration file.
6. If the task runs without a user session, make sure the account has access to the local Python installation and that the virtual environment is created for that same account.
7. Optionally enable Run whether user is logged on or not and set a suitable trigger such as a daily schedule.

If you prefer to invoke the CLI directly, use:

```bat
python -m kaltura_backup --config config.ini
```

## Configuration

The main configuration file is [config.ini.example](config.ini.example). If no config file is present, the application will create a default [config.ini](config.ini) for you on first run so you can adjust the connection and path settings before the next execution.

The example config now includes a `[metadata_profiles]` section. Each profile is configured on its own line using the Kaltura metadata profile ID and a comma-separated list of field names:

```ini
[metadata_profiles]
4696 = Attributie, LinkNaarBron, LinkNaarLicentievoorwaarden
4694 = VrijwaringsverklaringPortretrecht
```

When metadata backup is enabled, custom metadata is written to `metadata.csv` for each entry. The file includes a header line with `entry_id`, `name`, and the configured metadata field names.

You can also bootstrap it explicitly from Python:

```python
from kaltura_backup import ensure_default_config

ensure_default_config("config.ini")
```

## Project roadmap status

The implementation has moved beyond the initial scaffold and is now in the later operational phases.

- Phase 1 — ? Complete: project structure, packaging metadata, configuration example, and base documentation.
- Phase 2 — ? Complete: configuration loading, validation, logging, domain models, and state management.
- Phase 3 — ? Complete: the Kaltura client abstraction is in place, with a stubbed fallback for environments without the SDK; live SDK integration still awaits broader real-world validation.
- Phase 4 — ? Complete: backup orchestration, state persistence, resume-aware skipping, and manifest generation are implemented.
- Phase 5 — ? Complete: backup artifacts for metadata, API responses, captions, thumbnails, and attachments are produced during runs.
- Phase 6 — ? Complete: retry handling, reporting, worker-pool concurrency, and graceful shutdown are implemented.
- Phase 7 — ?? In progress: CLI entrypoints, user-friendly failure handling, dry-run, and retrying previously failed entries are implemented; state rebuild remains a future enhancement.
- Phase 8 — ? Complete: regression tests cover the core workflow, CLI behavior, packaging, configuration, and reporting.
