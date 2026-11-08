"""OpenSwarm Native Desktop Application for Linux (Libadwaita / GTK4 / WebKitGTK 6.0)."""

from .app import desktop_main, start_server, ServerHandle, gtk_available, choose_backend
from .config import DesktopConfig
from .installer import install_desktop, APP_ID, PET_APP_ID
from .pet import OpenSwarmPetWindow
from .window import OpenSwarmMainWindow

__all__ = [
    "desktop_main",
    "start_server",
    "ServerHandle",
    "gtk_available",
    "choose_backend",
    "DesktopConfig",
    "install_desktop",
    "APP_ID",
    "PET_APP_ID",
    "OpenSwarmMainWindow",
    "OpenSwarmPetWindow",
]
