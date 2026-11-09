"""Attach prepared parents and lend their environment to isolated task worktrees."""

import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from .store import WorkflowError, identifier
from .workspace import git, require_clean, relative_path, selected, fingerprint
from .parent_database import (DATABASE_KEYS, database_identity, inspect_database, private_write,
                              provision, read_environment, refresh_grants, sanitized_environment, schema_command)
from .parent_leases import ParentLeases
from .node_runtime import inspect_node, VERSION_FILES


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def digest_files(root, paths):
    result = {}
    for name in paths:
        path = Path(root) / name
        require(not path.is_symlink() and path.is_file(), 'A dependency manifest is missing or substituted.')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def common_dir(root):
    return str((Path(root) / git(root, 'rev-parse', '--git-common-dir')).resolve())


class ParentManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.leases = ParentLeases()

    def _directory(self, workspace_id):
        return self.store.root / 'parents' / workspace_id

    def _config(self, workspace):
        return read_environment_config(self._directory(workspace['id']) / 'config.json')

    def _workspace(self, db, workspace_id):
        return self.store.get(db, 'parent_workspace', workspace_id)

    def _identity(self, workspace):
        path = workspace['parent_path']
        require(str(Path(path).resolve()) == git(path,'rev-parse','--show-toplevel'), 'The parent path is no longer its Git root.')
        require(common_dir(path) == workspace['common_dir'], 'The parent Git identity changed.')
        require(git(path,'symbolic-ref','--short','HEAD') == workspace['branch'], 'The parent branch changed.')

    def attach(self, parent_path, config, reason):
        require(isinstance(config,dict), 'Provide parent configuration as an object.')
        require(isinstance(reason,str) and reason.strip(), 'Provide an attachment reason.')
        path = str(Path(parent_path).expanduser().resolve())
        require(git(path,'rev-parse','--show-toplevel') == path, 'Use the prepared parent Git root.')
        require_clean(path)
        branch = git(path,'symbolic-ref','--short','HEAD')
        identity = common_dir(path)
        workspace_id = 'workspace-' + hashlib.sha256((identity + '\0' + path).encode()).hexdigest()[:24]
        directory = self._directory(workspace_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory,0o700)
        config = self._attachment_config(path,config)
        resolved_dependency = self._attachment_dependencies(path,config)
        database = self._attachment_database(path,config,directory)
        probe = inspect_database(database,path)
        manifests = digest_files(path,config['manifest_paths'])
        node = inspect_node(path,resolved_dependency)
        with self.store.transaction() as db:
            old = self._previous_attachment(db,workspace_id,probe,manifests,node)
            workspace = self._attachment_record(workspace_id,path,identity,branch,resolved_dependency,manifests,old,probe,database,node)
            self._save_attachment_config(directory,config)
            self.store.put(db,'parent_workspace',workspace)
            self.store.event(db,workspace_id,'workspace_attached',reason,{'workspace_id':workspace_id})
        return self.context(workspace_id)

    def _attachment_config(self,path,config):
        config = dict(config)
        config.setdefault('adapter','generic')
        require(config['adapter'] in {'generic','backkr'}, 'Use the generic or Backkr parent adapter.')
        paths = config.get('manifest_paths', [name for name in ('package.json','package-lock.json','yarn.lock','pnpm-lock.yaml',*VERSION_FILES) if (Path(path)/name).is_file()])
        require(isinstance(paths,list), 'Use relative dependency manifest paths.')
        require(all(self._valid_manifest_path(name) for name in paths), 'Use relative dependency manifest paths.')
        config['manifest_paths'] = paths
        self._check_local_packages(path)
        return config

    @staticmethod
    def _valid_manifest_path(name):
        return isinstance(name,str) and not Path(name).is_absolute() and '..' not in Path(name).parts

    @staticmethod
    def _check_local_packages(path):
        package = Path(path)/'package.json'
        if not package.exists():
            return
        manifest = json.loads(package.read_text())
        require(not manifest.get('workspaces'), 'Linked workspace packages require a child source adapter.')
        for group in ('dependencies','devDependencies','optionalDependencies'):
            require(all(not str(value).startswith(('file:','link:','workspace:')) for value in manifest.get(group,{}).values()), 'A linked local package requires a child source adapter.')

    @staticmethod
    def _attachment_dependencies(path,config):
        dependency = Path(path) / config.get('dependency_dir','node_modules')
        require(dependency.is_dir() or not config['manifest_paths'], 'The prepared parent dependency installation is missing.')
        if not dependency.is_dir():
            return None
        for entry in dependency.rglob('*'):
            if entry.is_symlink():
                require(entry.resolve().is_relative_to(dependency.resolve()), 'A local package resolves outside the dependency installation.')
        return str(dependency.resolve())

    @staticmethod
    def _attachment_database(path,config,directory):
        database = config.get('database')
        if not database:
            return database
        database = dict(database)
        database.setdefault('admin_env_file',str(Path(path)/'.env'))
        if not database.get('expected_identity'):
            admin = read_environment(database['admin_env_file'])
            database['expected_identity'] = database_identity(admin.get('DATABASE_URL'))
        database = provision(database,directory)
        config['database'] = database
        return database

    def _previous_attachment(self,db,workspace_id,probe,manifests,node):
        try:
            old = self._workspace(db,workspace_id)
        except WorkflowError:
            return None
        require(old['status'] == 'ready', 'Reconcile the parent before attachment.')
        require(old['database_identity'] == (probe['identity'] if probe else None), 'The attached database identity changed.')
        if self._dependency_changed(old,manifests,node):
            lease = self.leases.acquire([(self._dependency_resource(old),'exclusive')],{'workspace_id':workspace_id,'operation':'dependency_update'})
            self.leases.release(lease)
        if probe and old['schema_fingerprint'] != probe['schema_fingerprint']:
            raise WorkflowError('The actual schema changed outside the coordinator. Reconcile the parent.')
        return old

    @staticmethod
    def _attachment_generations(old,probe,manifests,node):
        if old:
            return {'dependency_generation':old['dependency_generation'] + ParentManager._dependency_changed(old,manifests,node),
                    'schema_generation':old['schema_generation'],'data_generation':old.get('data_generation',0)}
        return {'dependency_generation':1,'schema_generation':1 if probe else 0,'data_generation':0}

    @staticmethod
    def _dependency_changed(old,manifests,node):
        return old['manifest_digests'] != manifests or old.get('node_runtime') != node

    def _attachment_record(self,workspace_id,path,identity,branch,target,manifests,old,probe,database,node):
        enforcement = 'none'
        if database:
            enforcement = 'custom_probe' if database.get('probe_argv') else 'postgres_role'
        return {'id':workspace_id,'run_id':workspace_id,'parent_path':path,'common_dir':identity,'branch':branch,
                'source_commit':git(path,'rev-parse','HEAD'),'dependency_target':target,
                'manifest_digests':manifests,'node_runtime':node,**self._attachment_generations(old,probe,manifests,node),
                'schema_fingerprint':probe['schema_fingerprint'] if probe else None,
                'database_identity':probe['identity'] if probe else None,
                'capability_level':'cooperative','database_enforcement':enforcement,'status':'ready'}

    @staticmethod
    def _save_attachment_config(directory,config):
        private_write(directory/'config.json',config)
        capability_file = directory/'coordinator.capability'
        if not capability_file.exists():
            private_write(capability_file,secrets.token_urlsafe(48))
            capability_file.write_text(json.loads(capability_file.read_text()))

    def _dependency_resource(self, workspace):
        return 'dependency:' + str(workspace['dependency_target'] or workspace['parent_path'])

    def _database_resource(self, workspace):
        identity = dict(workspace['database_identity']) if workspace['database_identity'] else None
        if identity and identity.get('host') in {'localhost','127.0.0.1','::1'}:
            identity['host'] = 'loopback'
        return 'database:' + json.dumps(identity,sort_keys=True)

    def context(self, workspace_id):
        with self.store.transaction() as db:
            workspace = self._workspace(db,workspace_id)
        if workspace['database_identity']:
            workspace['data_generation'] = self.leases.data_generation(self._database_resource(workspace))
        resources = [self._dependency_resource(workspace),self._database_resource(workspace)]
        return {**workspace,'leases':self.leases.active(resources), 'isolation_notice':'Native agents share operating system access. Database roles restrict the supplied worker credential. A separate sandbox or broker is required for hard isolation.'}

    def prepare_child(self, db, run, attempt):
        if not run.get('workspace_id'):
            return None
        workspace = self._workspace(db,run['workspace_id'])
        self._identity(workspace)
        require(workspace['status'] == 'ready', 'The parent environment requires reconciliation.')
        config = self._config(workspace)
        child = Path(attempt['worktree']).resolve()
        self._check_child_identity(child,workspace,config)
        links = self._child_links(child,workspace,config)
        private_dir = self._child_private_directory(child,config)
        values = self._child_database_values(config,workspace)
        app = self._child_app(config,attempt)
        if app:
            values.update({'PORT':str(app['port']),'NEXT_PUBLIC_APP_URL':app['origin'],'APP_URL':app['origin']})
        values.update({'NEXT_BUILD_DIR':str(child/'.next'),'XDG_CACHE_HOME':str(private_dir/'cache'), 'npm_config_cache':str(private_dir/'npm-cache'),'UB_CRON_DISABLED':'1'})
        environment_file = private_dir/'worker.env.json'
        require(not environment_file.exists() or attempt.get('parent_environment',{}).get('workspace_id') == workspace['id'], 'Do not overwrite an existing private child environment.')
        private_write(environment_file,values)
        attempt['parent_environment'] = {'workspace_id':workspace['id'],'dependency_generation':workspace['dependency_generation'],'schema_generation':workspace['schema_generation'],'source_commit':workspace['source_commit'],'links':links,'ownership':'borrowed','released':False,'app':app,'node_runtime':workspace.get('node_runtime')}
        return attempt['parent_environment']

    @staticmethod
    def _check_child_identity(child,workspace,config):
        require(common_dir(child) == workspace['common_dir'], 'The child belongs to another Git repository.')
        require(child != Path(workspace['parent_path']), 'The parent cannot be its own child.')
        require(inspect_node(workspace['parent_path'],workspace['dependency_target']) == workspace.get('node_runtime'), 'Reattach the changed Node runtime before preparing a child.')
        require(digest_files(child,config['manifest_paths']) == workspace['manifest_digests'], 'The child dependency manifests differ from the prepared installation.')
        require(digest_files(workspace['parent_path'],config['manifest_paths']) == workspace['manifest_digests'], 'The parent dependency manifest generation changed.')

    @staticmethod
    def _child_links(child,workspace,config):
        if not workspace['dependency_target']:
            return []
        link = child / config.get('dependency_dir','node_modules')
        require(link.parent.resolve().is_relative_to(child), 'The dependency link escapes the child.')
        if link.is_symlink():
            require(str(link.resolve()) == workspace['dependency_target'], 'Do not overwrite an existing dependency link.')
        else:
            require(not link.exists(), 'Do not overwrite an existing dependency target.')
            link.symlink_to(workspace['dependency_target'],target_is_directory=True)
        return [{'path':str(link),'target':workspace['dependency_target'],'ownership':'borrowed'}]

    def _child_private_directory(self,child,config):
        private_dir = child / '.ub-parent'
        require(not private_dir.is_symlink(), 'The child private directory must not be a symlink.')
        private_dir.mkdir(exist_ok=True,mode=0o700)
        os.chmod(private_dir,0o700)
        self._ignore_child_outputs(child,config)
        return private_dir

    @staticmethod
    def _ignore_child_outputs(child,config):
        # The shared info exclude contains output paths without private values.
        exclude = Path(git(child,'rev-parse','--git-path','info/exclude'))
        if not exclude.is_absolute():
            exclude = child / exclude
        exclude.parent.mkdir(parents=True,exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ''
        ignores = ['.ub-parent/',config.get('dependency_dir','node_modules'),'.next/']
        with exclude.open('a') as stream:
            for pattern in ignores:
                if pattern not in existing.splitlines():
                    stream.write('\n' + pattern + '\n')

    @staticmethod
    def _child_database_values(config,workspace):
        if not config.get('database'):
            return {}
        inspect_database(config['database'],workspace['parent_path'])
        supplied = read_environment(config['database']['worker_env_file'])
        return {key:value for key,value in supplied.items() if key in DATABASE_KEYS or key.endswith('DATABASE_URL')}

    def _child_app(self,config,attempt):
        app = config.get('app',{})
        if not app:
            return None
        prior = attempt.get('parent_environment',{}).get('app')
        if prior:
            return prior
        base,count = self._child_port_range(app)
        parsed = self._child_origin(app)
        return self._lease_child_port(attempt,base,count,parsed)

    @staticmethod
    def _child_port_range(app):
        base = app.get('port',3000)
        count = app.get('port_count',1000)
        require(isinstance(base,int) and isinstance(count,int) and base > 0 and count > 0 and base + count <= 65536, 'Assign a valid child app port range.')
        return base,count

    @staticmethod
    def _child_origin(app):
        parsed = urlsplit(app.get('origin','http://127.0.0.1'))
        require(parsed.scheme == 'http' and parsed.hostname in {'localhost','127.0.0.1','::1'} and not parsed.username, 'Assign a local child app origin.')
        return parsed

    def _lease_child_port(self,attempt,base,count,parsed):
        offset = int(hashlib.sha256(attempt['id'].encode()).hexdigest()[:8],16) % count
        for index in range(count):
            port = base + (offset + index) % count
            try:
                lease = self.leases.acquire([('app-port:' + str(port),'exclusive')],{'attempt_id':attempt['id'],'state':str(self.store.root)})
                host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
                return {'port':port,'origin':urlunsplit(('http',host + ':' + str(port),'','','')),'lease_id':lease}
            except WorkflowError:
                continue
        raise WorkflowError('The child app port range has no available lease.')

    def _default_dependencies(self,workspace):
        return ['dependencies'] + (['database'] if workspace['database_identity'] else [])

    def environment_evidence(self, db, run, attempt=None, dependencies=None):
        if not run.get('workspace_id'):
            return None
        workspace = self._workspace(db,run['workspace_id'])
        self._identity(workspace)
        dependencies = self._default_dependencies(workspace) if dependencies is None else dependencies
        require(isinstance(dependencies,list) and set(dependencies) <= {'source','dependencies','database','data','http'}, 'Unknown check environment dependency.')
        require(workspace['status'] == 'ready', 'The parent environment requires reconciliation.')
        config = self._config(workspace)
        actual_source = git(workspace['parent_path'],'rev-parse','HEAD')
        if 'dependencies' in dependencies:
            self._check_dependency_generation(workspace,config,attempt)
        if set(dependencies) & {'database','data'}:
            self._check_database_generation(workspace,config)
        self._check_child_generations(workspace,attempt,dependencies)
        if 'data' in dependencies:
            workspace['data_generation'] = self.leases.data_generation(self._database_resource(workspace))
        evidence = self._capture_environment(workspace,dependencies,actual_source)
        if 'http' in dependencies:
            evidence.update(self._capture_http(attempt))
        return evidence

    @staticmethod
    def _check_dependency_generation(workspace,config,attempt):
        require(digest_files(workspace['parent_path'],config['manifest_paths']) == workspace['manifest_digests'], 'The actual dependency generation changed. Attach the updated installation.')
        require(inspect_node(workspace['parent_path'],workspace['dependency_target']) == workspace.get('node_runtime'), 'The actual Node runtime changed. Reattach the parent environment.')
        if workspace['dependency_target']:
            require(str((Path(workspace['parent_path'])/config.get('dependency_dir','node_modules')).resolve()) == workspace['dependency_target'], 'The shared dependency target changed.')
            if attempt:
                child_link = Path(attempt['worktree'])/config.get('dependency_dir','node_modules')
                require(child_link.is_symlink() and str(child_link.resolve()) == workspace['dependency_target'], 'The child dependency link changed.')

    @staticmethod
    def _check_database_generation(workspace,config):
        require(workspace['database_identity'] is not None,'This check requires an attached database.')
        probe = inspect_database(config['database'],workspace['parent_path'])
        require(probe['schema_fingerprint'] == workspace['schema_fingerprint'], 'The actual database schema changed. Reconcile the parent.')

    @staticmethod
    def _check_child_generations(workspace,attempt,dependencies):
        child_context = attempt.get('parent_environment') if attempt else None
        if child_context:
            for name,key in [('dependencies','dependency_generation'),('database','schema_generation'),('data','schema_generation')]:
                if name in dependencies:
                    require(child_context[key] == workspace[key], 'Refresh the stale child environment before this check.')

    @staticmethod
    def _capture_environment(workspace,dependencies,actual_source):
        fields = {'source':['source_commit'],'dependencies':['dependency_generation','manifest_digests','dependency_target','node_runtime'],
                  'database':['database_identity','schema_generation','schema_fingerprint'],'data':['data_generation','schema_generation'], 'http':['source_commit']}
        evidence = {'workspace_id':workspace['id'],'dependencies':dependencies}
        for dependency in dependencies:
            for key in fields[dependency]:
                evidence[key] = actual_source if key == 'source_commit' else workspace[key]
        return evidence

    @staticmethod
    def _capture_http(attempt):
        require(attempt is not None and attempt.get('app_server'), 'HTTP evidence requires an owned child app server.')
        server = attempt['app_server']
        if 'fingerprint' in server:
            require(server['fingerprint'] == fingerprint(attempt['worktree']) and server['commit'] == git(attempt['worktree'],'rev-parse','HEAD'), 'Restart the child server after its source revision changes.')
        return {'attempt_id':attempt['id'],'app_server':json.loads(json.dumps(server)),
                'child_commit':git(attempt['worktree'],'rev-parse','HEAD')}

    def validate_evidence(self, db, run, evidence):
        if evidence is None:
            return not run.get('workspace_id')
        try:
            attempt = self.store.get(db,'attempt',evidence['attempt_id']) if evidence.get('attempt_id') else None
            current = self.environment_evidence(db,run,attempt,evidence['dependencies'])
            return current == evidence
        except (WorkflowError,OSError,KeyError):
            return False

    def acquire_check(self, db, run, job, attempt=None):
        if not run.get('workspace_id'):
            return
        argv = job['check'].get('argv',[])
        blocked = ('db:push','db:migrate','migrate','migration','prisma','drizzle-kit','createdb','dropdb','psql','install','update','prune','rebuild','codegen')
        require(not any(any(token == word or token.startswith(word + ':') for word in blocked) for token in argv), 'Schema and shared dependency commands require the parent coordinator.')
        workspace = self._workspace(db,run['workspace_id'])
        dependencies = job['check'].get('environment_dependencies')
        job['environment'] = self.environment_evidence(db,run,attempt,dependencies)
        resources = self._check_resources(workspace,job['environment']['dependencies'])
        owner = self._lease_owner(job)
        lease_id = self.leases.acquire(resources,owner)
        job['environment_lease_id'] = lease_id
        job['environment_lease_claim'] = {'id':lease_id,'resources':[list(item) for item in resources],'owner':owner}
        job['environment_lease_verified'] = False

    def _check_resources(self,workspace,names):
        resources = []
        if 'dependencies' in names:
            resources.append((self._dependency_resource(workspace),'shared'))
        if set(names) & {'database','data'}:
            resources.append((self._database_resource(workspace),'exclusive' if 'data' in names else 'shared'))
        return resources

    def _lease_owner(self,job):
        return {'job_id':job['id'],'run_id':job['run_id'],'state':str(self.store.root)}

    def _owns_claim(self,job):
        claim = job.get('environment_lease_claim') or {}
        if claim.get('owner') != self._lease_owner(job):
            return False
        if claim.get('id') != job.get('environment_lease_id'):
            return False
        return self.leases.owns(claim['id'],claim['resources'],claim['owner'])

    def check_lease(self,db,run,job):
        if not run.get('workspace_id'):
            return True
        if not self._owns_claim(job):
            return False
        workspace = self._workspace(db,run['workspace_id'])
        evidence = job.get('environment') or {}
        resources = self._check_resources(workspace,evidence.get('dependencies',[]))
        return job['environment_lease_claim']['resources'] == [list(item) for item in resources]

    def attest_check(self,db,run,job):
        if not run.get('workspace_id'):
            return
        if not self.check_lease(db,run,job) or not self.validate_evidence(db,run,job.get('environment')):
            job.update(status='invalidated',environment_lease_verified=False,error='The environment lease or evidence changed before result recording.')
            return
        job['environment_lease_verified'] = True
        job['environment_lease_receipt'] = json.loads(json.dumps(job['environment_lease_claim']))

    def accepts_check(self,db,run,job):
        if not self.validate_evidence(db,run,job.get('environment')):
            return False
        if not run.get('workspace_id'):
            return True
        claim = job.get('environment_lease_claim') or {}
        return (job.get('environment_lease_verified') is True and job.get('environment_lease_receipt') == claim
                and claim.get('owner') == self._lease_owner(job))

    def release_check(self, db, job):
        lease_id = job.get('environment_lease_id')
        if not lease_id:
            return
        evidence = job.get('environment') or {}
        if not self._owns_claim(job):
            self._lost_check_lease(job,evidence)
            return
        if 'data' in evidence.get('dependencies',[]):
            workspace = self._workspace(db,evidence['workspace_id'])
            workspace['data_generation'] = self.leases.finish_data_writer(lease_id,self._database_resource(workspace))
            self.store.put(db,'parent_workspace',workspace)
            evidence['data_generation'] = workspace['data_generation']
        self.leases.release(lease_id)
        job['environment_lease_id'] = None

    def _lost_check_lease(self,job,evidence):
        job['environment_lease_verified'] = False
        job['error'] = 'The recorded environment lease is missing or has another owner.'
        if job.get('status') not in {'cancelled','interrupted'}:
            job['status'] = 'invalidated'
        if 'data' in evidence.get('dependencies',[]) and job.get('started_at'):
            self._invalidate_lost_data(job,evidence)
        job['environment_lease_id'] = None

    def _invalidate_lost_data(self,job,evidence):
        claim = job.get('environment_lease_claim') or {}
        for resource,mode in claim.get('resources',[]):
            if resource.startswith('database:') and mode == 'exclusive':
                evidence['data_generation'] = self.leases.invalidate_data(resource)

    def worker_environment(self, job, inherited=None):
        with self.store.transaction() as db:
            run = self.store.get(db,'run',job['run_id'])
            if not run.get('workspace_id'):
                return dict(os.environ if inherited is None else inherited)
            attempt = self.store.get(db,'attempt',job['attempt_id']) if job.get('attempt_id') else None
        if attempt:
            source = Path(attempt['worktree'])/'.ub-parent'/'worker.env.json'
            values = read_environment(source)
        else:
            with self.store.transaction() as db:
                workspace = self._workspace(db,run['workspace_id'])
            config = self._config(workspace)
            values = read_environment(config['database']['worker_env_file']) if config.get('database') else {}
            values = {key:value for key,value in values.items() if key in DATABASE_KEYS}
        return sanitized_environment(values,inherited)

    def schema_request(self, attempt_id, proposal, reason):
        require(isinstance(proposal,dict), 'Provide a schema proposal object.')
        require(isinstance(proposal.get('commit'),str) and isinstance(proposal.get('expected_fingerprint'),str), 'Record the proposal commit and expected schema fingerprint.')
        require(set(proposal) <= {'commit','expected_fingerprint','description','pre_checks','patch','schema_paths'}, 'The schema proposal contains unsupported fields.')
        with self.store.transaction() as db:
            attempt = self.store.get(db,'attempt',attempt_id)
            run = self.store.get(db,'run',attempt['run_id'])
            workspace = self._workspace(db,run['workspace_id'])
            require(workspace['database_identity'] is not None,'Attach a database before requesting a schema change.')
            require_clean(attempt['worktree'])
            revision = git(attempt['worktree'],'rev-parse','--verify',proposal['commit'] + '^{commit}')
            require(revision == git(attempt['worktree'],'rev-parse','HEAD'), 'Commit the current schema proposal before requesting review.')
            proposal = {**proposal,'commit':revision}
            config = self._config(workspace)
            scope = proposal.get('schema_paths',[])
            require(isinstance(scope,list), 'Provide schema paths as a list.')
            allowed = config.get('schema_paths',['src/server/db/schema/','prisma/','drizzle/'])
            for name in scope:
                relative_path(name.rstrip('/'))
                require(selected(name,allowed), 'The proposal schema path is outside the coordinator whitelist.')
            changed = [] if revision == attempt.get('base_commit') else git(attempt['worktree'],'diff-tree','--no-commit-id','--name-only','--root','-r',revision).splitlines()
            require(all(selected(name,scope) for name in changed), 'The proposal commit contains changes outside its reviewed schema scope.')
            record = {'id':identifier('schema'),'run_id':run['id'],'attempt_id':attempt_id,'workspace_id':workspace['id'],
                      'proposal':proposal,'prior_fingerprint':workspace['schema_fingerprint'],'schema_generation':workspace['schema_generation'],'status':'requested','author':attempt.get('agent_id',attempt_id)}
            self.store.put(db,'schema_proposal',record)
            self.store.event(db,run['id'],'schema_requested',reason,{'proposal_id':record['id']})
            return record

    def schema_review(self, proposal_id, reviewer_id, reason):
        with self.store.transaction() as db:
            record = self.store.get(db,'schema_proposal',proposal_id)
            require(record['status'] == 'requested','Review a requested schema proposal.')
            require(reviewer_id and reviewer_id != record['author'], 'Use an independent schema reviewer.')
            run = self.store.get(db, 'run', record['run_id'])
            assignment = self.runtime._assignment(db, run, reviewer_id, 'schema_review', proposal_id)
            record.update(status='reviewed',reviewer_id=reviewer_id)
            record.update(model_assignment_id=assignment['id'] if assignment else None,
                          requested_model=assignment['requested_model'] if assignment else None,
                          reported_model=assignment['reported_model'] if assignment else None,
                          model_source=assignment['model_source'] if assignment else None,
                          model_reference=assignment['model_reference'] if assignment else None)
            self.store.put(db,'schema_proposal',record)
            self.store.event(db,record['run_id'],'schema_reviewed',reason,{'proposal_id':proposal_id,'reviewer_id':reviewer_id})
            return record

    def schema_apply(self, proposal_id, capability, reason, stage=None):
        with self.store.transaction() as db:
            record = self.store.get(db,'schema_proposal',proposal_id)
            require(record['status'] in {'reviewed','applied'}, 'The schema proposal requires review before application.')
            workspace = self._workspace(db,record['workspace_id'])
            stored = (self._directory(workspace['id'])/'coordinator.capability').read_text().strip()
            # This is an authenticated CLI guard, not isolation from agents with the same filesystem access.
            require(isinstance(capability,str) and secrets.compare_digest(capability,stored), 'The coordinator capability is invalid.')
            if record['status'] == 'applied':
                return record
            self._identity(workspace)
            require_clean(workspace['parent_path'])
            require(workspace['status'] == 'ready', 'Reconcile the previous schema intent before application.')
            require(record['schema_generation'] == workspace['schema_generation'], 'The proposal schema generation is stale.')
            lease = self.leases.acquire([(self._database_resource(workspace),'exclusive')], {'proposal_id':proposal_id,'operation':'schema_apply'})
            try:
                probe = inspect_database(self._config(workspace)['database'],workspace['parent_path'])
                require(probe['schema_fingerprint'] == record['prior_fingerprint'] == workspace['schema_fingerprint'], 'The actual schema fingerprint differs from the reviewed baseline. Reconcile the parent before application.')
            except BaseException:
                # No source staging or schema command ran. The reviewed proposal remains unchanged.
                self.leases.release(lease)
                raise
            record.update(status='applying',lease_id=lease, prior_fingerprint=workspace['schema_fingerprint'],intent_at=time.time())
            workspace['status'] = 'reconciliation_required'
            self.store.put(db,'schema_proposal',record)
            self.store.put(db,'parent_workspace',workspace)
            self.store.event(db,record['run_id'],'schema_apply_intent',reason,{'proposal_id':proposal_id,'database_identity':workspace['database_identity']})
        try:
            if stage:
                stage(record,workspace)
                with self.store.transaction() as db:
                    stored_record = self.store.get(db,'schema_proposal',proposal_id)
                    record = {**record,**stored_record}
            require(git(workspace['parent_path'],'merge-base','--is-ancestor',record.get('staged_commit',record['proposal']['commit']),'HEAD') == '', 'Integrate the reviewed proposal into the parent first.')
            schema_command(self._config(workspace),workspace['parent_path'])
            recovery = self.recover(workspace['id'],reason)
            require(not recovery.get('recovery_required'), 'The schema fingerprint requires reconciliation before retrying.')
        except BaseException:
            # Keep the exclusive lease until the actual schema resolves the recorded intent.
            raise
        with self.store.transaction() as db:
            return self.store.get(db,'schema_proposal',proposal_id)

    def recover(self, workspace_id, reason, resolution=None):
        with self.store.transaction() as db:
            workspace = self._workspace(db,workspace_id)
            self._identity(workspace)
            config = self._config(workspace)
            probe = inspect_database(config.get('database'),workspace['parent_path'])
            records = [json.loads(row[0]) for row in db.execute("SELECT data FROM objects WHERE kind='schema_proposal'")]
            pending = [record for record in records if record['workspace_id'] == workspace_id and record['status'] == 'applying']
            if not pending:
                require(not probe or probe['schema_fingerprint'] == workspace['schema_fingerprint'], 'An unrecorded schema change requires coordinator reconciliation.')
                return workspace
            require(len(pending) == 1, 'Multiple schema intents require coordinator reconciliation.')
            record = pending[0]
            actual = probe['schema_fingerprint']
            expected = record['proposal']['expected_fingerprint']
            if actual != expected:
                if resolution == 'retry' and actual == record['prior_fingerprint']:
                    workspace.update(status='ready')
                    workspace.pop('recovery_state',None)
                    record['status'] = 'reviewed'
                    self.store.put(db,'schema_proposal',record)
                    self.store.put(db,'parent_workspace',workspace)
                    self.leases.release(record.get('lease_id'))
                    self.store.event(db,record['run_id'],'schema_retry_authorized',reason,{'proposal_id':record['id']})
                    return workspace
                workspace['recovery_state'] = 'not_applied' if actual == record['prior_fingerprint'] else 'partially_applied'
                self.store.put(db,'parent_workspace',workspace)
                # Keep the intent and lease. An explicit coordinator decision is required.
                return {**workspace,'recovery_required':True}
            workspace.pop('recovery_state',None)
            refresh_grants(config.get('database'))
            inspect_database(config.get('database'),workspace['parent_path'])
            workspace.update(schema_fingerprint=actual,schema_generation=workspace['schema_generation'] + 1, status='ready', source_commit=git(workspace['parent_path'],'rev-parse','HEAD'))
            record.update(status='applied',applied_at=time.time(),schema_generation=workspace['schema_generation'])
            self.store.put(db,'parent_workspace',workspace)
            self.store.put(db,'schema_proposal',record)
            self.store.event(db,record['run_id'],'schema_applied',reason,{'proposal_id':record['id'],'schema_generation':workspace['schema_generation']})
            self.leases.release(record.get('lease_id'))
            return workspace

    def refresh(self, run_id, attempt_id, reason):
        with self.store.transaction() as db:
            run = self.store.get(db,'run',run_id)
            attempt = self.store.get(db,'attempt',attempt_id)
            require(attempt['run_id'] == run_id,'The child belongs to another run.')
            require_clean(attempt['worktree'])
            workspace = self._workspace(db,run['workspace_id'])
            self._identity(workspace)
            require(workspace['status'] == 'ready','Reconcile the parent environment before refresh.')
            require(git(attempt['worktree'],'merge-base','--is-ancestor',git(workspace['parent_path'],'rev-parse','HEAD'),'HEAD') == '', 'Reconcile the child with the current parent commit before refresh.')
            parent_head = git(workspace['parent_path'],'rev-parse','HEAD')
            attempt.setdefault('original_base_commit',attempt['base_commit'])
            attempt['base_commit'] = parent_head
            workspace['source_commit'] = parent_head
            self.store.put(db,'parent_workspace',workspace)
            self.prepare_child(db,run,attempt)
            self.store.put(db,'attempt',attempt)
            self.store.event(db,run_id,'workspace_refreshed',reason,{'attempt_id':attempt_id,'workspace_id':workspace['id']})
            return attempt['parent_environment']

    def release_child(self, db, attempt):
        environment = attempt.get('parent_environment')
        if not environment:
            return {'released':True,'links':[]}
        if environment.get('app'):
            self.leases.release(environment['app'].get('lease_id'))
        environment['released'] = True
        self.store.put(db,'attempt',attempt)
        return {'released':True,'links':environment.get('links',[])}


def read_environment_config(path):
    path = Path(path)
    require(not path.is_symlink() and not path.stat().st_mode & 0o077, 'The parent configuration must remain private.')
    return json.loads(path.read_text())
