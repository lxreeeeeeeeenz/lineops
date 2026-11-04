class FGVError(Exception):
    status = 400
    code = "erro"

    def __init__(self, message, *, code=None, status=None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details


class NotFound(FGVError):
    status, code = 404, "nao_encontrado"


class Conflict(FGVError):
    status, code = 409, "conflito"
