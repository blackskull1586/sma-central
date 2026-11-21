from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from .models import AuditEvent


def valid_unit_scope(user):
    if user.is_owner:
        return user.unit_id is None
    return user.unit_id is None or user.unit.environment_id == user.environment_id


def unit_records(request, queryset):
    """A null unit is the original area, never a wildcard for all units."""
    if request.user.is_owner:
        return queryset
    if not valid_unit_scope(request.user):
        return queryset.none()
    if queryset.model is AuditEvent:
        queryset = queryset.filter(owner_only=False)
    return queryset.filter(environment_id=request.user.environment_id, unit_id=request.user.unit_id)

def access(*, manager=False, owner=False, scope=True, api=False):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return HttpResponse(status=401) if api else redirect_to_login(request.get_full_path())
            if not valid_unit_scope(user):
                return HttpResponseForbidden('A unidade desta conta está indisponível.')
            if user.must_change_password:
                return HttpResponse(status=403) if api else redirect('password_change')
            if owner and not user.is_owner or manager and not user.can_manage:
                return HttpResponseForbidden('Você não possui permissão para esta ação.')
            if scope and (not request.environment or not request.environment.active):
                if user.is_owner and not api:
                    return redirect('environments')
                return HttpResponseForbidden('Nenhum ambiente ativo está disponível.')
            return view(request, *args, **kwargs)
        return wrapped
    return decorator

_ACTOR_UNIT = object()


def audit(request, action, target, environment=None, *, unit=_ACTOR_UNIT):
    if unit is _ACTOR_UNIT:
        unit = request.user.unit
    AuditEvent.objects.create(actor=request.user, environment=environment or request.environment,
                              unit=unit, action=action, target=str(target)[:150])
