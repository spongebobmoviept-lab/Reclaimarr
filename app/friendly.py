"""Plain-English error messages for the setup wizard's connection tests.

A raw httpx exception ("[Errno -2] Name or service not known", a stack of
URLs with API keys in them) is useless to someone trying the app for the
first time. These map the common failures to what to actually check, and
never echo a secret back.
"""

import httpx

from .logger import _redact


def explain(exc: Exception, service: str) -> str:
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)) or "missing an 'http://'" in str(exc):
        return f"That {service} address isn't a valid URL. Include http:// (or https://) and the port, e.g. http://192.168.1.50:7878."
    if isinstance(exc, httpx.ConnectError):
        return (
            f"Couldn't reach {service} at that address. Check the IP and port, and that {service} is running. "
            "'localhost' means this container itself, so use the machine's LAN IP or the container name instead."
        )
    if isinstance(exc, httpx.TimeoutException):
        return f"{service} didn't answer within 10 seconds. Check the address, or whether a firewall is blocking it."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"{service} answered but rejected the key/token/password. Copy it again and make sure nothing was cut off."
        if code == 404:
            return f"Something answered at that address, but it doesn't look like {service}. Check the port (and any URL base path)."
        return f"{service} answered with an error (HTTP {code})."
    message = _redact(str(exc)) or exc.__class__.__name__
    return f"Couldn't connect to {service}: {message}"
