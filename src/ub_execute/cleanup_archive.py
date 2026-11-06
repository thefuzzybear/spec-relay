"""Private checked archives and independent restoration."""

from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

from .cleanup_inventory import digest, file_hash, inventory, restore_attributes, git
from .store import WorkflowError, encode, identifier


@contextmanager
def archive_data_errors():
    try:
        yield
    except WorkflowError:
        raise
    except (ValueError, UnicodeError, TypeError, KeyError, AttributeError, tarfile.TarError) as error:
        raise WorkflowError('The recovery archive is malformed.') from error


def flush_file(path):
    with Path(path).open('rb') as stream:
        os.fsync(stream.fileno())


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts or str(path) != name:
        raise WorkflowError('The archive contains an unsafe path.')
    return path


def require_archive_members(archive, entries):
    members = archive.getmembers()
    files = {item['path']: item for item in entries if item['type'] == 'file'}
    if {member.name for member in members} != set(files) or len(members) != len(files):
        raise WorkflowError('The archive file list does not match its manifest.')
    for member in members:
        if not member.isfile() or member.size != files[member.name]['size']:
            raise WorkflowError('The archive contains an unsupported entry.')


def restore_destination(destination, entry):
    relative = safe_name(entry['path'])
    path = destination.joinpath(*relative.parts)
    if any(parent.is_symlink() for parent in path.parents if parent != destination.parent):
        raise WorkflowError('A restored path crosses a symbolic link.')
    return path


def restore_entry(archive, entry, path):
    if entry['type'] == 'directory':
        path.mkdir(exist_ok=True)
    elif entry['type'] == 'symlink':
        path.symlink_to(entry['target'])
    elif entry['type'] == 'file':
        with archive.extractfile(entry['path']) as source, path.open('xb') as target:
            shutil.copyfileobj(source, target)
    else:
        raise WorkflowError('The archive contains an unsupported entry.')


def retain_existing(path, missing_only):
    return missing_only and (path.exists() or path.is_symlink())


def restore_files(directory, manifest, destination, missing_only=False, selected_paths=None):
    entries = manifest['inventory']
    with archive_data_errors(), tarfile.open(directory / 'files.tar', 'r') as archive:
        require_archive_members(archive, entries)
        restored = []
        for entry in entries:
            if selected_paths is not None and entry['path'] not in selected_paths:
                continue
            path = restore_destination(destination, entry)
            if retain_existing(path, missing_only):
                continue
            restored.append(entry)
            restore_entry(archive, entry, path)
        restore_metadata(destination, restored)


def restore_metadata(destination, entries):
    for entry in reversed(entries):
        path = destination / entry['path']
        if path.is_symlink():
            if hasattr(os, 'lchmod'):
                os.lchmod(path, entry['mode'])
        else:
            path.chmod(entry['mode'])
        os.chown(path, entry['uid'], entry['gid'], follow_symlinks=False)
        restore_attributes(path, entry['xattrs'])
        os.utime(path, ns=(entry['mtime_ns'], entry['mtime_ns']), follow_symlinks=False)


def require_retained_manifest(manifest, expected_record):
    fields = ('id', 'run_id', 'resource_id', 'head', 'branch', 'inventory',
              'source_identity', 'checksums', 'manifest_digest')
    for field in fields:
        if manifest[field] != expected_record[field]:
            raise WorkflowError('The recovery archive differs from its retained record: ' + field)


def require_planned_manifest(manifest, expected_snapshot):
    expected = {'head': expected_snapshot['identity']['head'], 'branch': expected_snapshot['identity']['branch'],
                'inventory': expected_snapshot['inventory'], 'source_identity': expected_snapshot['identity']}
    for field, value in expected.items():
        if manifest[field] != value:
            raise WorkflowError('The recovery archive differs from its cleanup plan: ' + field)


def require_archive_checksums(directory, manifest):
    for filename, expected in manifest['checksums'].items():
        if filename not in {'files.tar', 'history.bundle'} or file_hash(directory / filename) != expected:
            raise WorkflowError('An archive checksum does not match.')
    if set(manifest['checksums']) != {'files.tar', 'history.bundle'}:
        raise WorkflowError('The archive is incomplete.')


def read_archive(directory, expected_record=None, expected_snapshot=None):
    with archive_data_errors():
        directory = Path(directory)
        manifest = json.loads((directory / 'manifest.json').read_text())
        checksum = manifest.pop('manifest_digest')
        if digest(manifest) != checksum:
            raise WorkflowError('The archive manifest checksum does not match.')
        manifest['manifest_digest'] = checksum
        require_archive_checksums(directory, manifest)
        for entry in manifest['inventory']:
            safe_name(entry['path'])
        if expected_record is not None:
            require_retained_manifest(manifest, expected_record)
        if expected_snapshot is not None:
            require_planned_manifest(manifest, expected_snapshot)
        return manifest


def restore_archive(directory, destination, expected_record=None, expected_snapshot=None):
    with archive_data_errors():
        return _restore_archive(directory, destination, expected_record, expected_snapshot)


def _restore_archive(directory, destination, expected_record, expected_snapshot):
    directory = Path(directory).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise WorkflowError('Restore requires an absent destination.')
    if destination.parent.resolve() != destination.parent:
        raise WorkflowError('The restore destination crosses a symbolic link.')
    manifest = read_archive(directory, expected_record, expected_snapshot)
    destination.mkdir(mode=0o700, parents=True)
    try:
        git(destination, 'init')
        # The bundle is the only object source. No source repository is available.
        git(destination, 'fetch', '--update-head-ok', str(directory / 'history.bundle'), 'refs/*:refs/*')
        git(destination, 'symbolic-ref', 'HEAD', 'refs/heads/' + manifest['branch'])
        git(destination, 'update-ref', 'HEAD', manifest['head'])
        git(destination, 'read-tree', manifest['head'])
        restore_files(directory, manifest, destination)
        if inventory(destination) != manifest['inventory']:
            raise WorkflowError('The restored files do not match the archive inventory.')
        git(destination, 'fsck', '--full')
        return {'destination': str(destination), 'head': manifest['head'], 'archive_id': manifest['id']}
    except BaseException:
        # This directory belongs to this restore attempt. Source work remains unchanged.
        shutil.rmtree(destination)
        raise


def create_archive(root, archive_root, snapshot, resource_id, run_id):
    root = Path(root)
    archive_root = Path(archive_root)
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_root.chmod(0o700)
    archive_id = identifier('archive')
    pending = Path(tempfile.mkdtemp(prefix='.pending-', dir=archive_root))
    try:
        bundle = pending / 'history.bundle'
        git(root, 'bundle', 'create', str(bundle), '--all')
        with tarfile.open(pending / 'files.tar', 'w') as archive:
            for entry in snapshot['inventory']:
                if entry['type'] != 'file':
                    continue
                path = root / entry['path']
                metadata = tarfile.TarInfo(entry['path'])
                metadata.size = entry['size']
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(descriptor, 'rb') as stream:
                    archive.addfile(metadata, stream)
        for name in ('history.bundle', 'files.tar'):
            (pending / name).chmod(0o600)
            flush_file(pending / name)
        manifest = {'id': archive_id, 'run_id': run_id, 'resource_id': resource_id,
                    'head': snapshot['identity']['head'], 'branch': snapshot['identity']['branch'],
                    'inventory': snapshot['inventory'], 'source_identity': snapshot['identity'],
                    'checksums': {name: file_hash(pending / name) for name in ('history.bundle', 'files.tar')}}
        manifest['manifest_digest'] = digest(manifest)
        (pending / 'manifest.json').write_text(encode(manifest))
        (pending / 'manifest.json').chmod(0o600)
        flush_file(pending / 'manifest.json')
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=archive_root) as check:
            restore_archive(pending, Path(check) / 'restored', manifest, snapshot)
        if inventory(root) != snapshot['inventory']:
            raise WorkflowError('The source changed during archival.')
        descriptor = os.open(pending, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        published = archive_root / archive_id
        os.rename(pending, published)
        descriptor = os.open(archive_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {**manifest, 'directory': str(published), 'restore_verified': True,
                'archive_bytes': sum((published / name).stat().st_size for name in ('history.bundle', 'files.tar', 'manifest.json'))}
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
