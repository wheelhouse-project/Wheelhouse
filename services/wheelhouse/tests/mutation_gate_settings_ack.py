"""C4 settings acknowledgement gate. Run only in the isolated task worktree.

Uses the project test wrapper and fresh bytecode directories. A verdict needs
the named assertion failure, fresh JUnit output and no test errors. Source
bytes are restored only after owned child cleanup is confirmed. Unknown exit
preserves the mutant, original backup, bytecode, and logs and aborts the sweep.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / 'services/wheelhouse'
sys.path.insert(0, str(ROOT / 'scripts/codex'))
from owned_process import CleanupUnconfirmedError, run_owned
TESTS = ['tests/test_gui.py', 'tests/test_state_manager.py', 'tests/test_coupled_config_save.py',
         'tests/test_config_service.py']
MUTATIONS = [
    ('ack-confirms-live', 'state_manager.py', [
        ('result = {key: self.config_service.get_persisted(key) for key in values}',
         'result = {key: self.config_service.get(key) for key in values}'),
    ], 'test_settings_ack_never_confirms_newer_unsaved_live_edit[ack]'),
    ('reconcile-confirms-live', 'state_manager.py', [
        ("'values': {key: self.config_service.get_persisted(key) for key in keys}",
         "'values': {key: self.config_service.get(key) for key in keys}"),
    ], 'test_settings_ack_never_confirms_newer_unsaved_live_edit[reconcile]'),
    ('broadcast-confirms-live', 'gui.py', [
        ('persisted.get(key, message.get(wire_key, default))', 'message.get(wire_key, default)'),
    ], 'test_settings_ack_never_confirms_newer_unsaved_live_edit[broadcast]'),
    ('cancel-all-abandons-thread', 'config_service.py', [
        ('writer = asyncio.get_running_loop().run_in_executor(None, do_save)',
         'writer = asyncio.create_task(asyncio.to_thread(do_save))'),
        ('cancelled = True', 'if writer.cancelled():\n                            raise\n                        cancelled = True'),
    ], 'test_staged_cancel_all_owns_filesystem_until_publication[True]'),
    ('ack-send', 'state_manager.py', [
        ('self.state_to_gui_queue.put_nowait(message)', 'pass  # mutated: lost acknowledgement'),
    ], 'test_settings_ack_carries_request_and_saved[True]'),
    ('saved-bool', 'state_manager.py', [
        ("'saved': saved, 'values': result", "'saved': True, 'values': result"),
    ], 'test_settings_ack_carries_request_and_saved[False]'),
    ('tentative-live-values', 'config_service.py', [
        ('draft._config, draft._removed = snapshot, removed',
         'draft._config, draft._removed = self._config, self._removed'),
    ], 'test_staged_settings_stay_private_until_disk_success[False]'),
    ('stale-reply', 'gui.py', [
        ("pending = self._settings_requests.get(request_id)\n        if pending is None:\n            return",
         "pending = self._settings_requests.setdefault(request_id, {'values': dict(message.get('values', {}))})"),
        ('if self._settings_latest.get(key) == request_id and key in values', 'if key in values'),
    ], 'test_settings_ack_older_reply_cannot_replace_newer_success'),
    ('failure-keeps-new-value', 'gui.py', [
        ("defaults.get(key) if values[key] is None\n                                             else values[key]",
         "pending['values'][key] if failed else values[key]"),
    ], 'test_settings_ack_failure_reverts_confirmed_geometry'),
    ('pending-dropped', 'gui.py', [
        ('return bool(self._settings_requests)', 'return False'),
    ], 'test_settings_ack_pending_until_reply'),
    ('wrong-writeback-baseline', 'config_service.py', [
        ('_write_back_in_place(self._config, outcome["merged"], baseline,',
         '_write_back_in_place(self._config, outcome["merged"], snapshot,'),
    ], 'test_staged_settings_stay_private_until_disk_success[True]'),
    ('single-handler-id', 'main.py', [
        ("command.get('value'), request_id=command.get('request_id')", "command.get('value'), request_id=None"),
    ], 'test_settings_ack_handler_keeps_request_id'),
    ('group-handler-id', 'main.py', [
        ("command.get('values') or {}, request_id=command.get('request_id')",
         "command.get('values') or {}, request_id=None"),
    ], 'test_settings_ack_group_round_trip_through_handler_and_disk[True]'),
    ('notice-fallback-silent', 'gui.py', [
        ("send_notice('WheelHouse settings', text, timeout=15)",
         'pass  # mutated: no fallback notice'),
    ], 'test_settings_ack_notice_reaches_user_when_box_cannot_render'),
    ('superseded-request-kept', 'gui.py', [
        ('del self._settings_requests[old_id]',
         'pass  # mutated: superseded request retained'),
    ], 'test_settings_ack_superseded_request_leaves_no_reconciliation_ghost'),
    ('timeout-disabled', 'gui.py', [
        ('self._check_settings_timeout()', 'pass  # mutated: no reconciliation'),
    ], 'test_settings_ack_timeout_reads_actual_value_without_retry'),
    ('caller-mutable-values', 'config_service.py', [
        ('staged = copy.deepcopy(values)', 'staged = values'),
    ], 'test_staged_settings_copy_caller_values'),
    ('nested-publish-lost', 'config_service.py', [
        ('held_tables=held_tables)\n                self._loaded',
         'held_tables=None)\n                self._loaded'),
    ], 'test_staged_settings_nested_edit_preserves_sibling_and_identity'),
    ('cancel-abandons-writer', 'config_service.py', [
        ('cancelled = True', 'raise  # mutated: abandon owned completion'),
    ], 'test_staged_settings_cancel_waits_for_disk_outcome'),
    ('status-clear-dropped', 'gui.py', [
        ("elif not self.settings_pending:\n            self.settings_status_text = ''",
         "elif not self.settings_pending:\n            pass"),
    ], 'test_settings_ack_superseded_request_leaves_no_reconciliation_ghost'),
    # wh-codex-merge-audit.4.1.3 and .4.1.6 (stage 4 audit fix).
    ('audit4_rearm-bound-removed', 'gui.py', [
        ("            if pending['attempts'] >= 3:\n"
         "                self._settings_fail_request(request_id)\n"
         "                continue\n"
         "            pending['attempts'] += 1",
         "            pending['attempts'] += 1"),
    ], 'test_settings_ack_timeout_retries_are_bounded_then_fail'),
    ('audit4_notice-guard-removed', 'gui.py', [
        ("            # send_notice does not catch its own delivery failures.\n"
         "            try:\n"
         "                send_notice('WheelHouse settings', text, timeout=15)\n"
         "            except Exception:\n"
         "                logger.exception('Could not deliver the settings notice')",
         "            send_notice('WheelHouse settings', text, timeout=15)"),
    ], 'test_settings_ack_notice_delivery_failure_is_logged_not_raised'),
    ('audit4_drain-tick-guard-removed', 'gui.py', [
        ("        try:\n"
         "            self._check_settings_timeout()\n"
         "        except Exception:\n"
         "            logger.exception('Error checking the settings acknowledgement timeout')",
         "        self._check_settings_timeout()"),
    ], 'test_settings_ack_timeout_error_cannot_drop_the_drain_tick'),
]


def run_tests():
    evidence = SERVICE / '.pytest_cache' / 'settings-ack-gate'
    evidence.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='run-', dir=evidence))
    report = directory / 'results.xml'
    bytecode = directory / 'bytecode'
    confirmed = True
    try:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=str(bytecode))
        result = run_owned(
            [sys.executable, str(ROOT / 'scripts/run_tests.py'), *TESTS,
             '-k', 'settings_ack or staged_settings or staged_cancel_all', '--junitxml', str(report)],
            cwd=ROOT, env=env, timeout_seconds=120, evidence_dir=directory,
        )
        if not result.cleanup_confirmed:
            raise CleanupUnconfirmedError('Child cleanup was not confirmed')
        if result.returncode not in (0, 1) or not report.exists():
            raise RuntimeError(result.stdout + result.stderr)
        cases = list(ET.parse(report).getroot().iter('testcase'))
        if not cases or any(case.find('error') is not None for case in cases):
            raise RuntimeError('No tests or test errors: ' + result.stdout + result.stderr)
        failures = {case.get('name'): case.find('failure').get('message', '')
                    for case in cases if case.find('failure') is not None}
        return result.returncode, failures
    except BaseException as exc:
        confirmed = getattr(exc, 'cleanup_confirmed', True)
        raise
    finally:
        if confirmed and bytecode.is_dir():
            shutil.rmtree(bytecode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    prepared = []
    for name, filename, edits, catcher in MUTATIONS:
        path = SERVICE / filename
        original = path.read_bytes()
        source = original.decode('utf-8')
        newline = '\r\n' if b'\r\n' in original else '\n'
        for old, new in edits:
            old, new = old.replace('\n', newline), new.replace('\n', newline)
            if source.count(old) != 1:
                raise RuntimeError(f'{name}: anchor count {source.count(old)}')
            source = source.replace(old, new, 1)
        compile(source, str(path), 'exec')
        prepared.append((name, path, original, source.encode('utf-8'), catcher))
    print(f'Checked {len(prepared)} mutations; 0 errors', flush=True)
    if args.check:
        return 0
    if run_tests()[0] != 0:
        raise RuntimeError('Baseline is not green')
    survivors = errors = 0
    backups = SERVICE / '.pytest_cache' / 'settings-ack-gate' / uuid.uuid4().hex
    backups.mkdir(parents=True)
    for name, path, original, mutant, catcher in prepared:
        (backups / (name + '.original')).write_bytes(original)
        confirmed = True
        try:
            path.write_bytes(mutant)
            code, failures = run_tests()
            if code == 1 and 'assert' in failures.get(catcher, '').lower():
                print(f'CAUGHT {name}: {catcher}', flush=True)
            else:
                survivors += 1
                print(f'SURVIVED {name}: {failures}', flush=True)
        except CleanupUnconfirmedError:
            confirmed = False
            raise
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            errors += 1
            print(f'ERROR {name}: {exc}', flush=True)
        finally:
            if confirmed:
                path.write_bytes(original)
                if path.read_bytes() != original:
                    raise RuntimeError(f'Restore failed: {path}')
    print(f'{len(prepared)} mutations; {survivors} survivors; {errors} errors')
    return int(bool(survivors or errors))


if __name__ == '__main__':
    raise SystemExit(main())
