class BusinessError(Exception):
    """Refus attendu : données publiques autorisées uniquement dans details."""

    def __init__(self, code, message, *, status=409, details=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def response(self):
        return {"error": {"code": self.code, "message": self.message, **self.details}}
