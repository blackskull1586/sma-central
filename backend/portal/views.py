import re
import time
from urllib.parse import urlsplit
from uuid import UUID
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction, IntegrityError
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from .access import access, audit, unit_records, valid_unit_scope
from .forms import LoginForm, EnvironmentCreateForm, AccountCreateForm, AccountUnitForm, ChannelForm, ChannelViewersForm, UnitCreateForm, UnitEditForm
from .models import User, Environment, Channel, ChannelInput, Unit, AuditEvent
from .ingest import (internal_allowed, rtmp_callback, rtmp_relay_callback, realtime_publish_authorize,
                     source_channel, current_source_key)

class PortalLoginView(LoginView):
    template_name = 'login.html'
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        if self.request.is_shared_portal:
            user = self.request.user
            if user.must_change_password:
                return reverse('password_change')
            if user.is_owner:
                return reverse('environments')
            return self.get_redirect_url() or reverse('dashboard' if user.can_manage else 'monitor')
        if self.request.user.is_owner and getattr(self.request, 'is_owner_portal', False):
            return reverse('environments')
        return super().get_success_url()

class PortalLogoutView(LogoutView):
    pass

def form_page(request, form, title, back='dashboard', note=''):
    context = {'form': form, 'title': title, 'back': back, 'note': note}
    if request.user.is_authenticated and request.user.is_owner:
        name = request.resolver_match.url_name
        guidance = {
            'environment_create': ('Novo servidor', 'Criar servidor',
                ('O administrador usará este mesmo endereço para entrar no servidor criado.' if request.is_shared_portal
                 else 'Cadastre um servidor SMA e seu primeiro administrador. O endereço de acesso deve estar configurado antes de distribuir as contas.')),
            'unit_create': ('Novo ambiente', 'Criar ambiente',
                'Escolha o servidor e dê um nome à unidade ou equipe. Depois, vincule seus usuários a este ambiente.'),
            'unit_edit': (title, 'Salvar nome', 'A mudança do nome mantém os usuários, canais e permissões existentes.'),
            'account_create': ('Novo usuário', 'Criar usuário',
                'Escolha o perfil e o ambiente. O usuário deverá trocar a senha inicial no primeiro acesso.'),
            'account_unit': (title, 'Salvar ambiente', ''),
            'account_password': (title, 'Redefinir senha',
                'As sessões atuais serão encerradas. O usuário deverá trocar a nova senha no próximo acesso.'),
            'channel_create': ('Novo canal', 'Criar canal',
                'Defina o nome e o ambiente responsável. Na próxima etapa, copie a chave de transmissão e escolha quem pode assistir.'),
            'channel_edit': ('Renomear canal', 'Salvar nome',
                'O novo nome será exibido em todos os vídeos deste canal. A chave e as permissões serão mantidas.'),
            'password_change': (title, 'Atualizar minha senha', ''),
        }
        display_title, submit_label, description = guidance.get(name, (title, 'Salvar alterações', ''))
        anchor = '#usuarios' if name.startswith('account_') else '#canais' if name.startswith('channel_') else ''
        context.update(title=display_title, submit_label=submit_label, form_description=description,
                       back_url=reverse(back) + anchor,
                       back_label={'dashboard': 'Voltar aos usuários' if anchor == '#usuarios' else 'Voltar aos canais',
                                   'environments': 'Voltar aos servidores', 'units': 'Voltar aos ambientes',
                                   'monitor': 'Voltar ao monitoramento'}.get(back, 'Voltar'),
                       form_environment=request.environment if back == 'dashboard' else None)
    return render(request, 'form.html', context,
                  status=400 if form.is_bound and form.errors else 200)

@require_GET
def health(request):
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return HttpResponse('ok\n', content_type='text/plain')

def csrf_failure(request, reason=''):
    return render(request, 'error.html', {'title': 'Sessão do formulário expirada',
        'detail': 'Atualize a página e tente novamente.'}, status=403)

def monitoring_environment(request):
    """A viewing filter never changes the environment selected for management."""
    choices = request.GET.getlist('sma')
    if len(choices) > 1:
        raise Http404('Seleção de SMA inválida.')
    if not request.user.is_owner:
        if choices and choices[0] != str(request.user.environment_id):
            raise PermissionDenied('Use o SMA da sua conta.')
        if not request.environment or not request.environment.active:
            raise PermissionDenied('Este ambiente está desativado.')
        return request.environment
    selected = choices[0] if choices else 'all'
    if selected == 'all':
        return None
    try:
        environment_id = UUID(selected)
    except (ValueError, TypeError, AttributeError):
        raise Http404('SMA não encontrado.') from None
    return get_object_or_404(Environment, pk=environment_id, active=True)


def readable_channels(request):
    channels = Channel.objects.filter(active=True, environment__active=True).select_related('environment')
    user = request.user
    if not user.is_authenticated or not user.is_active or not valid_unit_scope(user):
        return channels.none()
    if user.is_owner:
        return channels
    grants = Channel.allowed_viewers.through.objects.filter(channel_id=OuterRef('pk'), user_id=user.pk)
    return channels.filter(environment_id=user.environment_id).alias(
        audience_allowed=Exists(grants),
    ).filter(Q(audience_restricted=False) | Q(audience_allowed=True))


def managed_channels(request):
    return unit_records(request, Channel.objects.filter(environment=request.environment))


def readable_media_source(request, stream_id):
    channels = readable_channels(request)
    channel = channels.filter(stream_id=stream_id).first()
    if channel is not None:
        return channel
    source = ChannelInput.objects.filter(channel__in=channels, stream_id=stream_id).select_related(
        'channel__environment').first()
    return source if source is not None and current_source_key(source) else None


@require_GET
@access(scope=False)
def monitor(request):
    environment = monitoring_environment(request)
    scope = str(environment.pk) if environment else 'all'
    owner = request.user.is_owner
    return render(request, 'monitor.html', {
        'monitor_environment': environment,
        'monitor_environments': Environment.objects.filter(active=True) if owner else [],
        'monitor_title': environment.name if environment else 'Todos os SMA',
        'catalog_url': '/streams.json?sma=' + scope if owner else '/streams.json',
        'storage_scope': f'{request.user.pk}:monitor:{scope}' if owner else f'{request.user.pk}:{scope}',
        'show_environment': owner and environment is None,
        'default_select_all': owner,
    })

@require_GET
@access(manager=True)
def dashboard(request):
    env = request.environment
    accounts = unit_records(request, env.users.all()).select_related('unit').order_by('role', 'username')
    channels = list(managed_channels(request).select_related('unit').prefetch_related('inputs'))
    for channel in channels:
        channel.online = any(is_online(source) for source in [channel, *channel.inputs.all()])
    return render(request, 'dashboard.html', {'accounts': accounts, 'channels': channels,
        'events': unit_records(request, env.events.all()).select_related('actor')[:12]})

@require_GET
@access(owner=True, scope=False)
def environments(request):
    items = Environment.objects.annotate(user_count=Count('users', distinct=True), channel_count=Count('channels', distinct=True)).order_by('name', 'id')
    return render(request, 'environments.html', {'environments': items})

@sensitive_post_parameters('password1', 'password2')
@sensitive_variables()
@require_http_methods(['GET', 'POST'])
@access(owner=True, scope=False)
def environment_create(request):
    form = EnvironmentCreateForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                env = Environment.objects.create(name=data['name'], slug=data['slug'])
                user = User.objects.create_user(username=data['username'], password=data['password1'],
                    environment=env, role=User.Role.ADMIN, must_change_password=True)
                user.remember_provisional_password(data['password1'])
                user.save(update_fields=['provisional_password_ciphertext'])
                audit(request, 'Ambiente e administrador criados', env.name, env)
        except IntegrityError:
            form.add_error(None, 'O identificador ou usuário já está em uso. Atualize os dados.')
        else:
            messages.success(request, 'Servidor criado. O administrador deverá trocar a senha no primeiro acesso.')
            return redirect('environments')
    return form_page(request, form, 'Novo servidor e administrador', 'environments')

@require_POST
@access(owner=True, scope=False)
def environment_select(request, pk):
    env = get_object_or_404(Environment, pk=pk, active=True)
    request.session['environment_id'] = str(env.pk)
    return redirect('dashboard')

@require_POST
@access(owner=True, scope=False)
def environment_state(request, pk):
    if request.POST.get('active') not in ['0', '1']:
        return HttpResponse(status=400)
    with transaction.atomic():
        env = get_object_or_404(Environment.objects.select_for_update(), pk=pk)
        env.active = request.POST['active'] == '1'
        env.save(update_fields=['active'])
        audit(request, 'Ambiente ativado' if env.active else 'Ambiente desativado', env.name, env)
    return redirect('environments')

@require_GET
@access(owner=True, scope=False)
def units(request):
    items = Unit.objects.select_related('environment').annotate(
        user_count=Count('users', distinct=True), channel_count=Count('channels', distinct=True)
    ).order_by('environment__name', 'name', 'id')
    return render(request, 'units.html', {'units': items})


@require_http_methods(['GET', 'POST'])
@access(owner=True, scope=False)
def unit_create(request):
    initial = {'environment': request.environment.pk} if request.environment and request.environment.active else {}
    form = UnitCreateForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            env = get_object_or_404(Environment.objects.select_for_update(), pk=form.cleaned_data['environment'].pk, active=True)
            name = form.cleaned_data['name']
            if Unit.objects.filter(environment=env, name__iexact=name).exists():
                form.add_error('name', 'Já existe um ambiente com esse nome neste servidor.')
            else:
                base = slugify(name)[:40] or 'ambiente'
                slug, suffix = base, 2
                while Unit.objects.filter(environment=env, slug=slug).exists():
                    slug = f'{base}-{suffix}'
                    suffix += 1
                unit = Unit(environment=env, name=name, slug=slug)
                unit.full_clean()
                unit.save()
                audit(request, 'Ambiente interno criado', unit.name, env, unit=unit)
                messages.success(request, 'Ambiente criado. Agora você pode cadastrar ou vincular seus usuários.')
                return redirect('units')
    return form_page(request, form, 'Novo ambiente', 'units')


@require_http_methods(['GET', 'POST'])
@access(owner=True, scope=False)
def unit_edit(request, pk):
    unit = get_object_or_404(Unit.objects.select_related('environment'), pk=pk, environment__active=True)
    if request.method == 'POST':
        with transaction.atomic():
            get_object_or_404(Environment.objects.select_for_update(), pk=unit.environment_id, active=True)
            unit = get_object_or_404(Unit.objects.select_for_update(), pk=pk, environment_id=unit.environment_id)
            form = UnitEditForm(request.POST, unit=unit)
            if form.is_valid():
                if form.cleaned_data['name'] != unit.name:
                    unit.name = form.cleaned_data['name']
                    unit.full_clean()
                    unit.save(update_fields=['name'])
                    audit(request, 'Ambiente interno renomeado', unit.name, unit.environment, unit=unit)
                messages.success(request, 'Ambiente atualizado.')
                return redirect('units')
    else:
        form = UnitEditForm(unit=unit)
    return form_page(request, form, 'Renomear ambiente', 'units', note=unit.environment.name)


def unit_delete_page(request, unit, *, error='', status=200):
    return render(request, 'unit-delete.html', {
        'unit': unit, 'user_count': unit.users.count(),
        'channel_count': unit.channels.count(), 'error': error,
    }, status=status)


@require_http_methods(['GET', 'POST'])
@access(owner=True, scope=False)
def unit_delete(request, pk):
    unit = get_object_or_404(Unit.objects.select_related('environment'),
                             pk=pk, environment__active=True)
    if request.method == 'GET':
        return unit_delete_page(request, unit)
    if request.POST.getlist('confirmation') != [str(unit.pk)]:
        return unit_delete_page(request, unit,
            error='Confirme a exclusão deste ambiente para continuar.', status=400)
    try:
        with transaction.atomic():
            # Serialize with unit edits, account moves and new FK references.
            env = get_object_or_404(Environment.objects.select_for_update(),
                                    pk=unit.environment_id, active=True)
            unit = get_object_or_404(Unit.objects.select_for_update(), pk=pk, environment=env)
            if not User.objects.filter(pk=request.user.pk, role=User.Role.OWNER,
                                       is_active=True, must_change_password=False).exists():
                raise PermissionDenied('Somente o proprietário pode excluir ambientes.')
            if unit.users.exists() or unit.channels.exists():
                return unit_delete_page(request, unit,
                    error='Este ambiente ainda possui usuários ou canais vinculados.', status=409)
            # Preserve past events without exposing them in the initial area's log.
            AuditEvent.objects.filter(unit=unit).update(unit=None, owner_only=True)
            label = f'{unit.name} ({unit.pk})'
            unit.delete()
            AuditEvent.objects.create(actor=request.user, environment=env, unit=None,
                owner_only=True, action='Ambiente excluído', target=label)
    except (ProtectedError, IntegrityError):
        # A concurrent reference must never cascade into account or channel loss.
        unit = get_object_or_404(Unit.objects.select_related('environment'), pk=pk)
        return unit_delete_page(request, unit,
            error='O ambiente recebeu um novo vínculo. Atualize a página e confira os usuários e canais antes de tentar novamente.',
            status=409)
    messages.success(request, 'Ambiente excluído. O histórico de administração foi preservado.')
    return redirect('units')


@require_POST
@access(owner=True, scope=False)
def unit_accounts(request, pk):
    action = request.POST.getlist('action')
    if action not in [['create'], ['manage']]:
        return HttpResponse(status=400)
    unit = get_object_or_404(Unit.objects.select_related('environment'), pk=pk, environment__active=True)
    request.session['environment_id'] = str(unit.environment_id)
    if action == ['create']:
        return redirect(reverse('account_create') + '?unit=' + str(unit.pk))
    return redirect(reverse('dashboard') + '#usuarios')


@sensitive_post_parameters('password1', 'password2')
@sensitive_variables()
@require_http_methods(['GET', 'POST'])
@access(manager=True)
def account_create(request):
    initial = {}
    if request.user.is_owner and 'unit' in request.GET:
        values = request.GET.getlist('unit')
        if len(values) != 1:
            raise Http404('Ambiente não encontrado.')
        try:
            selected = UUID(values[0])
        except (ValueError, TypeError, AttributeError):
            raise Http404('Ambiente não encontrado.') from None
        initial['unit'] = get_object_or_404(Unit, pk=selected, environment=request.environment)
    form = AccountCreateForm(request.POST if request.method == 'POST' else None, actor=request.user, environment=request.environment, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save(commit=False)
                user.environment = request.environment
                user.unit = form.cleaned_data.get('unit') if request.user.is_owner else request.user.unit
                user.must_change_password = True
                user.is_staff = user.is_superuser = False
                user.save()
                user.remember_provisional_password(form.cleaned_data['password1'])
                user.save(update_fields=['provisional_password_ciphertext'])
                audit(request, 'Acesso criado', user.username, unit=user.unit)
        except IntegrityError:
            form.add_error('username', 'Este nome de usuário já está em uso.')
        else:
            messages.success(request, 'Acesso criado. A troca da senha será exigida no primeiro login.')
            return redirect('dashboard')
    return form_page(request, form, 'Novo acesso')

def manageable_account(request, pk, lock=False):
    queryset = unit_records(request, User.objects.filter(environment=request.environment))
    if lock:
        queryset = queryset.select_for_update()
    target = get_object_or_404(queryset, pk=pk)
    if target == request.user or target.is_owner or not request.user.is_owner and target.role != User.Role.VIEWER:
        return None
    return target


def has_other_active_admin(target):
    return User.objects.filter(environment_id=target.environment_id, unit_id=target.unit_id,
                               role=User.Role.ADMIN, is_active=True).exclude(pk=target.pk).exists()


def revoke_account_sessions(user_id, *, include_expired=False):
    keys = []
    sessions = Session.objects.all() if include_expired else Session.objects.filter(expire_date__gt=timezone.now())
    for session in sessions.iterator(chunk_size=200):
        if str(session.get_decoded().get('_auth_user_id', '')) == str(user_id):
            keys.append(session.session_key)
    return Session.objects.filter(session_key__in=keys).delete()[0] if keys else 0


def locked_account_action(request, pk):
    """Recheck scope after serializing with account moves and state changes."""
    request.environment = get_object_or_404(Environment.objects.select_for_update(),
        pk=request.environment.pk, active=True)
    try:
        request.user.refresh_from_db()
    except User.DoesNotExist:
        raise PermissionDenied('Este acesso está indisponível.') from None
    if (not request.user.is_active or not request.user.can_manage or request.user.must_change_password
            or (not request.user.is_owner and request.user.environment_id != request.environment.pk)):
        raise PermissionDenied('Este acesso não pode administrar esta conta.')
    return manageable_account(request, pk, lock=True)


@sensitive_variables()
@require_GET
@access(manager=True)
def account_credentials(request, pk):
    with transaction.atomic():
        target = locked_account_action(request, pk)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser consultada por este acesso.')
        password = target.reveal_provisional_password()
        if request.is_shared_portal:
            host = request.get_host()
        else:
            host = next((host for host, slug in sorted(settings.SMA_TENANT_HOSTS.items())
                         if slug == target.environment.slug),
                        settings.SMA_OWNER_HOST if settings.SMA_SINGLE_HOST else None)
        response = render(request, 'account-credentials.html', {
            'account': target, 'login_url': f'https://{host}/login/' if host else None,
            'provisional_password': password,
            'credential_read_error': bool(target.provisional_password_ciphertext) and
                                     target.must_change_password and password is None,
        })
        audit(request, 'Credenciais de acesso consultadas', target.username, unit=target.unit)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    # HTTPS form posts need their same-origin referrer for CSRF validation.
    # Keep credential-page URLs private when navigating to other origins.
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET', 'POST'])
@access(manager=True)
def account_delete(request, pk):
    if request.method == 'GET':
        target = manageable_account(request, pk)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser excluída por este acesso.')
        return render(request, 'account-delete.html', {'account': target})
    with transaction.atomic():
        target = locked_account_action(request, pk)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser excluída por este acesso.')
        if request.POST.getlist('confirmation') != [str(target.pk)]:
            return render(request, 'account-delete.html', {'account': target,
                'error': 'Confirme a exclusão deste acesso para continuar.'}, status=400)
        if target.role == User.Role.ADMIN and target.is_active and not has_other_active_admin(target):
            return render(request, 'account-delete.html', {'account': target,
                'error': 'Este é o último administrador ativo deste ambiente. '
                         'Crie outro administrador ativo antes de excluir este acesso.'}, status=400)
        audit(request, 'Acesso excluído', f'{target.username} ({target.pk})', unit=target.unit)
        revoke_account_sessions(target.pk, include_expired=True)
        target.delete()
    messages.success(request, 'Acesso excluído. As sessões deste usuário foram encerradas.')
    return redirect('dashboard')


@require_http_methods(['GET', 'POST'])
@access(owner=True)
def account_unit(request, pk):
    if request.method == 'POST':
        with transaction.atomic():
            # Serialize unit moves with last-administrator state changes.
            Environment.objects.select_for_update().get(pk=request.environment.pk)
            target = manageable_account(request, pk, lock=True)
            if target is None:
                return HttpResponseForbidden('Esta conta não pode ser alterada por este acesso.')
            form = AccountUnitForm(request.POST, environment=request.environment, target=target)
            if form.is_valid():
                original = str(target.unit_id) if target.unit_id else 'none'
                chosen = form.cleaned_data['unit']
                chosen_id = chosen.pk if chosen else None
                if form.cleaned_data['original_unit'] != original:
                    form.add_error(None, 'O vínculo deste usuário foi alterado. Atualize a página e tente novamente.')
                elif chosen_id == target.unit_id:
                    messages.success(request, 'O usuário já está vinculado a este ambiente.')
                    return redirect('dashboard')
                elif target.role == User.Role.ADMIN and target.is_active and not has_other_active_admin(target):
                    current_label = target.unit.name if target.unit else 'Área inicial (sem ambiente vinculado)'
                    form.add_error(None, f'Esta conta é o último administrador ativo de {current_label}. '
                        'Crie outro administrador ativo com esse vínculo antes de mover este acesso.')
                else:
                    previous_unit = target.unit
                    target.unit = chosen
                    target.full_clean()
                    target.save(update_fields=['unit'])
                    revoke_account_sessions(target.pk)
                    audit(request, 'Acesso removido da unidade', target.username, unit=previous_unit)
                    audit(request, 'Acesso vinculado à unidade', target.username, unit=chosen)
                    messages.success(request, 'Ambiente atualizado. O usuário deverá entrar novamente.')
                    return redirect('dashboard')
    else:
        target = manageable_account(request, pk)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser alterada por este acesso.')
        form = AccountUnitForm(environment=request.environment, target=target)
    return form_page(request, form, f'Ambiente de {target.username}', note=(
        'Ao mudar o vínculo, as sessões deste usuário serão encerradas. '
        'O vínculo define o alcance da administração e das atividades. '
        'As permissões de visualização definidas nos canais serão mantidas.'))


@require_POST
@access(manager=True)
def account_state(request, pk):
    if request.POST.get('active') not in ['0', '1']:
        return HttpResponse(status=400)
    with transaction.atomic():
        # The environment lock also serializes changes to its last administrator.
        Environment.objects.select_for_update().get(pk=request.environment.pk)
        target = manageable_account(request, pk, lock=True)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser alterada por este acesso.')
        active = request.POST['active'] == '1'
        if not active and target.is_active and target.role == User.Role.ADMIN:
            if not has_other_active_admin(target):
                return render(request, 'error.html', {'title': 'Administrador necessário',
                    'detail': 'Crie outro administrador ativo antes de desativar este acesso.'}, status=400)
        target.is_active = active
        target.save(update_fields=['is_active'])
        audit(request, 'Acesso ativado' if active else 'Acesso desativado', target.username, unit=target.unit)
    return redirect('dashboard')

@sensitive_post_parameters('new_password1', 'new_password2')
@sensitive_variables()
@require_http_methods(['GET', 'POST'])
@access(manager=True)
def account_password(request, pk):
    if request.method == 'POST':
        with transaction.atomic():
            target = locked_account_action(request, pk)
            if target is None:
                return HttpResponseForbidden('Esta conta não pode ser alterada por este acesso.')
            form = SetPasswordForm(target, request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                user.must_change_password = True
                user.remember_provisional_password(form.cleaned_data['new_password1'])
                user.save(update_fields=['password', 'must_change_password', 'provisional_password_ciphertext'])
                revoke_account_sessions(user.pk)
                audit(request, 'Senha de acesso redefinida', user.username, unit=user.unit)
                messages.success(request, 'Senha redefinida. As sessões anteriores foram invalidadas.')
                return redirect('dashboard')
    else:
        target = manageable_account(request, pk)
        if target is None:
            return HttpResponseForbidden('Esta conta não pode ser alterada por este acesso.')
        form = SetPasswordForm(target)
    return form_page(request, form, f'Redefinir senha de {target.username}')

@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
@sensitive_variables()
@require_http_methods(['GET', 'POST'])
@login_required
def password_change(request):
    if request.method == 'POST':
        with transaction.atomic():
            if request.environment is not None:
                Environment.objects.select_for_update().get(pk=request.environment.pk)
            user = User.objects.select_for_update().get(pk=request.user.pk)
            form = PasswordChangeForm(user, request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                user.must_change_password = False
                user.save(update_fields=['password', 'must_change_password', 'provisional_password_ciphertext'])
                audit(request, 'Senha pessoal alterada', user.username, unit=user.unit)
                update_session_auth_hash(request, user)
                messages.success(request, 'Sua senha foi atualizada.')
                return redirect('environments' if user.is_owner else 'dashboard' if user.can_manage else 'monitor')
    else:
        form = PasswordChangeForm(request.user)
    note = 'Defina uma senha pessoal para continuar.' if request.user.must_change_password else ''
    return form_page(request, form, 'Alterar minha senha', 'monitor', note)

def channel_credentials(request, channel, key):
    server_url = settings.RTMP_PUBLIC_URL
    if not request.is_shared_portal:
        server_url = settings.SMA_RTMP_URLS.get(channel.environment.slug, server_url)
    return render(request, 'credentials.html', {'channel': channel, 'stream_key': key, 'server_url': server_url,
        'key_read_error': bool(channel.key_ciphertext) and key is None})

@require_GET
@access(manager=True)
def channel_show_credentials(request, pk):
    channel = get_object_or_404(managed_channels(request), pk=pk)
    key = channel.reveal_key()
    audit(request, 'Credenciais do canal consultadas', f'{channel.name} ({channel.pk})', unit=channel.unit)
    return channel_credentials(request, channel, key)


@require_http_methods(['GET', 'POST'])
@access(owner=True)
def channel_viewers(request, pk):
    if request.method == 'POST':
        with transaction.atomic():
            # Serialize with account deletion/reassignment and other audience edits.
            request.environment = get_object_or_404(
                Environment.objects.select_for_update(), pk=request.environment.pk, active=True)
            channel = get_object_or_404(
                Channel.objects.select_for_update(), pk=pk, environment=request.environment)
            actor = get_object_or_404(User.objects.select_for_update(), pk=request.user.pk)
            if not actor.is_active or not actor.is_owner or actor.must_change_password or not valid_unit_scope(actor):
                return HttpResponseForbidden('Somente o proprietário do Central pode alterar os visualizadores.')
            request.user = actor
            form = ChannelViewersForm(request.POST, channel=channel)
            if form.is_valid():
                selected = list(form.cleaned_data['allowed_viewers'].select_for_update(of=('self',)))
                channel.allowed_viewers.set(selected)
                channel.audience_restricted = bool(selected)
                channel.save(update_fields=['audience_restricted'])
                audience = f'{len(selected)} conta(s)' if selected else 'Todas as contas do servidor'
                audit(request, 'Visualizadores do canal alterados',
                      f'{channel.name} ({channel.pk}) · {audience}', unit=channel.unit)
                messages.success(request, 'Visualizadores atualizados.')
                return redirect('dashboard')
    else:
        channel = get_object_or_404(managed_channels(request), pk=pk)
        form = ChannelViewersForm(channel=channel)
    return render(request, 'channel-viewers.html', {'channel': channel, 'form': form},
                  status=400 if form.is_bound and form.errors else 200)

@require_http_methods(['GET', 'POST'])
@access(manager=True)
def channel_delete(request, pk):
    if request.method == 'GET':
        channel = get_object_or_404(managed_channels(request), pk=pk)
        return render(request, 'channel-delete.html', {'channel': channel})
    with transaction.atomic():
        channel = get_object_or_404(managed_channels(request).select_for_update(), pk=pk)
        if request.POST.getlist('confirmation') != [str(channel.pk)]:
            return render(request, 'channel-delete.html', {'channel': channel,
                'error': 'Confirme a exclusão deste canal para continuar.'}, status=400)
        audit(request, 'Canal excluído', f'{channel.name} ({channel.pk})', unit=channel.unit)
        channel.delete()
    messages.success(request, 'Canal excluído. A chave de transmissão foi invalidada.')
    return redirect('dashboard')

@require_http_methods(['GET', 'POST'])
@access(manager=True)
def channel_create(request):
    form = ChannelForm(request.POST if request.method == 'POST' else None,
                       environment=request.environment, unit=request.user.unit, actor=request.user)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                channel = form.save(commit=False)
                channel.environment = request.environment
                channel.unit = form.cleaned_data.get('unit') if request.user.is_owner else request.user.unit
                key = channel.issue_key()
                channel.save()
                audit(request, 'Canal criado', channel.name, unit=channel.unit)
        except IntegrityError:
            form.add_error('name', 'Já existe um canal com esse nome neste ambiente.')
        else:
            return channel_credentials(request, channel, key)
    return form_page(request, form, 'Novo canal')

@require_http_methods(['GET', 'POST'])
@access(manager=True)
def channel_edit(request, pk):
    channel = get_object_or_404(managed_channels(request), pk=pk)
    form = ChannelForm(request.POST if request.method == 'POST' else None, instance=channel,
                       environment=request.environment, unit=channel.unit, actor=request.user)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                # Updating only the name preserves a concurrently rotated key or status.
                managed_channels(request).filter(pk=channel.pk).update(name=form.cleaned_data['name'])
                audit(request, 'Canal renomeado', form.cleaned_data['name'], unit=channel.unit)
        except IntegrityError:
            form.add_error('name', 'Já existe um canal com esse nome neste ambiente.')
        else:
            return redirect('dashboard')
    return form_page(request, form, 'Editar canal')

@require_POST
@access(manager=True)
def channel_key(request, pk):
    with transaction.atomic():
        channel = get_object_or_404(managed_channels(request).select_for_update(), pk=pk)
        key = channel.issue_key()
        channel.save(update_fields=['key_digest', 'key_ciphertext', 'key_format', 'publisher_client_id', 'publisher_generation', 'last_seen'])
        channel.inputs.update(publisher_client_id='', publisher_generation='', last_seen=None)
        audit(request, 'Chave de transmissão substituída', channel.name, unit=channel.unit)
    return channel_credentials(request, channel, key)

@require_POST
@access(manager=True)
def channel_state(request, pk):
    if request.POST.get('active') not in ['0', '1']:
        return HttpResponse(status=400)
    with transaction.atomic():
        channel = get_object_or_404(managed_channels(request).select_for_update(), pk=pk)
        channel.active = request.POST['active'] == '1'
        if not channel.active:
            channel.publisher_client_id = ''
            channel.publisher_generation = ''
            channel.last_seen = None
            channel.inputs.update(publisher_client_id='', publisher_generation='', last_seen=None)
        channel.save(update_fields=['active', 'publisher_client_id', 'publisher_generation', 'last_seen'])
        audit(request, 'Canal ativado' if channel.active else 'Canal desativado', channel.name, unit=channel.unit)
    return redirect('dashboard')

def is_online(source):
    channel = source_channel(source)
    if not channel.active or not current_source_key(source) or source.last_seen is None:
        return False
    if not 0 <= (timezone.now() - source.last_seen).total_seconds() < settings.HLS_FRESH_SECONDS:
        return False
    try:
        return time.time() - (settings.HLS_DIR / f'{source.stream_id}.m3u8').stat().st_mtime < settings.HLS_FRESH_SECONDS
    except OSError:
        return False

@require_GET
@access(api=True, scope=False)
def streams(request):
    environment = monitoring_environment(request)
    channels = readable_channels(request)
    if environment is not None:
        channels = channels.filter(environment=environment)
    data = []
    for channel in channels.order_by('environment__name', 'name', 'id').prefetch_related('inputs'):
        sources = [source for source in [channel, *channel.inputs.all()] if is_online(source)]
        for source in sources:
            number = source.slot if isinstance(source, ChannelInput) else 1
            label = channel.name
            item = {'id': source.stream_id, 'channel_id': str(channel.pk), 'input_number': number,
                    'label': label, 'url': f'/hls/{source.stream_id}.m3u8'}
            if settings.SMA_REALTIME_ENABLED:
                item['webrtc_url'] = f'/rtc/{source.stream_id}/whep'
            if request.user.is_owner:
                item['environment'] = {'id': str(channel.environment_id), 'name': channel.environment.name}
            data.append(item)
    return JsonResponse(data, safe=False)

@require_GET
@access(manager=True, api=True)
def channels_api(request):
    return JsonResponse([{'id': str(c.pk), 'stream_id': c.stream_id, 'name': c.name, 'active': c.active}
        for c in managed_channels(request)], safe=False)

@require_GET
def hls_authorize(request):
    if not internal_allowed(request):
        return HttpResponse(status=403)
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    if request.user.must_change_password:
        return HttpResponse(status=403)
    path = urlsplit(request.headers.get('X-Original-URI', '')).path
    match = re.fullmatch(r'/hls/([0-9a-f]{32})(?:\.m3u8|-\d+\.ts)', path)
    allowed = bool(match) and readable_media_source(request, match[1]) is not None
    return HttpResponse(status=204 if allowed else 403)

@require_GET
def realtime_authorize(request, stream_id):
    """The media gateway reuses the session, domain and channel permissions."""
    if not settings.SMA_REALTIME_ENABLED or not internal_allowed(request):
        return HttpResponse(status=403)
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    if request.user.must_change_password or not re.fullmatch(r'[0-9a-f]{32}', stream_id):
        return HttpResponse(status=403)
    source = readable_media_source(request, stream_id)
    if source is None:
        return HttpResponse(status=403)
    return JsonResponse({'allowed': True, 'online': is_online(source)})
