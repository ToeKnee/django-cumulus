from django.conf import settings

CUMULUS = {
    'API_KEY': None,
    'CNAMES': None,
    'CONTAINER': None,
    'SERVICENET': False,
    'TIMEOUT': 5,
    'MAX_RETRIES': 5,
    'CONNECTION_ARGS': {},
    'TTL': 86400,  # (24h)
    'USE_SSL': False,
    'USERNAME': None,
    'STATIC_CONTAINER': None,
    'FILTER_LIST': [],
    'HEADERS': {},
    'PYRAX_IDENTITY_TYPE': 'keystone'
}

if hasattr(settings, 'CUMULUS'):
    CUMULUS.update(settings.CUMULUS)


# backwards compatibility for old-style cumulus settings
if not hasattr(settings, 'CUMULUS') and hasattr(settings, 'CUMULUS_API_KEY'):
    import warnings
    warnings.warn(
        "settings.CUMULUS_* is deprecated; use settings.CUMULUS instead.",
        PendingDeprecationWarning
    )

    CUMULUS.update({
        'API_KEY': getattr(settings, 'CUMULUS_API_KEY'),
        'CNAMES': getattr(settings, 'CUMULUS_CNAMES', None),
        'CONTAINER': getattr(settings, 'CUMULUS_CONTAINER'),
        'SERVICENET': getattr(settings, 'CUMULUS_USE_SERVICENET', False),
        'TIMEOUT': getattr(settings, 'CUMULUS_TIMEOUT', 5),
        'TTL': getattr(settings, 'CUMULUS_TTL', 8400),
        'USERNAME': getattr(settings, 'CUMULUS_USERNAME'),
    })
