"""P2P installation reads only an existing callback credential file path."""
import pathlib
import pytest
from scripts import install_p2p_runtime as runtime


def test_callback_file_comes_from_fixed_existing_service(tmp_path):
    key = tmp_path / "callback-key"
    key.write_text("never-log-me", encoding="utf-8")
    path = runtime.callback_path("DEPLOY_CONTROLLER_URL=http://127.0.0.1:8765 DEPLOY_CALLBACK_API_KEY_FILE=" + str(key))
    assert path == key
    assert str(key) not in runtime.CONTROLLER


@pytest.mark.parametrize("bad", [
    "",
    "DEPLOY_CALLBACK_API_KEY_FILE=relative-key",
    "DEPLOY_CALLBACK_API_KEY_FILE=/not/a/real/file",
    "DEPLOY_CALLBACK_API_KEY_FILE=/tmp/missing DEPLOY_CALLBACK_API_KEY_FILE=/tmp/missing",
])
def test_invalid_credential_file_fails_closed(bad):
    with pytest.raises(ValueError):
        runtime.callback_path(bad)



def test_exporter_is_root_owned_and_in_separate_systemd_units():
    assert str(runtime.EXPORT_DST).startswith("/usr/local/libexec/mygithub12/")
    assert str(runtime.EXPORT_DIR) == "/var/lib/mygithub12-p2p-source"
    assert runtime.EXPORT_SERVICE_DST.name == "mygithub12-p2p-source-export.service"
    assert runtime.EXPORT_TIMER_DST.name == "mygithub12-p2p-source-export.timer"
