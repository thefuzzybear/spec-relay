"""Preservation-first cleanup of explicitly owned completed worktrees."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import threading

from .cleanup_archive import create_archive, read_archive, restore_archive, restore_files, restore_metadata
from .cleanup_inventory import digest, exact_path, git_identity, inventory, registrations, unknown_files, require_supported_git, git
from .processes import is_locked
from .store import WorkflowError, identifier


def resource_manifest(run, attempt=None):
    """Record ownership when the runtime creates or borrows a worktree."""
    source = attempt or run
    identity = git_identity(str(Path(source['worktree']).resolve()))
    return {'id': attempt['id'] if attempt else run['id'] + '/integration',
            'run_id': run['id'], 'attempt_id': attempt['id'] if attempt else None,
            'ownership': 'borrowed' if not attempt and run.get('integration_owned') is False else 'owned',
            'repository': run['repository'], 'base_commit': source['base_commit'],
            **identity, 'registered_at': time.time()}


class CleanupManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def _resources(self, db, run):
        records = self.store.list(db, 'cleanup_resource', run['id'])
        known = {item['id'] for item in records}
        expected = [{'id': run['id'] + '/integration', 'path': run['worktree']}]
        expected.extend({'id': item['id'], 'path': item['worktree']} for item in self.store.list(db, 'attempt', run['id']))
        return records + [{**item, 'run_id': run['id'], 'ownership': 'unknown'} for item in expected if item['id'] not in known]

    def _expected_head(self, db, run, resource):
        if resource.get('attempt_id'):
            attempt = self.store.get(db, 'attempt', resource['attempt_id'])
            task = self.store.get(db, 'task', run['id'] + '/' + attempt['task_id'])
            if attempt['status'] != 'accepted' or task['status'] != 'integrated' or task['attempts'][-1] != attempt['id']:
                raise WorkflowError('Only integrated task attempts permit cleanup.')
            return attempt['candidate_commit']
        return run['integration_commit']

    def _writers(self, db, run, resource, head):
        self._require_revision_release(db, run, resource, head)
        self._require_idle_jobs(db, run)
        directory = self.store.root / 'runs' / run['id']
        if is_locked(directory / 'controller.lock'):
            raise WorkflowError('A supervisor controller still owns this run.')
        self._require_service_release(db, run, resource)

    def _require_revision_release(self, db, run, resource, head):
        releases = self.store.list(db, 'cleanup_release', run['id'])
        if not any(item['resource_id'] == resource['id'] and item['head'] == head for item in releases):
            raise WorkflowError('The coordinator did not release writers and owned services for this revision.')

    def _require_idle_jobs(self, db, run):
        for job in self.store.list(db, 'job', run['id']):
            directory = Path(job['directory'])
            if job['status'] in {'queued', 'running'} or any(is_locked(directory / name) for name in ('worker.lock', 'command.lock')):
                raise WorkflowError('A detached check or command still owns this run.')

    def _require_service_release(self, db, run, resource):
        for service in self.store.list(db, 'service', run['id']):
            if service.get('resource_id') in {None, resource['id']} and service.get('status') != 'released':
                raise WorkflowError('An owned service has no release record.')

    def _generated(self, db, run, resource):
        result = dict(resource.get('generated_artifacts', {}))
        for job in self.store.list(db, 'job', run['id']):
            if job.get('attempt_id') == resource.get('attempt_id'):
                result.update(job.get('generated_artifacts', {}))
        return result

    def _inspect(self, db, run, resource, cleanup_id=None):
        if run['status'] != 'complete':
            raise WorkflowError('Only complete runs permit cleanup.')
        if resource.get('ownership') != 'owned':
            raise WorkflowError('The runtime does not own this worktree.')
        path = self._protected_path(run, resource)
        identity = git_identity(path)
        require_supported_git(path)
        self._require_recorded_identity(resource, identity)
        current = self._registered_worktree(run, path)
        expected = self._expected_head(db, run, resource)
        self._require_candidate_revision(path, identity, current, expected)
        self._writers(db, run, resource, expected)
        self._require_cleanup_lease(db, run, resource, cleanup_id)
        files = inventory(path)
        untracked = self._registered_outputs(db, run, resource, path, files)
        return {'identity': identity, 'inventory': files, 'generated_paths': untracked,
                'reclaim_bytes': sum(item.get('size', 0) for item in files)}

    def _protected_path(self, run, resource):
        path = exact_path(resource['path'])
        if path == Path(run['repository']).resolve():
            raise WorkflowError('The primary repository is protected.')
        state = self.store.root
        if path == state or state.is_relative_to(path):
            raise WorkflowError('The cleanup target contains the state store.')
        archives = state / 'cleanup-archives'
        if archives.is_relative_to(path):
            raise WorkflowError('The cleanup target contains recovery archives.')
        return path

    @staticmethod
    def _require_recorded_identity(resource, identity):
        for key in ('path', 'git_dir', 'common_dir', 'common_identity', 'path_identity', 'branch'):
            if identity[key] != resource[key]:
                raise WorkflowError('The recorded worktree identity changed.')

    @staticmethod
    def _registered_worktree(run, path):
        registered = registrations(run['repository'])
        current = next((item for item in registered if item.get('worktree') == str(path)), None)
        if not current or registered[0] == current:
            raise WorkflowError('The candidate is not a registered secondary worktree.')
        if 'locked' in current or 'prunable' in current:
            raise WorkflowError('Git locked this worktree or marked its registration invalid.')
        return current

    @staticmethod
    def _require_candidate_revision(path, identity, current, expected):
        if identity['head'] != expected or current.get('HEAD') != expected:
            raise WorkflowError('The candidate contains an unexpected commit.')
        if git(path, 'status', '--porcelain', '--untracked-files=no'):
            raise WorkflowError('The candidate contains tracked, staged, or conflicted changes.')

    def _require_cleanup_lease(self, db, run, resource, cleanup_id):
        for lease in self.store.list(db, 'cleanup_lease', run['id']):
            if lease['id'] == resource['id'] and lease.get('active') and lease['cleanup_id'] != cleanup_id:
                raise WorkflowError('Another cleanup job owns this worktree.')

    def _registered_outputs(self, db, run, resource, path, files):
        generated = self._generated(db, run, resource)
        untracked = unknown_files(path)
        indexed = {item['path']: item for item in files}
        for name in untracked:
            entry = indexed.get(name, {})
            if entry.get('type') != 'file' or generated.get(name) != entry.get('sha256'):
                raise WorkflowError('An unknown untracked file blocks cleanup: ' + name)
        return untracked

    def release(self, run_id, resource_id, agent_id, reason):
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise WorkflowError('Provide the coordinator agent ID.')
        with self.store.transaction() as db:
            run = self.store.get(db, 'run', run_id)
            resource = self.store.get(db, 'cleanup_resource', resource_id)
            if resource['run_id'] != run_id:
                raise WorkflowError('The resource belongs to another run.')
            head = git(resource['path'], 'rev-parse', 'HEAD')
            if head != self._expected_head(db, run, resource):
                raise WorkflowError('Release must identify the recorded candidate revision.')
            record = {'id': identifier('release'), 'run_id': run_id, 'resource_id': resource_id,
                      'head': head, 'agent_id': agent_id, 'reason': reason, 'released_at': time.time()}
            self.store.event(db, run_id, 'cleanup_writer_release', reason, record)
            self.store.put(db, 'cleanup_release', record)
            return record

    def plan(self, run_id):
        with self.store.transaction() as db:
            run = self.store.get(db, 'run', run_id)
            candidates, blocked = [], []
            for resource in self._resources(db, run):
                record = {'resource_id': resource['id'], 'path': resource['path']}
                try:
                    record['snapshot'] = self._inspect(db, run, resource)
                    record['digest'] = digest(record['snapshot'])
                    candidates.append(record)
                except (WorkflowError, OSError, KeyError, subprocess.SubprocessError) as error:
                    blocked.append({**record, 'reason': str(error)})
            plan = {'id': identifier('cleanup-plan'), 'run_id': run_id, 'candidates': candidates,
                    'blocked': blocked, 'created_at': time.time()}
            plan['digest'] = digest(plan)
            self.store.put(db, 'cleanup_plan', plan)
            self.store.event(db, run_id, 'cleanup_planned', 'Inspect owned resources without removal.',
                             {'plan_id': plan['id'], 'digest': plan['digest']})
            return plan

    def authorize(self, plan_id, plan_digest, reason):
        with self.store.transaction() as db:
            plan = self.store.get(db, 'cleanup_plan', plan_id)
            if plan['digest'] != plan_digest:
                raise WorkflowError('The authorization does not match this plan digest.')
            record = {'id': plan_id, 'run_id': plan['run_id'], 'digest': plan_digest, 'reason': reason}
            self.store.event(db, plan['run_id'], 'cleanup_authorized', reason, record)
            self.store.put(db, 'cleanup_authorization', record)
            return record

    def apply(self, plan_id, reason):
        with self.store.transaction() as db:
            plan = self.store.get(db, 'cleanup_plan', plan_id)
            if digest({key: value for key, value in plan.items() if key != 'digest'}) != plan['digest']:
                raise WorkflowError('The persisted plan digest changed.')
            existing = [item for item in self.store.list(db, 'cleanup', plan['run_id']) if item['plan_id'] == plan_id]
            job = existing[0] if existing else self._create_cleanup_job(db, plan, reason)
        if job['status'] not in {'complete', 'blocked'}:
            self._launch(job['id'])
        return job

    def _require_authorization(self, db, run, plan):
        policy = run['bundle'].get('cleanup_policy', {})
        authorized = policy.get('authorized') is True and policy.get('on_complete') is True
        approvals = self.store.list(db, 'cleanup_authorization', run['id'])
        if not authorized and not any(item['id'] == plan['id'] and item['digest'] == plan['digest'] for item in approvals):
            raise WorkflowError('Cleanup requires policy authorization or approval of this exact plan.')

    @staticmethod
    def _job_resources(plan):
        candidates = [{'resource_id': item['resource_id'], 'path': item['path'], 'stage': 'inspected'}
                      for item in plan['candidates']]
        blocked = [{'resource_id': item['resource_id'], 'path': item['path'], 'stage': 'blocked', 'reason': item['reason']}
                   for item in plan['blocked']]
        return candidates + blocked

    def _create_cleanup_job(self, db, plan, reason):
        run = self.store.get(db, 'run', plan['run_id'])
        self._require_authorization(db, run, plan)
        job = {'id': identifier('cleanup'), 'run_id': run['id'], 'plan_id': plan['id'],
               'status': 'queued', 'created_at': time.time(), 'resources': self._job_resources(plan)}
        for item in plan['candidates']:
            self._inspect(db, run, self.store.get(db, 'cleanup_resource', item['resource_id']))
        for item in plan['candidates']:
            self.store.put(db, 'cleanup_lease', {'id': item['resource_id'], 'run_id': run['id'],
                                               'cleanup_id': job['id'], 'active': True})
        self.store.event(db, run['id'], 'cleanup_started', reason, {'cleanup_id': job['id'], 'plan_id': plan['id']})
        self.store.put(db, 'cleanup', job)
        return job

    def _launch(self, cleanup_id):
        directory = self.store.root / 'cleanup-jobs' / cleanup_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        with (directory / 'worker.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-m', 'ub_execute.cleanup_worker', '--state', str(self.store.root), '--cleanup', cleanup_id],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env)
        threading.Thread(target=process.wait, daemon=True).start()

    def resume(self, cleanup_id):
        job = self.status(cleanup_id)
        if job['status'] not in {'complete', 'blocked'}:
            self._launch(cleanup_id)
        return job

    def status(self, cleanup_id):
        with self.store.transaction() as db:
            return self.store.get(db, 'cleanup', cleanup_id)

    def _save(self, job, item=None):
        with self.store.transaction() as db:
            self.store.put(db, 'cleanup', job)
            if item and item['stage'] in {'reconciled', 'blocked'}:
                self.store.put(db, 'cleanup_lease', {'id': item['resource_id'], 'run_id': job['run_id'],
                                                   'cleanup_id': job['id'], 'active': False})

    def _archive(self, job, item, snapshot):
        archive = create_archive(item['path'], self.store.root / 'cleanup-archives', snapshot, item['resource_id'], job['run_id'])
        archive['plan_id'] = job['plan_id']
        with self.store.transaction() as db:
            self.store.put(db, 'cleanup_archive', archive)
        item.update(archive_id=archive['id'], stage='restore_verified', archive_bytes=archive['archive_bytes'],
                    reclaim_bytes=snapshot['reclaim_bytes'])
        self._save(job)
        return archive

    def _reconcile(self, job, item, resource, snapshot):
        if Path(item['path']).exists() or Path(item['path']).is_symlink():
            return False
        if item['stage'] not in {'removal_started', 'removed', 'reconciled'} or not item.get('archive_id'):
            raise WorkflowError('The worktree disappeared without a checked removal record.')
        archive = self._checked_archive(job, item, snapshot)
        if any(record.get('worktree') == item['path'] for record in registrations(resource['repository'])):
            raise WorkflowError('The removed path still has a Git worktree registration.')
        item['stage'] = 'reconciled'
        with self.store.transaction() as db:
            resource.update(removed=True, archive_id=archive['id'], cleanup_id=job['id'])
            self.store.put(db, 'cleanup_resource', resource)
        self._save(job, item)
        return True

    @staticmethod
    def _check_owned_unlinks(snapshot, current, removed):
        expected = {item['path']: item for item in snapshot['inventory'] if item['path'] not in removed}
        observed = {item['path']: item for item in current}
        if expected.keys() != observed.keys():
            raise WorkflowError('The source changed during registered output removal.')
        for name, original in expected.items():
            allowed = original
            if original['type'] == 'directory' and any(path.startswith(name + '/') for path in removed):
                allowed = {**original, 'mtime_ns': observed[name]['mtime_ns']}
            if observed[name] != allowed:
                raise WorkflowError('The source changed during registered output removal.')

    def _rollback_generated(self, job, item, snapshot, archive):
        current = inventory(Path(item['path']))
        if current == snapshot['inventory']:
            return
        if current != item.get('removal_inventory'):
            raise WorkflowError('The source changed after owned output removal. Preserve it for recovery.')
        removed = item.get('removed_generated', [])
        if not removed:
            raise WorkflowError('The interrupted output state has no checked removal record.')
        manifest = read_archive(archive['directory'], archive, snapshot)
        restore_files(Path(archive['directory']), manifest, Path(item['path']), missing_only=True, selected_paths=removed)
        ancestors = [entry for entry in snapshot['inventory'] if entry['type'] == 'directory'
                     and any(name.startswith(entry['path'] + '/') for name in removed)]
        restore_metadata(Path(item['path']), ancestors)
        if inventory(Path(item['path'])) != snapshot['inventory']:
            raise WorkflowError('Owned output recovery did not restore the checked inventory.')
        item.update(removal_inventory=snapshot['inventory'], removed_generated=[])
        self._save(job)

    def _execute_resource(self, job, item, planned):
        with self.store.transaction() as db:
            run = self.store.get(db, 'run', job['run_id'])
            resource = self.store.get(db, 'cleanup_resource', item['resource_id'])
        snapshot = planned['snapshot']
        if self._reconcile(job, item, resource, snapshot):
            return
        self._resume_generated(job, item, snapshot)
        with self.store.transaction() as db:
            self._require_plan_match(db, run, resource, job, planned, 'The cleanup plan is stale. The source changed.')
        archive = self._recovery_archive(job, item, snapshot)
        self._recheck_plan(job, resource, planned, 'The cleanup plan changed after archive verification.')
        self._start_removal(job, item)
        self._remove_worktree(job, item, planned, resource, archive)
        item['stage'] = 'removed'
        self._save(job)
        self._reconcile(job, item, resource, snapshot)

    def _resume_generated(self, job, item, snapshot):
        if item['stage'] == 'removal_started' and snapshot['generated_paths']:
            prior = self._checked_archive(job, item, snapshot)
            self._rollback_generated(job, item, snapshot, prior)

    @staticmethod
    def _require_archive_context(archive, job, item):
        expected = {'id': item['archive_id'], 'resource_id': item['resource_id'],
                    'run_id': job['run_id'], 'plan_id': job['plan_id']}
        for field, value in expected.items():
            if archive[field] != value:
                raise WorkflowError('The retained archive belongs to another cleanup context: ' + field)
        if archive.get('restore_verified') is not True:
            raise WorkflowError('The recovery archive has no retained restoration proof.')

    def _checked_archive(self, job, item, snapshot):
        with self.store.transaction() as db:
            archive = self.store.get(db, 'cleanup_archive', item['archive_id'])
        self._require_archive_context(archive, job, item)
        read_archive(archive['directory'], archive, snapshot)
        return archive

    def _recovery_archive(self, job, item, snapshot):
        if not item.get('archive_id'):
            self._archive(job, item, snapshot)
        return self._checked_archive(job, item, snapshot)

    def _require_plan_match(self, db, run, resource, job, planned, message):
        current = self._inspect(db, run, resource, job['id'])
        if digest(current) != planned['digest']:
            raise WorkflowError(message)

    def _recheck_plan(self, job, resource, planned, message):
        with self.store.transaction() as db:
            run = self.store.get(db, 'run', job['run_id'])
            self._require_plan_match(db, run, resource, job, planned, message)

    def _start_removal(self, job, item):
        item['stage'] = 'removal_authorized'
        self._save(job)
        item['stage'] = 'removal_started'
        self._save(job)

    def _unlink_generated(self, job, item, snapshot):
        item.update(removal_inventory=snapshot['inventory'], removed_generated=[])
        self._save(job)
        for name in snapshot['generated_paths']:
            (Path(item['path']) / name).unlink()
            removed = item['removed_generated'] + [name]
            intermediate = inventory(Path(item['path']))
            self._check_owned_unlinks(snapshot, intermediate, removed)
            item.update(removal_inventory=intermediate, removed_generated=removed)
            self._save(job)
        if inventory(Path(item['path'])) != item['removal_inventory']:
            raise WorkflowError('The source changed before ordinary Git removal.')

    def _remove_worktree(self, job, item, planned, resource, archive):
        snapshot = planned['snapshot']
        try:
            self._recheck_plan(job, resource, planned, 'The source changed immediately before removal.')
            self._checked_archive(job, item, snapshot)
            self._unlink_generated(job, item, snapshot)
            self._checked_archive(job, item, snapshot)
            git(resource['repository'], 'worktree', 'remove', item['path'])
        except BaseException:
            if Path(item['path']).is_dir() and snapshot['generated_paths']:
                self._rollback_generated(job, item, snapshot, archive)
            raise

    def execute(self, cleanup_id):
        directory = self.store.root / 'cleanup-jobs' / cleanup_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / 'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.status(cleanup_id)
            job = self.status(cleanup_id)
            if job['status'] in {'complete', 'blocked'}:
                return job
            with self.store.transaction() as db:
                plan = self.store.get(db, 'cleanup_plan', job['plan_id'])
            planned = {item['resource_id']: item for item in plan['candidates']}
            job['status'] = 'running'
            self._save(job)
            for item in job['resources']:
                if item['stage'] in {'reconciled', 'blocked'}:
                    continue
                try:
                    self._execute_resource(job, item, planned[item['resource_id']])
                except (WorkflowError, OSError, KeyError, subprocess.SubprocessError) as error:
                    item.update(stage='blocked', reason=str(error))
                    self._save(job, item)
            job['status'] = 'blocked' if any(item['stage'] == 'blocked' for item in job['resources']) else 'complete'
            job['finished_at'] = time.time()
            self._save(job)
            return job

    def _archive_snapshot(self, db, archive):
        plan = self.store.get(db, 'cleanup_plan', archive['plan_id'])
        if digest({key: value for key, value in plan.items() if key != 'digest'}) != plan['digest']:
            raise WorkflowError('The retained cleanup plan digest changed.')
        if plan['run_id'] != archive['run_id']:
            raise WorkflowError('The retained archive belongs to another cleanup plan.')
        for candidate in plan['candidates']:
            if candidate['resource_id'] == archive['resource_id']:
                if digest(candidate['snapshot']) != candidate['digest']:
                    raise WorkflowError('The retained candidate snapshot digest changed.')
                return candidate['snapshot']
        raise WorkflowError('The retained archive has no candidate in its cleanup plan.')

    def restore(self, archive_id, destination):
        with self.store.transaction() as db:
            archive = self.store.get(db, 'cleanup_archive', archive_id)
            snapshot = self._archive_snapshot(db, archive)
        if archive['id'] != archive_id or archive.get('restore_verified') is not True:
            raise WorkflowError('The recovery archive has no matching retained restoration proof.')
        result = restore_archive(archive['directory'], destination, archive, snapshot)
        with self.store.transaction() as db:
            self.store.event(db, archive['run_id'], 'cleanup_restored', 'Restore checked files into an absent destination.', result)
        return result
