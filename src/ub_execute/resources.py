"""Register exact generated files and borrowed service access."""

import hashlib
import os
import time
from pathlib import Path

from .contracts import names, need, text
from .store import identifier
from .workspace import fingerprint, git, inside


def register_artifacts(runtime, run_id, resource_id, paths, reason):
    names(paths, 'generated file paths')
    text(reason, 'the producing operation and ownership reason')
    with runtime.store.transaction() as db:
        run = runtime._run(db, run_id, active=True)
        resource = runtime.store.get(db, 'cleanup_resource', resource_id)
        need(resource['run_id'] == run_id and resource['ownership'] == 'owned', 'Register files only for an owned run resource.')
        tracked = set(git(resource['path'], 'ls-files', '-z').split('\0'))
        generated = resource.setdefault('generated_artifacts', {})
        for name in paths:
            path = inside(resource['path'], name)
            need(name not in tracked and not path.is_symlink() and path.is_file(), 'Register an exact regular untracked file.')
            with path.open('rb') as stream:
                generated[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        runtime.store.put(db, 'cleanup_resource', resource)
        runtime.store.event(db, run_id, 'artifacts_registered', reason, {'resource_id':resource_id, 'paths':paths})
        return {'resource_id':resource_id, 'generated_artifacts':generated}


def register_service(runtime, attempt_id, service, reason):
    need(isinstance(service, dict), 'Provide a child service record.')
    need(set(service) <= {'pid','origin','port','environment_dependencies'}, 'The child service contains unsupported fields.')
    need(type(service.get('pid')) is int and service['pid'] > 1, 'Provide the owned service process ID.')
    text(reason, 'the service ownership reason')
    with runtime.store.transaction() as db:
        attempt = runtime.store.get(db, 'attempt', attempt_id)
        run = runtime._run(db, attempt['run_id'], active=True)
        need(attempt['status'] == 'running', 'Start services only for a running attempt.')
        need(run.get('workspace_id'), 'Attach a parent before registering a borrowed child service.')
        app = attempt.get('parent_environment', {}).get('app', {})
        need(service.get('origin') == app.get('origin') and service.get('port') == app.get('port') and app, 'Use the assigned child port and origin.')
        need(_alive(service['pid']), 'The owned service process is not running.')
        need(not attempt.get('app_server'), 'Release the previous child server first.')
        record = {'id':identifier('service'), 'run_id':run['id'], 'resource_id':attempt_id, 'attempt_id':attempt_id, 'status':'running', 'started_at':time.time(), **service,
                  'worktree':attempt['worktree'], 'commit':git(attempt['worktree'],'rev-parse','HEAD'), 'fingerprint':fingerprint(attempt['worktree'])}
        attempt['app_server'] = {key:record[key] for key in ('id','pid','origin','port','commit','fingerprint')}
        runtime.store.put(db, 'attempt', attempt)
        workspace = runtime.store.get(db, 'parent_workspace', run['workspace_id'])
        dependencies = ['dependencies','http'] + (['database'] if workspace['database_identity'] else [])
        record['check'] = {'argv':[], 'environment_dependencies':service.get('environment_dependencies', dependencies)}
        runtime._parent_manager().acquire_check(db, run, record, attempt)
        runtime.store.put(db, 'service', record)
        runtime.store.event(db, run['id'], 'service_registered', reason, {'service_id':record['id'], 'attempt_id':attempt_id})
        return record


def release_service(runtime, service_id, reason):
    text(reason, 'the service stop and release reason')
    with runtime.store.transaction() as db:
        record = runtime.store.get(db, 'service', service_id)
        runtime._mutable(db, record['run_id'])
        need(not _alive(record['pid']), 'Stop the owned service before releasing its environment lease.')
        if record['status'] != 'released':
            runtime._parent_manager().release_check(db, record)
            record['status'] = 'released'
            runtime.store.put(db, 'service', record)
            attempt = runtime.store.get(db, 'attempt', record['attempt_id'])
            attempt.pop('app_server', None)
            runtime.store.put(db, 'attempt', attempt)
            runtime.store.event(db, record['run_id'], 'service_released', reason, {'service_id':service_id})
        return record


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
