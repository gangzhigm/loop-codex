"""旧 Scheduler 运行时导入的兼容层；新代码统一使用 ``service_runtime``。"""

from common.service_runtime import ServiceRuntimeFiles, install_shutdown_signals


SchedulerRuntimeFiles = ServiceRuntimeFiles

__all__ = ["SchedulerRuntimeFiles", "install_shutdown_signals"]
