"""Complete worktree inventories that never follow symbolic links."""

import hashlib
import ctypes
import base64
import sys
import os
from pathlib import Path
import stat
import subprocess

from .store import WorkflowError, encode


def git_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, env=git_environment())
    if result.returncode:
        raise WorkflowError(result.stderr.strip() or 'Git failed.')
    return result.stdout.strip()


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def exact_path(value):
    path = Path(value).absolute()
    if str(path) != value or path.resolve() != path:
        raise WorkflowError('The resource path changed or contains a symbolic link.')
    return path


def _os_extended_attributes(path):
    return {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode()
            for name in os.listxattr(path, follow_symlinks=False)}


def _darwin_attribute_names(path, library):
    size = library.listxattr(os.fsencode(path), None, 0, 1)
    if size < 0:
        raise OSError(ctypes.get_errno(), 'Cannot inspect extended file metadata.')
    buffer = ctypes.create_string_buffer(size)
    if library.listxattr(os.fsencode(path), buffer, size, 1) != size:
        raise WorkflowError('Extended file metadata changed during inventory.')
    return buffer.raw.split(b'\0')


def _darwin_attribute_value(path, name, library):
    length = library.getxattr(os.fsencode(path), name, None, 0, 0, 1)
    if length < 0:
        raise OSError(ctypes.get_errno(), 'Cannot read extended file metadata.')
    value = ctypes.create_string_buffer(length)
    if library.getxattr(os.fsencode(path), name, value, length, 0, 1) != length:
        raise WorkflowError('Extended file metadata changed during inventory.')
    return base64.b64encode(value.raw).decode()


def extended_attributes(path):
    if hasattr(os, 'listxattr'):
        return _os_extended_attributes(path)
    if sys.platform != 'darwin':
        raise WorkflowError('This platform cannot inspect extended file metadata.')
    library = ctypes.CDLL(None, use_errno=True)
    return {os.fsdecode(name): _darwin_attribute_value(path, name, library)
            for name in _darwin_attribute_names(path, library) if name}


def _darwin_remove_attribute(path, name):
    if ctypes.CDLL(None, use_errno=True).removexattr(os.fsencode(path), os.fsencode(name), 1):
        raise WorkflowError('Cannot restore extended file metadata.')


def _remove_attribute(path, name):
    if hasattr(os, 'removexattr'):
        os.removexattr(path, name, follow_symlinks=False)
    else:
        _darwin_remove_attribute(path, name)


def _darwin_set_attribute(path, name, value):
    if ctypes.CDLL(None, use_errno=True).setxattr(os.fsencode(path), os.fsencode(name), value, len(value), 0, 1):
        raise WorkflowError('Cannot restore extended file metadata.')


def _set_attribute(path, name, value):
    if hasattr(os, 'setxattr'):
        os.setxattr(path, name, value, follow_symlinks=False)
    else:
        _darwin_set_attribute(path, name, value)


def restore_attributes(path, attributes):
    current = extended_attributes(path)
    for name in current.keys() - attributes.keys():
        _remove_attribute(path, name)
    for name, encoded in attributes.items():
        value = base64.b64decode(encoded, validate=True)
        _set_attribute(path, name, value)


def _missing_acl_supported(info, error):
    return error == 2 or (stat.S_ISLNK(info.st_mode) and error == 45)


def _darwin_has_acl(path, info):
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_link_np.restype = ctypes.c_void_p
    acl = library.acl_get_link_np(os.fsencode(path), 0x100)
    if not acl and _missing_acl_supported(info, ctypes.get_errno()):
        return False
    if not acl:
        raise OSError(ctypes.get_errno(), 'Cannot inspect file access controls.')
    try:
        # The archive does not support ACLs. Preserve any resource with an ACL object.
        return True
    finally:
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free(acl)


def _posix_has_acl(path):
    return any(name in extended_attributes(path) for name in ('system.posix_acl_access', 'system.posix_acl_default'))


def unsupported_metadata(path, info):
    if getattr(info, 'st_flags', 0):
        return True
    if sys.platform != 'darwin':
        return _posix_has_acl(path)
    return _darwin_has_acl(path, info)


def require_supported_metadata(path, info):
    if unsupported_metadata(path, info):
        raise WorkflowError('Extended file metadata requires a cleanup adapter.')
    if info.st_uid != os.getuid() or info.st_gid not in {*os.getgroups(), os.getgid()}:
        raise WorkflowError('Foreign file ownership requires a cleanup adapter.')
    if stat.S_IMODE(info.st_mode) & 0o7000:
        raise WorkflowError('Special permission bits require a cleanup adapter.')


def inventory_type(path, info):
    if stat.S_ISLNK(info.st_mode):
        return {'type': 'symlink', 'target': os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        require_no_bare_repository(path)
        return {'type': 'directory'}
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        return {'type': 'file', 'size': info.st_size, 'sha256': file_hash(path)}
    raise WorkflowError('A special file or hard link blocks cleanup.')


def require_no_bare_repository(path):
    if (path / 'HEAD').is_file() and (path / 'objects').is_dir() and (path / 'refs').is_dir():
        raise WorkflowError('A nested bare repository blocks cleanup.')


def require_stable_file(path, before):
    after = path.lstat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise WorkflowError('A file changed during inventory.')


def inventory_entry(path, root):
    before = path.lstat()
    require_supported_metadata(path, before)
    record = {'path': str(path.relative_to(root)), 'mode': stat.S_IMODE(before.st_mode),
              'mtime_ns': before.st_mtime_ns, 'xattrs': extended_attributes(path),
              'uid': before.st_uid, 'gid': before.st_gid}
    record.update(inventory_type(path, before))
    require_stable_file(path, before)
    return record, before.st_ino


def inventory_directory(directory, root, result):
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        path = Path(entry.path)
        if str(path.relative_to(root)) == '.git':
            continue
        if entry.name == '.git':
            raise WorkflowError('A nested repository blocks cleanup.')
        record, inode = inventory_entry(path, root)
        result.append(record)
        if record['type'] == 'directory':
            exact_path(str(path))
            inventory_directory(path, root, result)
            if path.lstat().st_ino != inode:
                raise WorkflowError('A directory changed during inventory.')


def inventory(root):
    root = exact_path(str(root))
    require_supported_metadata(root, root.lstat())
    result = []
    inventory_directory(root, root, result)
    return result


def git_identity(root):
    root = exact_path(str(root))
    common = Path(git(root, 'rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    git_dir = Path(git(root, 'rev-parse', '--absolute-git-dir')).resolve()
    common_stat = common.stat()
    root_stat = root.stat()
    return {'path': str(root), 'git_dir': str(git_dir), 'common_dir': str(common), 'common_identity': [common_stat.st_dev, common_stat.st_ino],
            'path_identity': [root_stat.st_dev, root_stat.st_ino],
            'branch': git(root, 'symbolic-ref', '--short', 'HEAD'), 'head': git(root, 'rev-parse', 'HEAD')}


def require_visible_index(root):
    for option in ('-v', '-f'):
        output = subprocess.check_output(['git', '-C', str(root), 'ls-files', option, '-z'], env=git_environment())
        for entry in output.split(b'\0'):
            tag = entry[:1]
            if tag and (tag.islower() or tag == b'S'):
                raise WorkflowError('An index flag can hide tracked changes. Preserve this worktree.')


def require_complete_git_storage(root, common):
    if any((common / name).exists() for name in ('objects/info/alternates', 'shallow', 'info/grafts')):
        raise WorkflowError('External or incomplete Git object storage blocks cleanup.')
    if git(root, 'for-each-ref', '--format=%(refname)', 'refs/replace'):
        raise WorkflowError('Git replacement objects require a cleanup adapter.')


def require_supported_tracked_content(root):
    if '160000 ' in git(root, 'ls-files', '--stage'):
        raise WorkflowError('Submodules require a cleanup adapter.')
    if (root / '.gitmodules').exists():
        raise WorkflowError('Submodule metadata requires a cleanup adapter.')
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'], env=git_environment())
    attributes = subprocess.run(['git', '-C', str(root), 'check-attr', '-z', '--stdin', 'filter'],
                                input=tracked, capture_output=True, env=git_environment(), check=True).stdout.split(b'\0')
    if b'lfs' in attributes[2::3]:
        raise WorkflowError('Git LFS requires a cleanup adapter.')


def require_no_partial_clone(root, common):
    partial = subprocess.run(['git', '-C', str(root), 'config', '--get', 'extensions.partialClone'],
                             capture_output=True, env=git_environment())
    if partial.returncode == 0 or list((common / 'objects' / 'pack').glob('*.promisor')):
        raise WorkflowError('Partial Git repositories require a cleanup adapter.')


def require_idle_git_linkage(root):
    linkage = root / '.git'
    if not linkage.is_file() or linkage.is_symlink():
        raise WorkflowError('The candidate has unsupported Git linkage.')
    metadata = Path(git(root, 'rev-parse', '--absolute-git-dir'))
    if any((metadata / name).exists() for name in ('index.lock', 'HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge', 'rebase-apply')):
        raise WorkflowError('An unfinished Git operation blocks cleanup.')


def require_supported_git(root):
    root = Path(root)
    require_visible_index(root)
    identity = git_identity(root)
    common = Path(identity['common_dir'])
    require_complete_git_storage(root, common)
    require_supported_tracked_content(root)
    require_no_partial_clone(root, common)
    require_idle_git_linkage(root)


def registrations(repository):
    output = subprocess.check_output(['git', '-C', str(repository), 'worktree', 'list', '--porcelain', '-z'], env=git_environment())
    result = []
    record = {}
    for field in output.split(b'\0'):
        if not field:
            if record:
                result.append(record)
                record = {}
            continue
        key, _, value = os.fsdecode(field).partition(' ')
        record[key] = value
    if record:
        result.append(record)
    return result


def unknown_files(root):
    data = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-o', '--exclude-standard', '-z'], env=git_environment())
    return [os.fsdecode(item) for item in data.split(b'\0') if item]
