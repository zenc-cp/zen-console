"""Verify real console session persistence with isolated fake-agent boundaries."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
TEST = 'tests/test_background_session_persistence.py'
MODULES = ('models', 'task_store', 'helpers', 'result_receipt', 'streaming', 'task_worker', 'task_routes')
INPUTS = [f'api/{name}.py' for name in MODULES] + [TEST, 'scripts/verify_background_persistence.py']
parser = argparse.ArgumentParser()
parser.add_argument('--artifact-dir', required=True, type=Path)
parser.add_argument('--_child', action='store_true', help=argparse.SUPPRESS)
args = parser.parse_args()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if args._child:
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.pop('PYTEST_ADDOPTS', None)
    os.environ.pop('PERSISTENCE_PROBE_OVERLAY', None)
    os.environ['PERSISTENCE_PROBE_REPO'] = str(REPO)
    os.environ['PERSISTENCE_PROBE_RUN'] = str(args.artifact_dir)
    sys.dont_write_bytecode = True
    import pytest

    class Evidence:
        def __init__(self):
            self.selected, self.reports, self.deselected = [], [], []

        def pytest_collection_finish(self, session):
            self.selected = [item.nodeid for item in session.items]
            for item in session.items:
                assert not any(m.name in ('skip', 'skipif', 'xfail') for m in item.iter_markers()), item.nodeid

        def pytest_deselected(self, items):
            self.deselected.extend(item.nodeid for item in items)

        def pytest_runtest_logreport(self, report):
            self.reports.append({'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome})

    plugin = Evidence()
    pytest_args = ['--noconftest', '-p', 'no:cacheprovider', '-v', '--tb=short',
                   '-W', 'error::pytest.PytestUnhandledThreadExceptionWarning',
                   '--rootdir', str(REPO), '--confcutdir', str(REPO),
                   '--basetemp', str(args.artifact_dir / 'pytest-tmp'),
                   '-o', 'tmp_path_retention_policy=all', str(REPO / TEST)]
    code = pytest.main(pytest_args, plugins=[plugin])
    assert not any(n == 'conftest' or n.endswith('.conftest') for n in sys.modules)
    payload = {'selected': plugin.selected, 'reports': plugin.reports,
               'deselected': plugin.deselected, 'exit_code': int(code),
               'pytest_args': pytest_args, 'pytest_version': pytest.__version__,
               'server_conftest_loaded': False}
    (args.artifact_dir / 'pytest.json').write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    raise SystemExit(code)

artifact = args.artifact_dir.resolve()
assert artifact != REPO and not artifact.is_relative_to(REPO), 'Keep synthetic artifacts outside the repository'
run_dir = artifact / 'test-runs' / ('persistence-' + uuid.uuid4().hex)
run_dir.mkdir(parents=True, exist_ok=False)
assert not (run_dir / 'pytest-tmp').exists()
before = {name: sha(REPO / name) for name in INPUTS}
for name in INPUTS:
    ast.parse((REPO / name).read_bytes(), filename=name)
command = [sys.executable, '-B', str(Path(__file__).resolve()), '--artifact-dir', str(run_dir), '--_child']
run = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120,
                     env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1'))
(run_dir / 'stdout.txt').write_text(run.stdout, encoding='utf-8')
(run_dir / 'stderr.txt').write_text(run.stderr, encoding='utf-8')
print(run.stdout if run.returncode else '\n'.join(run.stdout.splitlines()[-3:]))
print(run.stderr, file=sys.stderr)
assert before == {name: sha(REPO / name) for name in INPUTS}, 'Source changed during verification'
report_path = run_dir / 'pytest.json'
assert report_path.exists(), 'Child did not finish its report; inspect retained logs'
report = json.loads(report_path.read_text(encoding='utf-8'))
cases = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(run_dir.glob('case-*.json'))]
passed = [r for r in report['reports'] if r['when'] == 'call' and r['outcome'] == 'passed']
failures = [r for r in report['reports'] if r['outcome'] == 'failed']
skips = [r for r in report['reports'] if r['outcome'] == 'skipped']
expected_imports = {name: str(REPO / 'api' / (name + '.py')) for name in MODULES}
expected_hashes = {name: before['api/' + name + '.py'] for name in MODULES}
checks = {
    'all_tests_passed': run.returncode == 0 and not failures and not skips,
    'all_selected_called': len(passed) == len(report['selected']) and len(passed) > 0,
    'no_deselection': not report['deselected'],
    'all_case_evidence_retained': len(cases) == len(report['selected']) and bool(cases),
    'actual_modules_loaded': all(c['imports'] == expected_imports for c in cases),
    'actual_module_hashes_match': all(c['source_sha256'] == expected_hashes for c in cases),
    'no_external_effects': all(not c['effect_violations'] and c['network_process_probe_calls'] == [0, 0, 0] for c in cases),
    'server_conftest_unloaded': report['server_conftest_loaded'] is False,
    'real_thread_case_exercised': any(c['mode'] == 'real_thread_cancellation' and c['producer_threads'] == 1 and not c['driver_errors'] for c in cases),
}
summary = {'status': 'pass' if all(checks.values()) else 'fail',
           'timestamp_utc': datetime.now(timezone.utc).isoformat(),
           'python_version': sys.version.split()[0], 'run_dir': str(run_dir),
           'selected': len(report['selected']), 'passed': len(passed),
           'failed_reports': len(failures), 'skipped': len(skips),
           'deselected': report['deselected'], 'exit_code': run.returncode,
           'source_sha256': before, 'checks': checks,
           'synthetic_dependencies': ['config', 'profiles', 'workspace', 'agent', 'approval', 'provider', 'external session database', 'redactor', 'notification and insights sinks'],
           'live_agent_or_deployment_exercised': False}
(run_dir / 'verification.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
print(json.dumps(summary, indent=2))
raise SystemExit(0 if summary['status'] == 'pass' else (run.returncode or 1))
