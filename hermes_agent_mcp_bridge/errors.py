"""Safe, stable error values. Messages are fixed and contain no request data."""


class ConfigError(ValueError):
    """The configuration is missing or refuses to start."""


class HermesFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OAuthError(Exception):
    def __init__(self, status: int, code: str, description: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.description = description
