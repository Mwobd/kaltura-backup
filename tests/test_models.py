from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.models import BackupEntry, BackupStatistics, BackupStatus, DownloadStatus


def test_backup_entry_round_trip_serialization() -> None:
    entry = BackupEntry(
        entry_id="1",
        name="demo",
        updated_at=1,
        created_at=1,
    )
    entry.start()
    entry.downloads.media = True
    entry.downloads.media_completed = None

    payload = entry.to_dict()
    restored = BackupEntry.from_dict(payload)

    assert restored.entry_id == entry.entry_id
    assert restored.status == BackupStatus.PROCESSING
    assert restored.downloads.media is True


def test_backup_statistics_from_dict_handles_strings() -> None:
    stats = BackupStatistics(entries_completed=2, bytes_downloaded=42)
    payload = stats.to_dict()
    restored = BackupStatistics.from_dict(payload)

    assert restored.entries_completed == 2
    assert restored.bytes_downloaded == 42
