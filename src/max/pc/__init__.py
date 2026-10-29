"""Windows-first personal desktop UI and explicit local commands.

Imported lazily: headless Railway/Discord services require no desktop libraries.
"""
from .control import PCController, PCReply, ProfileStore

__all__ = ["PCController", "PCReply", "ProfileStore"]
