class RetryLater(RuntimeError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = max(0, min(600, float(retry_after)))


class JellyfinPersonNotFound(RuntimeError):
    pass
