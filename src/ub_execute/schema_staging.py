"""Journal schema-only Git integration before coordinator database application."""

import json
from pathlib import Path

from .contracts import need
from .workspace import changed_paths, git, require_clean, commit_paths


def stage(runtime, record, workspace):
    manager = runtime._parent_manager()
    key = 'integration:' + workspace['common_dir'] + ':' + workspace['parent_path']
    lease = manager.leases.acquire([(key, 'exclusive')], {'proposal_id':record['id'], 'operation':'schema_stage'})
    try:
        return _stage(runtime, record, workspace)
    finally:
        manager.leases.release(lease)


def _stage(runtime, record, workspace):
    store, tree = runtime.store, workspace['parent_path']
    original = record['proposal']['commit']
    with store.transaction() as db:
        run = runtime._run(db, record['run_id'])
        runtime._mutable(db, run['id'])
        need(run['status'] == 'active', 'Resume the run before schema application.')
        need(run['worktree'] == tree, 'Use the prepared parent as the proposal integration target.')
        need(not run.get('integrating_task'), 'Reconcile the pending task integration first.')
        require_clean(tree)
        all_runs = [json.loads(row[0]) for row in db.execute("SELECT data FROM objects WHERE kind='run'")]
        for attached in all_runs:
            if attached.get('workspace_id') != workspace['id']:
                continue
            for job in store.list(db, 'job', attached['id']):
                need(not (job['worktree'] == tree and job['status'] in {'queued','running'}), 'A check still owns the parent integration worktree.')
        head = git(tree, 'rev-parse', 'HEAD')
        staged = run.get('provisional_commits', {}).get(original)
        if staged:
            git(tree, 'merge-base', '--is-ancestor', staged, 'HEAD')
        else:
            intent = run.get('schema_staging')
            if intent:
                need(intent['proposal_id'] == record['id'], 'Reconcile the pending schema staging intent first.')
            else:
                need(head == run['integration_commit'], 'Reconcile the parent integration revision first.')
                intent = {'proposal_id':record['id'], 'before':head, 'original':original}
                run['schema_staging'] = intent
                store.put(db, 'run', run)
                store.event(db, run['id'], 'schema_stage_intent', 'Integrate the reviewed schema proposal.', intent)
    if not staged:
        staged = _finish_cherry_pick(tree, intent)
    with store.transaction() as db:
        current = store.get(db, 'schema_proposal', record['id'])
        current['staged_commit'] = staged
        store.put(db, 'schema_proposal', current)
        runtime._advance_parent(db, run, intent['before'] if not run.get('provisional_commits', {}).get(original) else run['integration_commit'], staged)
        run = store.get(db, 'run', run['id'])
        run.setdefault('provisional_commits', {})[original] = staged
        run.pop('schema_staging', None)
        store.put(db, 'run', run)
        store.event(db, run['id'], 'schema_staged', 'Record the integrated schema proposal.', {'proposal_id':record['id'], 'original':original, 'staged_commit':staged})
    return staged


def _finish_cherry_pick(tree, intent):
    marker = Path(git(tree, 'rev-parse', '--git-path', 'CHERRY_PICK_HEAD'))
    if not marker.is_absolute():
        marker = Path(tree) / marker
    need(not marker.exists(), 'Finish or abort the schema cherry-pick before retrying.')
    before, original = intent['before'], intent['original']
    head = git(tree, 'rev-parse', 'HEAD')
    if head == before:
        git(tree, 'cherry-pick', '-x', original)
        return git(tree, 'rev-parse', 'HEAD')
    commits = git(tree, 'rev-list', before + '..HEAD').splitlines()
    need(len(commits) == 1, 'The schema staging history contains unexpected commits.')
    need(original in git(tree, 'log', '-1', '--format=%B'), 'The schema staging commit marker is missing.')
    expected = set(commit_paths(tree, original))
    need(set(changed_paths(tree, before)) == expected, 'The staged schema paths differ from the reviewed proposal.')
    return head
