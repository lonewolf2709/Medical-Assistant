"""In-memory rate limiter using slowapi."""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Key function: identify users by their IP (Telegram sends from fixed IPs)
# In production with Redis, swap storage_uri to "redis://..."
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
