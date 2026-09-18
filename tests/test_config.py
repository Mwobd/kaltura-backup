from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.config import ConfigLoader, MetadataConfig
from kaltura_backup.exceptions import ConfigurationError


def test_loader_raises_clear_error_for_missing_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text("[Connection]\nPartnerId = 1\n", encoding="utf-8")

    loader = ConfigLoader(config_path)

    try:
        loader.load()
    except ConfigurationError as exc:
        assert "Missing configuration section [Paths]" in str(exc)
    else:
        raise AssertionError("Expected ConfigurationError")


def test_loader_reads_metadata_profile_ids_and_field_names(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Connection]\nPartnerId = 123\nAdminSecret = secret\nServiceUrl = https://example.invalid\n"
        "[Paths]\nBackupDir = backup\nCsvDir = csv\nLogDir = logs\nReportDir = reports\nStateFile = state.json\n"
        "[Download]\nWorkers = 1\nRetryCount = 0\nRetryDelaySeconds = 0\nTimeout = 5\nSkipOlderThanHours = 0\nResumeDownloads = false\nVerifyChecksum = false\n"
        "[Export]\nSaveMetadata = true\nSaveApiResponses = false\nSaveCaptions = false\nSaveThumbnails = false\nSaveAttachments = false\n"
        "[metadata_profiles]\n4696 = Attributie, LinkNaarBron, LinkNaarLicentievoorwaarden\n4694 = VrijwaringsverklaringPortretrecht\n4695 = Contractpartij, Contractnummer, Artikel24ARVODI2018OngewijzigdVanKrachtInContract\n4697 = GrondslagAVG\n4698 = Directie\n4699 = Status, EindeBewaartermijn, UitzonderingOpVernietiging\n4693 = Proces, Afdoeningsjaar, UitgeplaatstBijNIBG, LinkNaarNIBG\n"
        "[Logging]\nLevel = INFO\nKeepLogs = 1\n",
        encoding="utf-8",
    )

    configuration = ConfigLoader(config_path).load()

    assert configuration.metadata.profile_fields == {
        "4696": ("Attributie", "LinkNaarBron", "LinkNaarLicentievoorwaarden"),
        "4694": ("VrijwaringsverklaringPortretrecht",),
        "4695": ("Contractpartij", "Contractnummer", "Artikel24ARVODI2018OngewijzigdVanKrachtInContract"),
        "4697": ("GrondslagAVG",),
        "4698": ("Directie",),
        "4699": ("Status", "EindeBewaartermijn", "UitzonderingOpVernietiging"),
        "4693": ("Proces", "Afdoeningsjaar", "UitgeplaatstBijNIBG", "LinkNaarNIBG"),
    }


def test_loader_reads_mysql_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Connection]\nPartnerId = 123\nAdminSecret = secret\nServiceUrl = https://example.invalid\n"
        "[Paths]\nBackupDir = backup\nCsvDir = csv\nLogDir = logs\nReportDir = reports\nStateFile = state.json\n"
        "[Download]\nWorkers = 1\nRetryCount = 0\nRetryDelaySeconds = 0\nTimeout = 5\nSkipOlderThanHours = 0\nResumeDownloads = false\nVerifyChecksum = false\n"
        "[Export]\nSaveMetadata = true\nSaveApiResponses = false\nSaveCaptions = false\nSaveThumbnails = false\nSaveAttachments = false\n"
        "[mysql]\nhost = localhost\ndatabase = backup_10206\nroot_user = root\nroot_password = secret\nuser = backupuser10206\npassword = userpass\n"
        "[Logging]\nLevel = INFO\nKeepLogs = 1\n",
        encoding="utf-8",
    )

    configuration = ConfigLoader(config_path).load()

    assert configuration.database is not None
    assert configuration.database.host == "localhost"
    assert configuration.database.database == "backup_10206"
    assert configuration.database.user == "backupuser10206"
