"""Private database credentials, real PostgreSQL probes, and guarded schema commands."""

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit, urlunsplit, quote, unquote

from .store import WorkflowError


DATABASE_KEYS = {'DATABASE_URL', 'DIRECT_URL', 'POSTGRES_URL', 'POSTGRES_URL_NON_POOLING', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE', 'PGPASSFILE', 'PGOPTIONS'}


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise WorkflowError('A private environment path must not be a symlink.')
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')


def read_environment(path):
    source = Path(path)
    if source.stat().st_mode & 0o077:
        raise WorkflowError('A database credential file must have mode 600.')
    content = source.read_text()
    value = json.loads(content) if content.lstrip().startswith('{') else _dotenv_environment(content)
    if not isinstance(value,dict) or any(not _environment_entry(key,item) for key,item in value.items()):
        raise WorkflowError('A private environment must contain string keys and values.')
    return value


def _environment_entry(key,value):
    return isinstance(key,str) and isinstance(value,str)


def _dotenv_environment(content):
    assignments = map(_dotenv_assignment,content.splitlines())
    return dict(filter(None,assignments))


def _dotenv_assignment(line):
    line = line.strip()
    if line.startswith('#'):
        return None
    key, separator, item = line.removeprefix('export ').partition('=')
    return (key.strip(),item.strip().strip('"\'')) if separator else None


def database_identity(url):
    try:
        parsed = urlsplit(url)
        identity = {'host':parsed.hostname, 'port':parsed.port or 5432, 'database':unquote(parsed.path.lstrip('/'))}
    except ValueError:
        raise WorkflowError('The worker database URL is invalid.') from None
    if parsed.scheme not in {'postgres','postgresql'} or identity['host'] not in {'localhost','127.0.0.1','::1'} or not identity['database'] or parsed.query or parsed.fragment:
        raise WorkflowError('Use an exact local PostgreSQL database URL without connection overrides.')
    return identity


def connect(url):
    try:
        import psycopg
    except ImportError:
        raise WorkflowError('Install the postgres extra to inspect PostgreSQL permissions.') from None
    try:
        return psycopg.connect(url, connect_timeout=10, autocommit=True)
    except Exception:
        raise WorkflowError('The private database connection failed.') from None


def provision(config, directory):
    if config.get('worker_env_file'):
        return config
    admin = read_environment(config['admin_env_file'])
    url = admin.get('DATABASE_URL')
    database_identity(url)
    role = 'ub_worker_' + secrets.token_hex(10)
    password = secrets.token_urlsafe(32)
    from psycopg import sql
    try:
        with connect(url) as connection:
            connection.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role), sql.Literal(password)))
            database = connection.execute('SELECT current_database()').fetchone()[0]
            # PUBLIC CREATE grants would also permit worker schema changes. Require the coordinator to remove them explicitly.
            connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database), sql.Identifier(role)))
            for schema, in connection.execute("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"):
                connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
                connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
                if config.get('data_write', False):
                    connection.execute(sql.SQL('GRANT INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
                    connection.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
    except WorkflowError:
        raise
    except Exception:
        raise WorkflowError('The coordinator could not provision the restricted worker role.') from None
    parsed = urlsplit(url)
    worker_url = urlunsplit((parsed.scheme, quote(role) + ':' + quote(password) + '@' + parsed.netloc.rsplit('@',1)[-1], parsed.path, '', ''))
    destination = Path(directory) / 'database-worker.json'
    private_write(destination, {'DATABASE_URL':worker_url})
    return {**config, 'worker_env_file':str(destination), 'provisioned_role':role}


SCHEMA_QUERIES = {
    'schemas': """SELECT nspname,pg_get_userbyid(nspowner) FROM pg_namespace
        WHERE nspname !~ '^pg_' AND nspname<>'information_schema' ORDER BY nspname""",
    'relations': """SELECT n.nspname,c.relname,c.relkind,c.relpersistence,c.relrowsecurity,
        c.relforcerowsecurity,c.reloptions,pg_get_userbyid(c.relowner),
        CASE WHEN c.relkind='p' THEN pg_get_partkeydef(c.oid) END,
        pg_get_expr(c.relpartbound,c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname""",
    'columns': """SELECT n.nspname,c.relname,a.attnum,a.attname,
        format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,
        co.collname,pg_get_expr(d.adbin,d.adrelid)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_collation co ON co.oid=a.attcollation
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,a.attnum""",
    'constraints': """SELECT n.nspname,c.relname,t.typname,co.conname,
        pg_get_constraintdef(co.oid,true),co.convalidated,co.condeferrable,co.condeferred
        FROM pg_constraint co JOIN pg_namespace n ON n.oid=co.connamespace
        LEFT JOIN pg_class c ON c.oid=co.conrelid LEFT JOIN pg_type t ON t.oid=co.contypid
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,t.typname,co.conname""",
    'indexes': """SELECT n.nspname,c.relname,i.relname,pg_get_indexdef(i.oid),
        x.indisvalid,x.indisready,x.indisreplident
        FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid
        JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,i.relname""",
    'views': """SELECT n.nspname,c.relname,pg_get_viewdef(c.oid,true)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('v','m') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname""",
    'triggers': """SELECT n.nspname,c.relname,t.tgname,pg_get_triggerdef(t.oid,true),t.tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,t.tgname""",
    'rules': """SELECT n.nspname,c.relname,r.rulename,pg_get_ruledef(r.oid,true),r.ev_enabled
        FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,r.rulename""",
    'sequences': """SELECT n.nspname,c.relname,format_type(s.seqtypid,NULL),s.seqstart,
        s.seqincrement,s.seqmax,s.seqmin,s.seqcache,s.seqcycle
        FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname""",
    'policies': """SELECT n.nspname,c.relname,p.polname,p.polpermissive,p.polcmd,
        ARRAY(SELECT CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_get_userbyid(r)::text END
            FROM unnest(p.polroles) r ORDER BY 1),
        pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,c.relname,p.polname""",
    'functions': """SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),
        pg_get_functiondef(p.oid),pg_get_userbyid(p.proowner)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND p.prokind<>'a'
        ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)""",
    'types': """SELECT n.nspname,t.typname,t.typtype,t.typnotnull,t.typdefault,
        CASE WHEN t.typbasetype<>0 THEN format_type(t.typbasetype,t.typtypmod) END
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND t.typtype IN ('d','e','r','m')
        ORDER BY n.nspname,t.typname""",
    'enums': """SELECT n.nspname,t.typname,e.enumlabel,e.enumsortorder::text FROM pg_enum e
        JOIN pg_type t ON t.oid=e.enumtypid JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
        ORDER BY n.nspname,t.typname,e.enumsortorder""",
    'extensions': 'SELECT extname,extversion FROM pg_extension ORDER BY extname',
}


def postgres_probe(config):
    environment = read_environment(config['worker_env_file'])
    url = environment.get('DATABASE_URL')
    identity = database_identity(url)
    try:
        with connect(url) as connection:
            _check_server_identity(connection,identity)
            hazards = _role_hazards(connection)
            schema = {name:connection.execute(query).fetchall() for name,query in SCHEMA_QUERIES.items()}
    except WorkflowError:
        raise
    except Exception:
        raise WorkflowError('The PostgreSQL permission probe failed.') from None
    return {'identity':identity, 'schema_fingerprint':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest(), 'worker_restricted':not hazards, 'hazards':hazards}


def _check_server_identity(connection,identity):
    actual_database, actual_port = connection.execute('SELECT current_database(), inet_server_port()').fetchone()
    # NAT can map a different local port. The URL identifies the external endpoint.
    if actual_database != identity['database'] or not actual_port:
        raise WorkflowError('The actual database identity differs from the configured identity.')


def _role_hazards(connection):
    hazards = []
    flags = connection.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
    if any(flags):
        hazards.append('privileged role attributes')
    if connection.execute("SELECT has_database_privilege(current_database(),'CREATE')").fetchone()[0]:
        hazards.append('database CREATE privilege')
    for label,query in ROLE_HAZARD_QUERIES:
        if connection.execute(query).fetchall():
            hazards.append(label)
    return hazards


ROLE_HAZARD_QUERIES = [
    ('permanent schema CREATE privilege', "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND has_schema_privilege(oid,'CREATE')"),
    ('object ownership or inherited owner membership', "SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND deptype='o' AND pg_has_role(refobjid,'MEMBER') LIMIT 1"),
    ('privileged role membership', "SELECT 1 FROM pg_roles WHERE oid<>(SELECT oid FROM pg_roles WHERE rolname=current_user) AND pg_has_role(oid,'MEMBER') AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolname LIKE 'pg_%') LIMIT 1"),
    ('callable privileged function', "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prosecdef AND has_function_privilege(p.oid,'EXECUTE') LIMIT 1"),
]

def inspect_database(config, parent):
    if not config:
        return None
    environment = read_environment(config['worker_env_file'])
    _check_worker_credentials(environment,config.get('expected_identity'))
    if config.get('probe_argv'):
        probe = _command_probe(config,parent,environment)
    else:
        probe = postgres_probe(config)
    _check_probe(probe,config.get('expected_identity'))
    return probe


def _worker_url_key(key):
    return key.endswith('DATABASE_URL') or key in {'DIRECT_URL','POSTGRES_URL','POSTGRES_URL_NON_POOLING'}


def _check_worker_credentials(environment,expected):
    urls = [value for key,value in environment.items() if _worker_url_key(key)]
    if not environment.get('DATABASE_URL') or any(value != environment['DATABASE_URL'] for value in urls) or any(key.startswith('PG') for key in environment):
        raise WorkflowError('The worker environment contains conflicting database credentials or connection overrides.')
    identities = [database_identity(value) for value in urls]
    if not identities or any(value != expected for value in identities):
        raise WorkflowError('The worker database identity differs from the expected identity.')


def _command_probe(config,parent,environment):
    result = subprocess.run(config['probe_argv'], cwd=parent, env=sanitized_environment(environment), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise WorkflowError('The configured private database probe failed.')
    try:
        return json.loads(result.stdout)
    except (ValueError,TypeError):
        raise WorkflowError('The configured database probe returned invalid evidence.') from None


def _check_probe(probe,expected):
    if probe.get('identity') != expected:
        raise WorkflowError('The actual database identity differs from the expected identity.')
    if probe.get('worker_restricted') is not True or probe.get('hazards'):
        raise WorkflowError('The worker role has unsafe privileges. Supply a restricted worker role.')
    if not isinstance(probe.get('schema_fingerprint'),str) or not probe['schema_fingerprint']:
        raise WorkflowError('The database probe did not return a schema fingerprint.')

def sanitized_environment(private=None, inherited=None):
    inherited = os.environ if inherited is None else inherited
    allowed = {'PATH','HOME','USER','LOGNAME','SHELL','LANG','LC_ALL','TMPDIR','SYSTEMROOT','COMSPEC','PATHEXT','PYTHONPATH','VIRTUAL_ENV','CI','TERM'}
    result = {key:value for key,value in inherited.items() if key in allowed}
    if private:
        result.update(private)
    result['UB_PARENT_WORKER'] = '1'
    return result


def schema_command(config, parent):
    argv = _schema_argv(config)
    environment = _coordinator_environment(config['database'])
    result = subprocess.run(argv, cwd=parent, env=environment, capture_output=True, timeout=600)
    if result.returncode:
        raise WorkflowError('The coordinator schema command failed. Reconcile the recorded intent before retrying.')


def _schema_argv(config):
    argv = ['npm','run','db:push'] if config.get('adapter') == 'backkr' else config.get('database',{}).get('schema_apply_argv')
    if not isinstance(argv,list) or not argv or any(not isinstance(value,str) for value in argv):
        raise WorkflowError('Configure a coordinator schema command.')
    return argv


def _coordinator_url_key(key):
    return key.endswith('DATABASE_URL') or ('URL' in key and ('POSTGRES' in key or 'DATABASE' in key)) or key in {'DIRECT_URL','DB_URL'}


def _coordinator_environment(database):
    environment = read_environment(database['admin_env_file'])
    aliases = [value for key,value in environment.items() if _coordinator_url_key(key)]
    if any(key.startswith('PG') for key in environment):
        raise WorkflowError('The coordinator environment contains conflicting PostgreSQL connection overrides.')
    if not environment.get('DATABASE_URL') or any(database_identity(value) != database['expected_identity'] for value in aliases):
        raise WorkflowError('The coordinator database identity differs from the attached database.')
    return {key:value for key,value in sanitized_environment(environment).items() if key != 'UB_PARENT_WORKER'}

def refresh_grants(config):
    """Grant data access to new objects without granting schema ownership."""
    if not config or config.get('probe_argv'):
        return
    _refresh_worker_grants(config)


def _refresh_worker_grants(config):
    worker = read_environment(config['worker_env_file'])
    role = unquote(urlsplit(worker['DATABASE_URL']).username)
    admin = read_environment(config['admin_env_file'])
    from psycopg import sql
    try:
        _grant_database_schemas(admin['DATABASE_URL'],sql,role,config)
    except Exception as error:
        _raise_grant_failure(error)


def _raise_grant_failure(error):
    if isinstance(error,WorkflowError):
        raise error
    raise WorkflowError('The coordinator could not refresh the worker data grants.') from None


def _grant_database_schemas(url,sql,role,config):
    with connect(url) as connection:
        for schema, in connection.execute("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"):
            _grant_schema_access(connection,sql,schema,role,config)


def _grant_schema_access(connection,sql,schema,role,config):
    connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    privileges = _worker_table_privileges(config)
    connection.execute(sql.SQL('GRANT ' + privileges + ' ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    if config.get('data_write'):
        connection.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))


def _worker_table_privileges(config):
    return 'SELECT,INSERT,UPDATE,DELETE' if config.get('data_write') else 'SELECT'
