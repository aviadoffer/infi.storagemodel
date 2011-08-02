
from ..utils import cached_method, cached_property, clear_cache, LazyImmutableDict
from ..base import StorageModel, scsi, multipath
from contextlib import contextmanager

class WindowsDeviceMixin(object):
    @cached_method
    def get_pdo(self):
        return self._device_object.psuedo_device_object

    @contextmanager
    def asi_context(self):
        from infi.asi.win32 import OSFile
        from infi.asi import create_platform_command_executer
        handle = OSFile(self.get_pdo())
        executer = create_platform_command_executer(handle)
        try:
            yield executer
        finally:
            handle.close()

    @cached_method
    def get_ioctl_interface(self):
        from infi.devicemanager.ioctl import DeviceIoControl
        return DeviceIoControl(self.get_pdo())

    @cached_method
    def get_instance_id(self):
        return self._device_object._instance_id

    @cached_method
    def get_hctl(self):
        from ..dtypes import HCTL
        return HCTL(*self.get_ioctl_interface().scsi_get_address())

    @cached_method
    def get_scsi_vendor_id(self):
        # a faster implemntation on windows
        return str(self._device_object.hardware_ids[-2][0:8].replace('_', ''))

    @cached_method
    def get_scsi_product_id(self):
        # a faster implementation on windows
        return str(self._device_object.hardware_ids[-2][8:24].replace('_', ''))

    @cached_method
    def get_parent(self):
        return self._device_object.parent

class WindowsSCSIDevice(WindowsDeviceMixin, scsi.SCSIDevice):
    def __init__(self, device_object):
        super(WindowsSCSIDevice, self).__init__()
        self._device_object = device_object

    @cached_method
    def get_block_access_path(self):
        return self.get_pdo()

    @cached_method
    def get_scsi_access_path(self):
        return self.get_pdo()

    @cached_method
    def get_display_name(self):
        return self.get_scsi_access_path().split('\\')[-1]

class WindowsDiskDeviceMixin(object):
    @cached_method
    def get_size_in_bytes(self):
        return self.get_ioctl_interface().disk_get_drive_geometry_ex()

    @cached_method
    def get_physical_drive_number(self):
        """returns the drive number of the disk.
        if the disk is hidden (i.e. part of MPIODisk), it returns -1
        """
        number = self.get_ioctl_interface().storage_get_device_number()
        return -1 if number == 0xffffffff else number

    @cached_method
    def get_display_name(self):
        return "PHYSICALDRIVE%s" % self.get_physical_drive_number()

class WindowsSCSIBlockDevice(WindowsDiskDeviceMixin, WindowsSCSIDevice, scsi.SCSIBlockDevice):
    pass

class WindowsSCSIStorageController(WindowsSCSIDevice, scsi.SCSIStorageController):
    def __init__(self, device_object):
        super(WindowsSCSIStorageController, self).__init__()
        self._device_object = device_object

class WindowsSCSIModel(scsi.SCSIModel):
    @cached_method
    def get_device_manager(self):
        from infi.devicemanager import DeviceManager
        return DeviceManager()

    @cached_method
    def get_all_scsi_block_devices(self):
        return filter(lambda disk: disk.get_physical_drive_number() != -1,
                      [WindowsSCSIBlockDevice(device) for device in self.get_device_manager().disk_drives])

    @cached_method
    def get_all_storage_controller_devices(self):
        from infi.devicemanager.setupapi.constants import SYSTEM_DEVICE_GUID_STRING
        return filter(lambda device: u'ScsiArray' in device.hardware_ids,
                      [WindowsSCSIStorageController(device) for device in self.get_device_manager().scsi_devices])

class LazyLoadBalancingInfomrationDict(LazyImmutableDict):
    # Getting the load balancing information in Windows requires a seperate WQL call,
    # which is exepnsive. So we do not want to execute it unless the get_policy information is asked for
    # Fetching the get_policy information from WMI returns the information for all the devices,
    # not just for a specific one, so we must not execute it for every device
    # This is the mechanism I found suitable:
    # On the first call to the dict, it fetches the key and values from WMI and uses them from here on
    def __init__(self, wmi_client):
        # this (skipping the __init__ of LazyImmutableDict is on purpose 
        super(LazyImmutableDict, self).__init__()
        self.wmi_client = wmi_client

    @cached_property
    def _dict(self):
        from infi.wmpio import get_load_balace_policies
        return get_load_balace_policies(self.wmi_client)

MPIO_BUS_DRIVER_INSTANCE_ID = u"Root\\MPIO\\0000"

class WindowsNativeMultipathModel(multipath.NativeMultipathModel):
    @cached_method
    def get_all_multipath_devices(self):
        from infi.devicemanager import DeviceManager
        from infi.wmpio import WmiClient, get_multipath_devices

        device_manager = DeviceManager()
        wmi_client = WmiClient()

        devices = filter(lambda device: device.parent._instance_id == MPIO_BUS_DRIVER_INSTANCE_ID,
                         device_manager.disk_drives)
        multipath_dict = get_multipath_devices(wmi_client)
        policies_dict = LazyLoadBalancingInfomrationDict(wmi_client)
        return [WindowsNativeMultipathDevice(device_object,
                                       multipath_dict[u"%s_0" % device_object._instance_id],
                                       policies_dict) for device_object in devices]

    def filter_non_multipath_scsi_block_devices(self, scsi_block_devices):
        return filter(lambda device: device.get_parent()._instance_id != MPIO_BUS_DRIVER_INSTANCE_ID,
                         scsi_block_devices)

class WindowsFailoverOnly(multipath.FailoverOnly):
    def __init__(self, device):
        active_path_id = None
        for path in device.get_paths():
            if path.get_state() == 'up':
                active_path_id = path.get_path_id()
            print path.get_state()
        super(WindowsFailoverOnly, self).__init__(active_path_id)

class WindowsRoundRobin(multipath.RoundRobin):
    pass

class WindowsRoundRobinWithSubset(multipath.RoundRobinWithSubset):
    def __init__(self, device):
        active_paths = filter(lambda path: path.get_state() == 'up', device.get_paths())
        active_path_ids = [path.get_path_id() for path in active_paths]
        super(WindowsRoundRobinWithSubset, self).__init__(active_path_ids)

class WindowsWeightedPaths(multipath.WeightedPaths):
    def __init__(self, wmpio_policy):
        weights = dict([(path.DsmPathId, path.PathWeight) for path in wmpio_policy.DSM_Paths])
        super(WindowsWeightedPaths, self).__init__(weights)

class WindowsLeastBlocks(multipath.LeastBlocks):
    pass

class WindowsLeastQueueDepth(multipath.LeastQueueDepth):
    pass

class WindowsNativeMultipathDevice(WindowsDiskDeviceMixin, WindowsDeviceMixin, multipath.MultipathDevice):
    def __init__(self, device_object, multipath_object, policies_dict):
        super(WindowsNativeMultipathDevice, self).__init__()
        self._device_object = device_object
        self._multipath_object = multipath_object
        self._policies_dict = policies_dict

    @cached_method
    def get_device_access_path(self):
        return self.get_pdo()

    @cached_method
    def get_paths(self):
        return [WindowsPath(item) for item in self._multipath_object.PdoInformation]

    @cached_method
    def get_policy(self):
        from infi.wmpio.mpclaim import FAIL_OVER_ONLY, ROUND_ROBIN, ROUND_ROBIN_WITH_SUBSET, \
                                       WEIGHTED_PATHS, LEAST_BLOCKS, LEAST_QUEUE_DEPTH
        wmpio_policy = self._policies_dict["%s_0" % self.get_instance_id()]
        policy_number = wmpio_policy.LoadBalancePolicy
        if policy_number == FAIL_OVER_ONLY:
            return WindowsFailoverOnly(self)
        if policy_number == ROUND_ROBIN:
            return WindowsRoundRobin()
        if policy_number == ROUND_ROBIN_WITH_SUBSET:
            return WindowsRoundRobinWithSubset(self)
        if policy_number == WEIGHTED_PATHS:
            return WindowsWeightedPaths(wmpio_policy)
        if policy_number == LEAST_BLOCKS:
            return WindowsLeastBlocks()
        if policy_number == LEAST_QUEUE_DEPTH:
            return WindowsLeastQueueDepth()

class WindowsPath(multipath.Path):
    def __init__(self, pdo_information):
        super(WindowsPath, self).__init__()
        self._pdo_information = pdo_information

    @cached_method
    def get_path_id(self):
        return self._pdo_information.PathIdentifier

    @cached_method
    def get_hctl(self):
        from ..dtypes import HCTL
        scsi_address = self._pdo_information.ScsiAddress
        return HCTL(scsi_address.PortNumber, scsi_address.ScsiPathId, scsi_address.TargetId, scsi_address.Lun)

    @cached_method
    def get_state(self):
        return "up"

class WindowsStorageModel(StorageModel):
    def _create_scsi_model(self):
        return WindowsSCSIModel()

    def _create_native_multipath_model(self):
        return WindowsNativeMultipathModel()

    def initiate_rescan(self):
        from infi.devicemanager import DeviceManager
        dm = DeviceManager()
        for controller in dm.storage_controllers:
            if not controller.is_real_device():
                continue
            controller.rescan()
