"""Wznawia odczyty po sprawdzeniu dostępu; nie zeruje budżetu i nie odpytuje PGE."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pge_credentials import PgeAccessError
from pge_request_budget import RequestBudget


def main():
    if len(sys.argv) != 1:
        raise SystemExit('Uruchom bez argumentów, jako konto usługi, po sprawdzeniu przyczyny blokady.')
    try:
        RequestBudget().resume()
    except PgeAccessError as error:
        raise SystemExit(str(error)) from None
    print('Wznowiono odczyty PGE. Zachowano licznik zapytań i ewentualną blokadę HTTP 429.')


if __name__ == '__main__':
    main()
