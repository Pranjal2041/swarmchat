"""swarmchat: a tiny messaging server and CLI for multi-agent systems."""
import os

NAME = "swarmchat"  # the command, the config folder and the environment prefix all come from this one name
ENV = NAME.upper()  # SWARMCHAT_SERVER, SWARMCHAT_TOKEN, SWARMCHAT_ADMIN_TOKEN, SWARMCHAT_CONFIG_DIR, SWARMCHAT_DATA_DIR
CONFIG_DIR = os.environ.get(f"{ENV}_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), NAME)
# The server's folder when --data is not given, so serve, admin and view find the same one from any directory.
DATA_DIR = os.environ.get(f"{ENV}_DATA_DIR") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), NAME)
__version__ = "0.1.0"
