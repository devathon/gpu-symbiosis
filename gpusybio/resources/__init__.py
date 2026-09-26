"""Resource management (spec 07): GPU backends, attribution, reservations."""

from gpusybio.resources.gpu import GpuBackend, FakeGpuBackend, NvmlBackend, make_backend
from gpusybio.resources.manager import ResourceManager, make_resource_manager

__all__ = [
    "FakeGpuBackend",
    "GpuBackend",
    "NvmlBackend",
    "ResourceManager",
    "make_backend",
    "make_resource_manager",
]
