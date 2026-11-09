import json
import os
import subprocess


def config_file():
    """Config path, separate per tmux session when inside tmux."""
    if os.environ.get("TMUX"):
        session = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True).stdout.strip()
        if session:
            return os.path.expanduser(f"~/.config/.dabctl-{session}.json")
    return os.path.expanduser("~/.config/.dabctl.json")


def load_config():
    """Load config from JSON file."""
    path = config_file()
    if not os.path.exists(path):
        return {}

    with open(path, "r") as f:
        return json.load(f)


def save_config(config):
    """Save config to JSON file."""
    path = config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def get_current():
    """Get current settings."""
    return load_config()


def set_current(**kwargs):
    """Set current settings."""
    config = load_config()
    config.update(**kwargs)
    save_config(config)
