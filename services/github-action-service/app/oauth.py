from app.config import settings


def get_oauth_protected_resource_metadata() -> dict:
    return {
        "resource": settings.SERVICE_URL,
        "authorization_servers": [settings.SERVICE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{settings.SERVICE_URL}/docs",
    }
