"""In-game renderer: first-person footage recorded with the real CS2 client. See ``docs/ingame-recording.md``.

The recording technique (plugin loaded through a temporary ``gameinfo.gi`` search path, ``<demo>.json`` actions
file, ``startmovie``) is a port of CS Demo Manager by AkiVer (MIT, https://github.com/akiver/cs-demo-manager).
"""

from .renderer import IngameRenderer
from .settings import IngameSettings, load

__all__ = ["IngameRenderer", "IngameSettings", "load"]
