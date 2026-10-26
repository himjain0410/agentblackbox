import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
import requests

BASE_URL = 'http://127.0.0.1:8000'

def main():
    requests.get(f'{BASE_URL}/api/health', timeout=5).raise_for_status()
    response = requests.post(f'{BASE_URL}/api/runs', json={'name': 'Real sandbox tool-execution demo', 'agent': 'Controlled Python Agent'}, timeout=10)
    response.raise_for_status()
    run_id = response.json()['id']
    seq = 0

    def record(title, event_type, status='success', details=None, evidence=''):
        nonlocal seq
        seq += 1
        r = requests.post(f'{BASE_URL}/api/runs/{run_id}/events', json={'title': title, 'event_type': event_type, 'status': status, 'details': details or {}, 'evidence': evidence}, timeout=10)
        r.raise_for_status()
        print(f'[{seq}] {title}: {status}')

    with tempfile.TemporaryDirectory(prefix='abb-demo-') as tmp:
        root = Path(tmp)
        migration = root / '001_init.sql'
        migration.write_text('-- demo migration\n')
        record('Created isolated test workspace', 'inspection', details={'workspace': 'temporary directory'}, evidence='Temporary directory; removed automatically after the run.')
        record('Inspected required migration', 'tool_call', details={'file': migration.name, 'sha256': hashlib.sha256(migration.read_bytes()).hexdigest()}, evidence='Actual file hash recorded.')
        migration.unlink()
        record('Deleted required migration file', 'file_change', 'warning', {'file': migration.name, 'operation': 'delete', 'exists_after': migration.exists()}, 'Actual deletion inside temporary directory only.')
        result = subprocess.run([sys.executable, '-c', f"from pathlib import Path; p=Path({str(migration)!r}); assert p.exists(), 'Required migration missing'"], capture_output=True, text=True)
        record('Re-ran startup requirement check', 'verification', 'failure' if result.returncode else 'success', {'exit_code': result.returncode, 'stderr': result.stderr[-500:]}, 'Actual Python subprocess result.')
        migration.write_text('-- restored migration\n')
        result2 = subprocess.run([sys.executable, '-c', f"from pathlib import Path; p=Path({str(migration)!r}); assert p.exists(), 'Required migration missing'; print('startup ok')"], capture_output=True, text=True)
        record('Restored migration and verified recovery', 'verification', 'success' if result2.returncode == 0 else 'failure', {'exit_code': result2.returncode, 'stdout': result2.stdout}, 'Actual Python subprocess after restoration.')
    requests.post(f'{BASE_URL}/api/runs/{run_id}/finish', timeout=10).raise_for_status()
    print(f'\nDone! Run ID: {run_id}\nOpen {BASE_URL} and select Real sandbox tool-execution demo.')

if __name__ == '__main__':
    main()