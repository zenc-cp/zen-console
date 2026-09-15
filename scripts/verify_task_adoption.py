"""Run actual working-copy task tests with explicit synthetic dependencies."""
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
TESTS = ['tests/test_background_tasks_1_2.py', 'tests/test_background_tasks_3_4.py', 'tests/test_task_fencing.py', 'tests/test_task_result_receipt.py']
SOURCES = ['api/task_store.py', 'api/task_worker.py', 'api/task_routes.py', 'api/task_sweeper.py', 'api/task_integration.py', 'api/streaming.py', 'api/result_receipt.py']
parser = argparse.ArgumentParser()
parser.add_argument('--artifact-dir', type=Path, required=True)
parser.add_argument('--phase', choices=('baseline', 'verify'), default='verify')
parser.add_argument('--_child', action='store_true', help=argparse.SUPPRESS)
args = parser.parse_args()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if args._child:
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ.pop('PYTEST_ADDOPTS', None)
    os.environ.pop('TELEGRAM_BOT_TOKEN', None)
    sys.path.insert(0, str(REPO / 'tests'))
    import _task_unit_support as support
    support.install(args.artifact_dir / 'synthetic-state')
    import pytest
    selected_files = [n for n in TESTS if (REPO / n).exists()]
    pytest_args = ['--noconftest', '-p', 'no:cacheprovider', '-v', '-W', 'error::pytest.PytestUnhandledThreadExceptionWarning', '--basetemp', str(args.artifact_dir / 'pytest-tmp'), '-o', 'tmp_path_retention_policy=all', *selected_files]
    code = pytest.main(pytest_args, plugins=[support])
    report = support.report()
    report['pytest_args'] = pytest_args
    report['exit_code'] = int(code)
    (args.artifact_dir / 'pytest.json').write_bytes((json.dumps(report, indent=2) + '\n').encode())
    support.close()
    raise SystemExit(int(code))

artifact = args.artifact_dir.resolve()
if artifact == REPO or artifact.is_relative_to(REPO):
    raise SystemExit('Artifact directory must be outside the repository')
artifact.mkdir(parents=True, exist_ok=True)
runs = artifact / 'test-runs'
runs.mkdir(exist_ok=True)
run_dir = runs / (args.phase + '-' + uuid.uuid4().hex)
run_dir.mkdir(exist_ok=False)
assert not (run_dir / 'pytest-tmp').exists()

baseline_path = artifact / 'baseline-inputs.json'
baseline = json.loads(baseline_path.read_bytes()) if baseline_path.exists() else None
if baseline is not None:
    assert Path(baseline['repo_root']).resolve() == REPO
    for name, digest in baseline['unrelated_changes_sha256'].items():
        assert sha(REPO / name) == digest, ('unrelated change drift', name)
    for name, digest in baseline['historical_artifacts_sha256'].items():
        assert sha(Path(name)) == digest, ('historical evidence changed', name)

tracked_names = SOURCES + TESTS + ['tests/_task_unit_support.py', 'tests/fixtures/task_result_receipt.json', 'scripts/verify_task_adoption.py', 'ARCHITECTURE.md', 'ROADMAP.md', 'TESTING.md']
before = {n: sha(REPO / n) for n in tracked_names if (REPO / n).exists()}
for name in SOURCES:
    if args.phase == 'verify':
        assert (REPO / name).exists(), name
    if (REPO / name).exists():
        ast.parse((REPO / name).read_bytes(), filename=name)
command = [sys.executable, '-B', str(Path(__file__).resolve()), '--artifact-dir', str(run_dir), '--phase', args.phase, '--_child']
env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
run = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
(run_dir / 'stdout.txt').write_bytes(run.stdout.encode('utf-8'))
(run_dir / 'stderr.txt').write_bytes(run.stderr.encode('utf-8'))
# Retain the full log, but keep successful console output operationally small.
print(run.stdout if run.returncode else '\n'.join(run.stdout.splitlines()[-3:]))
print(run.stderr, file=sys.stderr)
assert before == {n: sha(REPO / n) for n in before}, 'Source changed while tests ran'
report_path = run_dir / 'pytest.json'
assert report_path.exists(), 'Child failed before writing evidence; inspect retained stdout/stderr'
result = json.loads(report_path.read_bytes())
assert not result['server_conftest_loaded']
assert all(n == 0 for n in result['effect_probe_calls'].values()), 'Forbidden effect was attempted'
failures = [r for r in result['test_reports'] if r['outcome'] == 'failed']
skips = [r for r in result['test_reports'] if r['outcome'] == 'skipped']
passed = [r for r in result['test_reports'] if r['when'] == 'call' and r['outcome'] == 'passed']

if baseline is not None:
    for name, digest in baseline['unrelated_changes_sha256'].items():
        assert sha(REPO / name) == digest, name
    for name, digest in baseline['historical_artifacts_sha256'].items():
        assert sha(Path(name)) == digest, name
    status = subprocess.run(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all'], cwd=REPO, capture_output=True, text=True, check=True)
    changed = {line[3:].replace('\\', '/').strip('"') for line in status.stdout.splitlines() if line}
    allowed = set(baseline['existing_write_set_sha256']) | set(baseline['new_write_set']) | set(baseline['unrelated_changes_sha256'])
    assert changed <= allowed, ('Unapproved working-tree path', sorted(changed - allowed))

summary = {'phase': args.phase, 'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'python_version': sys.version.split()[0], 'run_dir': str(run_dir), 'selected': len(result['selected']), 'passed': len(passed), 'failed_reports': len(failures), 'skipped': len(skips), 'deselected': result['deselected'], 'exit_code': run.returncode, 'source_sha256': before, 'actual_working_copy_imports': result['actual_working_copy_imports'], 'synthetic_dependencies': result['synthetic_dependencies'], 'effect_probe_calls': result['effect_probe_calls'], 'server_conftest_loaded': False, 'live_agent_or_deployment_exercised': False, 'unrelated_changes_preserved': baseline is not None}
checks = {
    'all_tests_passed': run.returncode == 0 and not failures and not skips,
    'every_selected_test_called': len(passed) == len(result['selected']) and len(passed) > 0,
    'no_deselection': not result['deselected'],
    'all_required_files_selected': all(any(n.startswith(name + '::') for n in result['selected']) for name in TESTS),
    'actual_modules_loaded': {'task_store', 'task_worker', 'task_routes', 'task_sweeper', 'task_integration', 'result_receipt'} <= set(result['actual_working_copy_imports']),
}
summary['checks'] = checks
if args.phase == 'verify':
    summary['status'] = 'pass' if all(checks.values()) else 'fail'
else:
    summary['status'] = 'baseline-observed'
(run_dir / 'verification.json').write_bytes((json.dumps(summary, indent=2) + '\n').encode())
print(json.dumps(summary, indent=2))
raise SystemExit((run.returncode or 1) if summary['status'] == 'fail' else run.returncode)
