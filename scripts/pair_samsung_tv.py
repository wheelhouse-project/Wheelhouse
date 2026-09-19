"""Explicit Samsung TV pairing setup. Never changes brightness or other picture settings."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.wheelhouse.integrations.samsung_control import (
    Pairing, SamsungError, SamsungIPClient, read_identity, save_pairing, validate_address,
)


def pair_tv(address: str, destination: Path, expected_model: str) -> int:
    address = validate_address(address)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError('Pairing file already exists; choose a new file for re-pairing')
    if not destination.parent.is_dir():
        raise ValueError('The pairing file directory must already exist')
    model, identity = read_identity(address)
    if model != expected_model:
        raise SamsungError('identity_mismatch')
    print(f'Found {model}. Choose Allow on the TV when the pairing prompt appears.', flush=True)
    client = SamsungIPClient(address, '', None, timeout=55)
    token = client.pair()
    record = Pairing(address, model, identity, client.fingerprint, token)
    save_pairing(destination, record)
    # Save approval first so a failed capability query never causes a second pairing.
    print('Pairing saved with Windows encryption.', flush=True)
    client.timeout = 3
    brightness = client.read_backlight()
    print(f'TV brightness: {brightness} out of 50. No picture setting changed.', flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip-address', required=True)
    parser.add_argument('--credential-file', required=True, type=Path)
    parser.add_argument('--expected-model', required=True)
    args = parser.parse_args()
    try:
        return pair_tv(args.ip_address, args.credential_file, args.expected_model)
    except (SamsungError, ValueError) as error:
        print(f'Pairing did not finish: {error}', file=sys.stderr)
        return 1
    except Exception:
        print('Pairing could not be saved or completed. Check Windows file access.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
