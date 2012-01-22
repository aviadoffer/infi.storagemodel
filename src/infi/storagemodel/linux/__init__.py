from contextlib import contextmanager

from ..base import StorageModel
from infi.pyutils.lazy import cached_method, cached_function

POSSIBLE_SCRIPT_NAMES = [
                          "rescan-scsi-bus",
                          "rescan-scsi-bus.sh",
                        ]

CHMOD_777 = 33261

def _write_an_executable_copy_of_builtin_rescan_script():
    from os import chmod, write, close
    from pkg_resources import resource_stream
    from tempfile import mkstemp
    fd, path = mkstemp(prefix='rescan-scsi-bus', text=True)
    write(fd, resource_stream(__name__, 'rescan-scsi-bus.sh').read())
    close(fd)
    chmod(path, CHMOD_777)
    return path

@cached_function
def _locate_rescan_script():
    from os import access, environ, X_OK, chmod
    from os.path import exists, join
    if _is_ubuntu():
        # The script in ubuntu waits to long (hard-coded 11 seconds) on each failed device
        # We use a modified version of the script that does not wait that long
        return _write_an_executable_copy_of_builtin_rescan_script()
    for script in POSSIBLE_SCRIPT_NAMES:
        for base in environ["PATH"].split(':'):
            for name in POSSIBLE_SCRIPT_NAMES:
                script = join(base, name)
                if exists(script) and access(script, X_OK):
                    return script
    # no script found
    return None

def _call_partprobe(env=None):
    from infi.execute import execute
    execute(["partprobe", ]).wait()

def _is_ubuntu():
    from platform import linux_distribution
    distname = linux_distribution()[0].lower()
    return distname in ["ubuntu", ]

def _call_rescan_script(env=None):
    """for testability purposes, we want to call execute with no environment variables, to mock the effect
    that the script does not exist"""
    from infi.exceptools import chain
    from infi.execute import execute_async
    from ..errors import StorageModelError
    rescan_script = _locate_rescan_script()
    if rescan_script is None:
        raise StorageModelError("no rescan-scsi-bus script found") # pylint: disable=W0710
    try:
        _ = execute_async([rescan_script, "--remove"], env=env)
    except Exception:
        raise chain(StorageModelError("failed to initiate rescan"))

class LinuxStorageModel(StorageModel):
    @cached_method
    def _get_sysfs(self):
        from .sysfs import Sysfs
        return Sysfs()

    def _create_scsi_model(self):
        from .scsi import LinuxSCSIModel
        return LinuxSCSIModel(self._get_sysfs())

    def _create_native_multipath_model(self):
        from .native_multipath import LinuxNativeMultipathModel
        return LinuxNativeMultipathModel(self._get_sysfs())

    def _create_disk_model(self):
        from .disk import LinuxDiskModel
        return LinuxDiskModel()

    def _create_mount_manager(self):
        from .mount import LinuxMountManager
        return LinuxMountManager()

    def _create_mount_repository(self):
        from .mount import LinuxMountRepository
        return LinuxMountRepository()

    def initiate_rescan(self):
        """the first attempt will be to use rescan-scsi-bus.sh, which comes out-of-the-box in redhat distributions,
        and from the debian packager scsitools.
        If and when we'll encounter a case in which this script doesn't work as expected, we will port it to Python
        and modify it accordingly.
        """
        _call_rescan_script()
        _call_partprobe()

def is_rescan_script_exists():
    return _locate_rescan_script() is not None

