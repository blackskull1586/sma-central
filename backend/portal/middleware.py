from django.conf import settings
from django.core.exceptions import ValidationError
from django.http.request import split_domain_port
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import redirect, render
from .models import Environment
from .access import valid_unit_scope

class AccessContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.environment = None
        host, _ = split_domain_port(request.get_host())
        owner_address = request.is_custom_domain or host == settings.SMA_OWNER_HOST or host in settings.SMA_OWNER_ALIASES
        request.is_shared_portal = request.is_custom_domain or (settings.SMA_SINGLE_HOST and host == settings.SMA_OWNER_HOST)
        request.is_owner_portal = owner_address and (
            not request.is_shared_portal or (request.user.is_authenticated and request.user.is_owner)
        )
        domain_slug = None if owner_address else settings.SMA_TENANT_HOSTS.get(host)
        internal_callback = request.path.startswith(('/internal/rtmp/', '/internal/relay/')) or request.path in ('/internal/realtime/publish/', '/internal/domains/permit/')
        if (settings.SMA_OWNER_HOST and not owner_address and not domain_slug
                and request.path != '/healthz' and not internal_callback):
            return HttpResponseNotFound('Endereço do SMA indisponível.')
        request.domain_environment = Environment.objects.filter(slug=domain_slug).first() if domain_slug else None
        if domain_slug and request.domain_environment is None:
            return HttpResponseNotFound('Ambiente indisponível.')
        if request.user.is_authenticated:
            if request.user.is_owner:
                selected = request.session.get('environment_id')
                if selected:
                    try:
                        request.environment = Environment.objects.filter(pk=selected).first()
                    except (ValueError, TypeError, ValidationError):
                        request.session.pop('environment_id', None)
                if request.environment is None:
                    request.environment = request.domain_environment
            else:
                request.environment = request.user.environment
        exempt = request.path in ['/login/', '/sair/', '/conta/senha/', '/healthz'] or request.path.startswith('/internal/')
        wrong_domain = (request.user.is_authenticated and not request.user.is_owner
                        and request.domain_environment is not None
                        and request.user.environment_id != request.domain_environment.pk)
        wrong_owner_portal = (bool(settings.SMA_OWNER_HOST) and request.user.is_authenticated
                              and request.user.is_owner != request.is_owner_portal)
        if (request.user.is_authenticated and not valid_unit_scope(request.user)
                and request.path not in ['/sair/', '/healthz']):
            response = HttpResponseForbidden('A unidade desta conta está indisponível.')
        elif wrong_owner_portal and request.path not in ['/sair/', '/healthz']:
            if (request.user.is_owner and request.method in ['GET', 'HEAD']
                    and request.path != '/streams.json'
                    and not request.path.startswith(('/api/', '/internal/'))):
                response = redirect(f'https://{settings.SMA_OWNER_HOST}/login/')
            else:
                response = render(request, 'error.html', {'title': 'Acesso restrito',
                    'detail': 'Use o endereço correspondente à sua conta.'}, status=403)
        elif wrong_domain and request.path not in ['/sair/', '/healthz']:
            response = render(request, 'error.html', {'title': 'Acesso de outro ambiente',
                'detail': 'Entre pelo endereço do seu ambiente ou saia para usar outra conta.'}, status=403)
        elif request.user.is_authenticated and not exempt:
            if not request.user.is_owner and (not request.environment or not request.environment.active):
                response = HttpResponseForbidden('Este ambiente está desativado.')
            elif request.user.must_change_password:
                if request.path == '/streams.json' or request.path.startswith('/api/'):
                    response = HttpResponseForbidden('Atualize sua senha para continuar.')
                else:
                    response = redirect('password_change')
            else:
                response = self.get_response(request)
        else:
            response = self.get_response(request)
        response['Cache-Control'] = 'no-store, private'
        response['X-Frame-Options'] = 'DENY'
        response['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        return response
