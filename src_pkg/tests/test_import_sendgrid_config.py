from pathlib import Path
import sys

from dotenv import dotenv_values


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packaging"))
import import_sendgrid_config


def test_import_sendgrid_config_preserves_unrelated_settings(tmp_path):
    source = tmp_path / "halo.env"
    destination = tmp_path / "medhunt.env.local"
    source.write_text(
        "SENDGRID_API_KEY='secret-key'\n"
        "EMAIL_FROM='verified@example.test'\n"
        "EMAIL_FROM_NAME='Recruiting Alerts'\n"
        "CEIPAL_EMAIL='one@example.test,two@example.test'\n",
        encoding="utf-8",
    )
    destination.write_text(
        "MEDHUNT_LOCAL_API_TOKEN='keep-me'\n"
        "SENDGRID_API_KEY='old-key'\n",
        encoding="utf-8",
    )

    count = import_sendgrid_config.import_config(source, destination)
    values = dotenv_values(destination)

    assert count == 2
    assert values["MEDHUNT_LOCAL_API_TOKEN"] == "keep-me"
    assert values["SENDGRID_API_KEY"] == "secret-key"
    assert values["EMAIL_FROM"] == "verified@example.test"
    assert values["EMAIL_FROM_NAME"] == "Recruiting Alerts"
    assert values["WATCHER_NOTIFICATION_EMAILS"] == "one@example.test,two@example.test"
    assert values["WATCHER_EMAIL_NOTIFICATIONS_ENABLED"] == "1"
