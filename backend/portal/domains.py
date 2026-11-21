"""Owner-managed HTTPS hostnames and the internal allow-list endpoint."""
import ipaddress
import re

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.http.request import split_domain_port
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .access import access
from .ingest import internal_allowed
from .models import AuditEvent, CustomDomain


LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
SUFFIX = re.compile(r'(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})')
SPECIAL_SUFFIXES = {'arpa', 'example', 'internal', 'invalid', 'lan', 'local', 'localdomain',
                    'localhost', 'onion', 'test'}


def public_hostname(value):
    if not value or not value.isascii():
        raise ValueError
    hostname = value.lower().removesuffix('.')
    labels = hostname.split('.')
    if (len(hostname) > 253 or len(labels) < 2 or not SUFFIX.fullmatch(labels[-1])
            or labels[-1] in SPECIAL_SUFFIXES or not all(LABEL.fullmatch(s) for s in labels)):
        raise ValueError
    return hostname


def fixed_hosts():
    return {*settings.SMA_STATIC_ALLOWED_HOSTS, settings.SMA_OWNER_HOST,
            *settings.SMA_OWNER_ALIASES, *settings.SMA_TENANT_HOSTS}


class ApprovedHostMiddleware:
    """Validate every Host before Django's security, cookie and CSRF middleware."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_custom_domain = False
        if settings.SMA_DOMAIN_MANAGEMENT:
            # get_host() still enforces Django's header grammar with ALLOWED_HOSTS=['*'].
            host, _ = split_domain_port(request.get_host())
            if host not in fixed_hosts():
                try:
                    name = public_hostname(host)
                except ValueError:
                    return HttpResponseBadRequest('Endereço do SMA inválido.')
                if not CustomDomain.objects.filter(hostname=name, enabled=True,
                                                   status=CustomDomain.State.ACTIVE).exists():
                    return HttpResponseBadRequest('Endereço do SMA indisponível.')
                request.is_custom_domain = True
        return self.get_response(request)


class DomainForm(forms.Form):
    hostname = forms.CharField(label='Nome do domínio', max_length=254,
                               widget=forms.TextInput(attrs={'placeholder':'central.exemplo.com.br',
                                                             'autocomplete':'off', 'spellcheck':'false'}))

    def clean_hostname(self):
        try:
            name = public_hostname(self.cleaned_data['hostname'])
        except ValueError:
            raise ValidationError('Use um domínio público, sem protocolo, porta, caminho ou curinga. Para nomes internacionais, use o formato Punycode.') from None
        if name in fixed_hosts():
            raise ValidationError('Este endereço já pertence à configuração da instalação.')
        return name


def check_domain_admin(request):
    if not settings.SMA_DOMAIN_MANAGEMENT or not request.is_owner_portal:
        raise Http404


def log_change(actor, action, domain):
    AuditEvent.objects.create(actor=actor, owner_only=True, action=action,
                             target=f'{domain.pk} {domain.hostname}'[:150])


@access(owner=True, scope=False)
@require_http_methods(['GET','POST'])
def domains(request):
    check_domain_admin(request)
    form = DomainForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            domain, created = CustomDomain.objects.get_or_create(hostname=form.cleaned_data['hostname'])
            if created:
                log_change(request.user, 'Domínio cadastrado', domain)
        if created:
            messages.success(request, 'Domínio cadastrado. A verificação do DNS começará automaticamente.')
            return redirect('domains')
        form.add_error('hostname', 'Este domínio já está cadastrado.')
    return render(request, 'domains.html', {'form':form, 'domains':CustomDomain.objects.all(),
                  'public_ip':settings.SMA_PUBLIC_IP, 'public_ipv6':settings.SMA_PUBLIC_IPV6}, status=400 if form.is_bound and form.errors else 200)


@access(owner=True, scope=False)
@require_POST
def domain_state(request, pk):
    check_domain_admin(request)
    actions = request.POST.getlist('action')
    if len(actions) != 1 or actions[0] not in ('enable','disable','retry'):
        return HttpResponseBadRequest('Operação inválida.')
    action = actions[0]
    with transaction.atomic():
        domain = get_object_or_404(CustomDomain.objects.select_for_update(), pk=pk)
        change = ((action == 'enable' and not domain.enabled) or (action == 'disable' and domain.enabled)
                  or (action == 'retry' and domain.enabled and domain.status != CustomDomain.State.ACTIVE))
        if change:
            domain.enabled = action != 'disable'
            domain.status = CustomDomain.State.PENDING
            domain.detail = ''
            domain.checked_at = None
            domain.save()
            label = {'enable':'Domínio reativado', 'disable':'Domínio desativado',
                     'retry':'Nova verificação de domínio'}[action]
            log_change(request.user, label, domain)
    # Host-only cookies require a fresh sign-in at the IP after disabling this alias.
    if action == 'disable' and request.is_custom_domain:
        current, _ = split_domain_port(request.get_host())
        if current == domain.hostname:
            return redirect(f'https://{settings.SMA_OWNER_HOST}{reverse("domains")}')
    return redirect('domains')


@require_GET
def domain_permit(request):
    host, _ = split_domain_port(request.get_host())
    if not settings.SMA_DOMAIN_MANAGEMENT or host != 'backend' or not internal_allowed(request):
        return HttpResponse(status=403)
    fields = set(request.GET)
    if fields not in ({'domain'}, {'domain', 'access'}) or len(request.GET.getlist('domain')) != 1:
        return HttpResponse(status=403)
    if 'access' in fields and request.GET.getlist('access') != ['1']:
        return HttpResponse(status=403)
    value = request.GET['domain']
    if settings.SMA_SINGLE_HOST and value == settings.SMA_OWNER_HOST:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            pass  # A named static Central is checked with the other public hostnames.
        else:
            allowed = (value == str(address) and not address.is_loopback and not address.is_unspecified
                       and not address.is_link_local and not address.is_multicast)
            return HttpResponse(status=204 if allowed else 403)
    try:
        name = public_hostname(value)
    except ValueError:
        return HttpResponse(status=403)
    if name in {settings.SMA_OWNER_HOST, *settings.SMA_OWNER_ALIASES, *settings.SMA_TENANT_HOSTS}:
        return HttpResponse(status=204)
    states = (CustomDomain.State.ACTIVE,) if 'access' in fields else (
        CustomDomain.State.PROVISIONING, CustomDomain.State.TLS_ERROR, CustomDomain.State.ACTIVE)
    allowed = (name not in fixed_hosts() and
               CustomDomain.objects.filter(hostname=name, enabled=True, status__in=states).exists())
    return HttpResponse(status=204 if allowed else 403)
