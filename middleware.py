from django.shortcuts import redirect
from django.urls import reverse


class SetupRequiredMiddleware:
    """Redirect to the first-run setup page if SystemConfig is not configured.

    Only intercepts requests under ``/test_lab/`` (except the setup page
    itself and static/API endpoints) so other Django apps are unaffected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith('/test_lab/') and not path.startswith('/test_lab/setup/'):
            from .models import SystemConfig
            config = SystemConfig.load()
            if not config.is_configured:
                return redirect(reverse('setup_page'))
        return self.get_response(request)
