import os
from django.core.wsgi import get_wsgi_application
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sma.settings')
if os.environ.get('SMA_TEST_SQLITE') == '1':
    raise RuntimeError('O modo de testes não pode servir a aplicação WSGI.')
application = get_wsgi_application()
