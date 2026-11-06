"""Inspect the Node executable and the parent's declared runtime constraints."""

import json
from pathlib import Path
import re
import shutil
import subprocess

from .store import WorkflowError


VERSION_FILES = ('.nvmrc','.node-version')


def inspect_node(parent_path,dependency_target):
    root = Path(parent_path)
    package = root/'package.json'
    if dependency_target is None or not package.is_file():
        return None
    executable = _node_executable()
    return {'executable':executable,'version':_node_version(executable),
            'declared':_node_declarations(root,package)}


def _node_executable():
    executable = shutil.which('node')
    if not executable:
        raise WorkflowError('Install the declared Node runtime before attaching dependencies.')
    return str(Path(executable).resolve())


def _node_version(executable):
    try:
        result = subprocess.run([executable,'--version'],capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.TimeoutExpired):
        raise WorkflowError('The Node runtime version probe failed.') from None
    return _version_result(result)


def _version_result(result):
    if result.returncode:
        raise WorkflowError('The Node runtime version probe failed.')
    return _checked_version(result.stdout.strip())


def _checked_version(version):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?',version):
        raise WorkflowError('The Node runtime returned an invalid version.')
    return version


def _node_declarations(root,package):
    manifest = _package_manifest(package)
    return {'engines':_engine_constraint(manifest.get('engines',{})),
            'version_files':_version_files(root)}


def _package_manifest(package):
    try:
        manifest = json.loads(package.read_text())
    except (OSError,ValueError):
        raise WorkflowError('The Node package manifest could not be read.') from None
    if not isinstance(manifest,dict):
        raise WorkflowError('The Node package manifest must contain an object.')
    return manifest


def _engine_constraint(engines):
    if not isinstance(engines,dict):
        raise WorkflowError('The Node engine declaration must contain an object.')
    constraint = engines.get('node')
    if constraint is None:
        return None
    return _checked_constraint(constraint)


def _checked_constraint(constraint):
    if not isinstance(constraint,str) or not constraint.strip():
        raise WorkflowError('A Node runtime declaration must contain a nonempty constraint.')
    return constraint.strip()


def _version_files(root):
    return {name:_version_file(root/name) for name in VERSION_FILES if (root/name).is_file()}


def _version_file(path):
    try:
        constraint = path.read_text()
    except OSError:
        raise WorkflowError('A Node runtime declaration file could not be read.') from None
    return _checked_constraint(constraint)
