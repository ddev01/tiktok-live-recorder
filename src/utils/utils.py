import json
import os
from http.cookies import SimpleCookie

from utils.enums import Info


def banner() -> None:
    """
    Prints a banner with the name of the tool and its version number.
    """
    print(Info.BANNER, flush=True)


def read_cookies():
    """
    Loads Docker's cookie secret when available, otherwise the CLI config file.
    """
    secret_path = os.environ.get(
        "TIKTOK_SESSION_SS_FILE", "/run/secrets/tiktok_session_ss"
    )
    if os.path.isfile(secret_path):
        with open(secret_path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        try:
            cookies = json.loads(value)
            if isinstance(cookies, dict):
                return cookies
        except json.JSONDecodeError:
            pass

        parsed = SimpleCookie()
        parsed.load(value)
        if parsed:
            return {key: morsel.value for key, morsel in parsed.items()}
        if value:
            return {"session_ss": value}

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "cookies.json")
    with open(config_path, "r") as f:
        return json.load(f)


def read_telegram_config():
    """
    Loads the telegram config file and returns it.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "telegram.json")
    with open(config_path, "r") as f:
        return json.load(f)


def is_termux() -> bool:
    """
    Checks if the script is running in Termux.

    Returns:
        bool: True if running in Termux, False otherwise.
    """
    import distro
    import platform

    return platform.system().lower() == "linux" and distro.like() == ""


def is_windows() -> bool:
    """
    Checks if the script is running on Windows.

    Returns:
        bool: True if running on Windows, False otherwise.
    """
    import platform

    return platform.system().lower() == "windows"


def is_linux() -> bool:
    """
    Checks if the script is running on Linux.

    Returns:
        bool: True if running on Linux, False otherwise.
    """
    import platform

    return platform.system().lower() == "linux"
