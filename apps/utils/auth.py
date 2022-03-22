from rest_framework.authtoken.models import Token
from functools import wraps
from django.http import JsonResponse

token = None
def get_token():
    global token
    if token == None:
        token = Token.generate_key()
    return token

def return_unauthorized():
    return JsonResponse({},status=401)

def is_logged(f):
    @wraps(f)
    def wrapper(request, *args, **kwds):
        token_header = request.headers.get('X-GREFFON-TOKEN')
        if token != token_header:
            return return_unauthorized()
        return f(request, *args, **kwds)
    return wrapper

