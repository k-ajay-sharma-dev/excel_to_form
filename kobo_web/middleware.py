from django.conf import settings


class DynamicCsrfOriginMiddleware:
    """
    Dynamically trusts whatever HTTP_ORIGIN or HTTP_HOST the request arrives from.
    This ensures CSRF verification never fails on Railway, Vercel, Render, or any custom domain.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.META.get("HTTP_ORIGIN")
        if origin:
            if origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(origin)
        else:
            # Fallback to host header if origin is missing
            host = request.get_host()
            scheme = request.scheme or "https"
            host_origin = f"{scheme}://{host}"
            if host_origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(host_origin)

        return self.get_response(request)
