"""Setup is explicit, identity-bound, non-destructive and never prints credentials."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'pair_samsung_tv.py'
spec = importlib.util.spec_from_file_location('pair_samsung_tv_test', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def pairing(monkeypatch):
    client = Mock(fingerprint='a' * 64)
    client.pair.return_value = 'synthetic-token'
    client.read_backlight.return_value = 5
    factory = Mock(return_value=client)
    monkeypatch.setattr(mod, 'SamsungIPClient', factory)
    monkeypatch.setattr(mod, 'read_identity', lambda ip: ('R95H', 'b' * 64))
    saved = Mock()
    monkeypatch.setattr(mod, 'save_pairing', saved)
    return factory, client, saved


def test_existing_file_never_requests_pairing(pairing, tmp_path):
    factory, _, saved = pairing
    destination = tmp_path / 'existing.dpapi'
    destination.write_bytes(b'keep')
    with pytest.raises(ValueError, match='already exists'):
        mod.pair_tv('192.0.2.10', destination, 'R95H')
    factory.assert_not_called()
    saved.assert_not_called()
    assert destination.read_bytes() == b'keep'


def test_wrong_model_never_requests_pairing(pairing, tmp_path):
    factory, _, saved = pairing
    with pytest.raises(mod.SamsungError, match='identity_mismatch'):
        mod.pair_tv('192.0.2.10', tmp_path / 'tv.dpapi', 'different')
    factory.assert_not_called()
    saved.assert_not_called()


def test_pairing_reads_only_and_never_prints_token(pairing, tmp_path, capsys):
    _, client, saved = pairing
    assert mod.pair_tv('192.0.2.10', tmp_path / 'tv.dpapi', 'R95H') == 0
    client.pair.assert_called_once()
    saved.assert_called_once()
    client.write_backlight.assert_not_called()
    assert 'synthetic-token' not in capsys.readouterr().out


def test_failed_capability_read_keeps_pairing(pairing, tmp_path):
    _, client, saved = pairing
    def fail_read():
        saved.assert_called_once()
        raise mod.SamsungError('unsupported')
    client.read_backlight.side_effect = fail_read
    with pytest.raises(mod.SamsungError, match='unsupported'):
        mod.pair_tv('192.0.2.10', tmp_path / 'tv.dpapi', 'R95H')
    client.pair.assert_called_once()
