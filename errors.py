class CalendarPluginError(Exception):
    """Base error safe to show to the model."""


class ConfigurationError(CalendarPluginError):
    pass


class AuthenticationError(CalendarPluginError):
    pass


class AccessDeniedError(CalendarPluginError):
    pass


class NotFoundError(CalendarPluginError):
    pass


class ConflictError(CalendarPluginError):
    pass


class ResponseTooLarge(CalendarPluginError):
    pass
