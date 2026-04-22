import secrets
from functools import wraps


# Decoupled from ``rest_framework.authtoken.models.Token`` and
# ``django.http.JsonResponse`` module-level imports. This module imports
# cleanly in both runtimes (Django + FastAPI) and in environments without
# Django configured (e.g. pytest on Python 3.13, where Django 3.2's
# ``django.http.request`` fails because the stdlib ``cgi`` module was
# removed). Feature #4 deletes this module outright once Django goes.

token = None


def get_token():
    global token
    if token is None:
        # ``secrets.token_urlsafe(32)`` returns a ~43-char base64 string;
        # DRF's ``Token.generate_key()`` returns a 40-char hex string. Both
        # are opaque, high-entropy bearer tokens. Format differs, security
        # equivalent.
        token = secrets.token_urlsafe(32)
    return token


def return_unauthorized():
    # Lazy-import Django here so the module imports without Django
    # configured. Used only by the @is_logged decorator (Django-only path).
    from django.http import JsonResponse

    return JsonResponse({}, status=401)


def is_logged(f):
    @wraps(f)
    def wrapper(request, *args, **kwds):
        token_header = request.headers.get('X-GREFFON-TOKEN')
        if token != token_header:
            return return_unauthorized()
        return f(request, *args, **kwds)

    return wrapper
