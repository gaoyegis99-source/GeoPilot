"""Structured failures while opening a local asset source."""


class SourceReadError(RuntimeError):
    code = "SOURCE_UNREADABLE"
    phase = "data"
    suggestion = "Check the local source and layer, then register it again."

    def __init__(self, message: str, *, field: str = "source.path") -> None:
        super().__init__(message)
        self.field = field
        self.message = message
