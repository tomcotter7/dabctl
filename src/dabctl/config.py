import json
import os

CONFIG_FILE = os.path.expanduser("~/.config/.dabctl.json")


def load_config():
    """Load config from JSON file."""
    if not os.path.exists(CONFIG_FILE):
        return {}

    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(config):
    """Save config to JSON file."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_current():
    """Get current settings."""
    return load_config()


def set_current(**kwargs):
    """Set current settings."""
    config = load_config()
    config.update(**kwargs)
    save_config(config)
