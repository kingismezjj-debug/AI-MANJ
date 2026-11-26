class AdapterError(Exception):
    """Base error for provider calls. The router may fall back after this."""


class ProviderNotConfigured(AdapterError):
    """Missing API key or endpoint. Safe to fall back."""


class ProviderResponseError(AdapterError):
    """The provider answered, but the payload cannot be used."""


class TransientProviderError(AdapterError):
    """Network or rate-limit failure. HTTP helpers retry these before raising."""
