"""Parent environment and cleanup operations for the execution runtime."""

import json
from pathlib import Path

from .cleanup import CleanupManager
from .contracts import need, text
from .parent import ParentManager
from .workspace import git, require_clean, selected, commit_paths


class Lifecycle:
    def _mutable(self, db, run_id):
        leases = self.store.list(db, 'cleanup_lease', run_id)
        need(not any(item.get('active') for item in leases), 'A cleanup lease blocks runtime changes.')

    def workspace_attach(self, parent_path, config, reason):
        text(reason, 'an action reason')
        return ParentManager(self).attach(parent_path, config, reason)

    def workspace_context(self, workspace_id):
        return ParentManager(self).context(workspace_id)

    def workspace_refresh(self, run_id, attempt_id, reason):
        with self.store.transaction() as db:
            self._run(db, run_id, active=True)
        return ParentManager(self).refresh(run_id, attempt_id, reason)

    def schema_change_request(self, attempt_id, proposal, reason):
        with self.store.transaction() as db:
            attempt = self.store.get(db, 'attempt', attempt_id)
            run = self._run(db, attempt['run_id'], active=True)
            need(attempt['status'] == 'running', 'Only a running attempt can propose a schema change.')
            need(isinstance(proposal, dict), 'Provide a schema proposal object.')
            text(proposal.get('commit'), 'the committed schema proposal')
            task = self._task(db, run['id'], attempt['task_id'])
            need(all(selected(path, task['scope']) for path in commit_paths(attempt['worktree'], proposal['commit'])), 'The schema proposal changes files outside the task scope.')
        return ParentManager(self).schema_request(attempt_id, proposal, reason)

    def schema_review(self, proposal_id, reviewer_id, reason):
        with self.store.transaction() as db:
            proposal = self.store.get(db, 'schema_proposal', proposal_id)
            self._run(db, proposal['run_id'], active=True)
        return ParentManager(self).schema_review(proposal_id, reviewer_id, reason)

    def coordinator_apply(self, proposal_id, capability, reason):
        """Apply a reviewed proposal through the CLI coordinator boundary."""
        with self.store.transaction() as db:
            proposal = self.store.get(db, 'schema_proposal', proposal_id)
            run = self._run(db, proposal['run_id'])
            self._mutable(db, run['id'])
            need(run['status'] == 'active', 'Resume the run before schema application.')
        return ParentManager(self).schema_apply(proposal_id, capability, reason, stage=self._schema_stage)

    def coordinator_recover(self, workspace_id, reason, resolution=None):
        with self.store.transaction() as db:
            workspace = self.store.get(db, 'parent_workspace', workspace_id)
            records = [json.loads(row[0]) for row in db.execute("SELECT data FROM objects WHERE kind='schema_proposal'")]
            pending = [record for record in records if record['workspace_id'] == workspace_id and record['status'] == 'applying']
            journals = [(record, self.store.get(db, 'run', record['run_id']).get('schema_staging')) for record in pending]
        for record, journal in journals:
            if journal and git(workspace['parent_path'], 'rev-parse', 'HEAD') != journal['before']:
                self._schema_stage(record, workspace)
        return ParentManager(self).recover(workspace_id, reason, resolution)

    def _provisional_task(self, db, run, attempt, task):
        records = [record for record in self.store.list(db, 'schema_proposal', run['id'])
                   if record['attempt_id'] == attempt['id'] and record['status'] == 'applied']
        for record in records:
            original, staged = record['proposal']['commit'], record['staged_commit']
            need(run.get('provisional_commits', {}).get(original) == staged, 'The provisional schema mapping changed.')
            git(attempt['worktree'], 'merge-base', '--is-ancestor', staged, 'HEAD')
            git(run['worktree'], 'merge-base', '--is-ancestor', staged, 'HEAD')
            need(all(selected(path, task['scope']) for path in commit_paths(attempt['worktree'], original)), 'The applied schema changes files outside task scope.')
        return bool(records)

    def _remaining_commits(self, db, run, attempt, task):
        tree = run['worktree']
        commits = git(tree, 'rev-list', '--reverse', attempt['base_commit'] + '..' + attempt['candidate_commit']).splitlines()
        need(commits or self._provisional_task(db, run, attempt, task), 'The accepted candidate contains no commits.')
        provisional = run.get('provisional_commits', {})
        for original in set(commits) & set(provisional):
            git(tree, 'merge-base', '--is-ancestor', provisional[original], 'HEAD')
        return [commit for commit in commits if commit not in provisional]

    def task_integrate(self, run_id, task_id, reason):
        return self._integration_operation(run_id, lambda: self._task_integrate(run_id, task_id, reason))

    def integration_reconcile(self, run_id, reason):
        return self._integration_operation(run_id, lambda: self._integration_reconcile(run_id, reason))

    def _integration_operation(self, run_id, operation):
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            self._mutable(db, run_id)
            workspace = self.store.get(db, 'parent_workspace', run['workspace_id']) if run.get('workspace_id') else None
        if workspace is None:
            return operation()
        manager = ParentManager(self)
        resource = 'integration:' + workspace['common_dir'] + ':' + workspace['parent_path']
        lease = manager.leases.acquire([(resource, 'exclusive')], {'run_id':run_id,'operation':'integration'})
        try:
            return operation()
        finally:
            manager.leases.release(lease)

    def _parent_manager(self):
        return ParentManager(self)

    def _schema_stage(self, record, workspace):
        from .schema_staging import stage
        return stage(self, record, workspace)

    def _advance_parent(self, db, run, before, head):
        if not run.get('workspace_id'):
            run['integration_commit'] = head
            self.store.put(db, 'run', run)
            return
        for row in db.execute("SELECT data FROM objects WHERE kind='run'").fetchall():
            attached = json.loads(row[0])
            if attached.get('workspace_id') != run['workspace_id'] or attached['status'] in {'complete', 'cancelled'}:
                continue
            need(attached['integration_commit'] in {before, head}, 'Another run requires parent revision reconciliation.')
            attached['integration_commit'] = head
            self.store.put(db, 'run', attached)
        workspace = self.store.get(db, 'parent_workspace', run['workspace_id'])
        workspace['source_commit'] = head
        self.store.put(db, 'parent_workspace', workspace)
        run['integration_commit'] = head

    def _recover_cleanup(self, run_id):
        with self.store.transaction() as db:
            jobs = self.store.list(db, 'cleanup', run_id)
        manager = CleanupManager(self)
        for job in jobs:
            if job['status'] not in {'complete', 'blocked'}:
                manager.resume(job['id'])
        return [self.cleanup_status(job['id']) for job in jobs]

    def artifact_register(self, run_id, resource_id, paths, reason):
        from .resources import register_artifacts
        return register_artifacts(self, run_id, resource_id, paths, reason)

    def service_register(self, attempt_id, service, reason):
        from .resources import register_service
        return register_service(self, attempt_id, service, reason)

    def service_release(self, service_id, reason):
        from .resources import release_service
        return release_service(self, service_id, reason)

    def cleanup_plan(self, run_id):
        return CleanupManager(self).plan(run_id)

    def cleanup_authorize(self, plan_id, plan_digest, reason):
        return CleanupManager(self).authorize(plan_id, plan_digest, reason)

    def cleanup_apply(self, plan_id, reason):
        return CleanupManager(self).apply(plan_id, reason)

    def cleanup_status(self, cleanup_id):
        result = CleanupManager(self).status(cleanup_id)
        with self.store.transaction() as db:
            for item in result['resources']:
                if item['stage'] == 'reconciled':
                    resource = self.store.get(db, 'cleanup_resource', item['resource_id'])
                    if resource.get('attempt_id'):
                        attempt = self.store.get(db, 'attempt', resource['attempt_id'])
                        ParentManager(self).release_child(db, attempt)
        return result

    def cleanup_restore(self, archive_id, destination):
        return CleanupManager(self).restore(archive_id, destination)

    def writer_release(self, run_id, resource_id, agent_id, reason):
        text(reason, 'the writer and service release reason')
        return CleanupManager(self).release(run_id, resource_id, agent_id, reason)

    def _auto_cleanup(self, run):
        policy = run['bundle'].get('cleanup_policy', {})
        if policy.get('authorized') is not True or policy.get('on_complete') is not True:
            return None
        try:
            plan = self.cleanup_plan(run['id'])
            return self.cleanup_apply(plan['id'], 'Apply the authorized completion cleanup policy.')
        except Exception as error:
            with self.store.transaction() as db:
                self.store.event(db, run['id'], 'cleanup_enqueue_failed', 'Preserve the completed execution result.', {'error': str(error)})
            return {'status': 'blocked', 'reason': str(error)}
