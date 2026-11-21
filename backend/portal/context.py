from django.conf import settings

def portal_context(request):
    page_name = (request.resolver_match.url_name or '') if request.resolver_match else ''
    return {'current_environment': getattr(request, 'environment', None),
            'domain_environment': getattr(request, 'domain_environment', None),
            'is_owner_portal': getattr(request, 'is_owner_portal', False),
            'is_shared_portal': getattr(request, 'is_shared_portal', False),
            'domain_management': settings.SMA_DOMAIN_MANAGEMENT,
            'environment_label': settings.SMA_ENVIRONMENT_LABEL,
            'central_management_page': page_name == 'dashboard' or page_name.startswith(('account_', 'channel_'))}
