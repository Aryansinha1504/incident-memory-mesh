"""Run the Incident Response Memory Mesh.

The application lives in the ``memory_mesh`` package; this file remains a
small, familiar entry point for the hackathon demo.
"""

from memory_mesh.server import serve, service
from memory_mesh.service import MemoryService

__all__ = ["MemoryService", "service", "serve"]


if __name__ == "__main__":
    serve()
