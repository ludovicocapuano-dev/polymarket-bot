"""
Redis Event Bus v9.0 — Pub/Sub + Cache per comunicazione tra layer.

Canali:
- news:breaking — news urgenti da GDELT/Finlight
- price:update — variazioni prezzo significative
- trade:signal — segnali validati pronti per esecuzione
- trade:executed — trade eseguiti (per attribution)
- resolution:detected — mercati risolti
- drift:alert — allarmi concept drift
"""

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """Redis Pub/Sub + Cache con graceful degradation."""

    CHANNELS = [
        "news:breaking",
        "price:update",
        "trade:signal",
        "trade:executed",
        "resolution:detected",
        "drift:alert",
    ]

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._redis: Any = None
        self._available = False
        self._pubsub: Any = None
        # Fallback in-memory se Redis non disponibile
        self._memory_cache: dict[str, tuple[str, float]] = {}  # key -> (value, expire_time)
        self._memory_subscribers: dict[str, list] = {}  # channel -> [callbacks]

    def connect(self) -> bool:
        """Connette a Redis. Ritorna False se non disponibile."""
        try:
            import redis
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            self._redis.ping()
            self._available = True
            logger.info(f"[REDIS] Connesso a {self.redis_url}")
            return True
        except ImportError:
            logger.warning("[REDIS] redis-py non installato — event bus in-memory")
            return False
        except Exception as e:
            logger.warning(f"[REDIS] Connessione fallita: {e} — event bus in-memory")
            return False

    @property
    def available(self) -> bool:
        return self._available

    def publish(self, channel: str, data: dict):
        """Pubblica un evento su un canale."""
        payload = json.dumps(data, default=str)
        if self._available:
            try:
                self._redis.publish(channel, payload)
                return
            except Exception as e:
                logger.warning(f"[REDIS] Errore publish {channel}: {e}")
        # Fallback in-memory
        for cb in self._memory_subscribers.get(channel, []):
            try:
                cb(data)
            except Exception:
                pass

    def subscribe(self, channels: list[str]):
        """Crea una sottoscrizione a canali (solo Redis reale)."""
        if not self._available:
            logger.debug("[REDIS] Subscribe in-memory (noop)")
            return None
        try:
            self._pubsub = self._redis.pubsub()
            self._pubsub.subscribe(*channels)
            return self._pubsub
        except Exception as e:
            logger.warning(f"[REDIS] Errore subscribe: {e}")
            return None

    def add_memory_subscriber(self, channel: str, callback):
        """Aggiunge un callback in-memory per un canale (fallback)."""
        self._memory_subscribers.setdefault(channel, []).append(callback)

    def cache_set(self, key: str, value: str, ttl: int = 300):
        """Salva un valore in cache con TTL."""
        if self._available:
            try:
                self._redis.setex(key, ttl, value)
                return
            except Exception as e:
                logger.debug(f"[REDIS] Cache set fallita: {e}")
        # Fallback in-memory
        self._memory_cache[key] = (value, time.time() + ttl)

    def cache_get(self, key: str) -> str | None:
        """Legge un valore dalla cache."""
        if self._available:
            try:
                return self._redis.get(key)
            except Exception as e:
                logger.debug(f"[REDIS] Cache get fallita: {e}")
        # Fallback in-memory
        entry = self._memory_cache.get(key)
        if entry:
            value, expire = entry
            if time.time() < expire:
                return value
            else:
                del self._memory_cache[key]
        return None

    def cache_delete(self, key: str):
        """Rimuove un valore dalla cache."""
        if self._available:
            try:
                self._redis.delete(key)
                return
            except Exception as e:
                logger.debug(f"[REDIS] Cache delete fallita: {e}")
        self._memory_cache.pop(key, None)

    def close(self):
        """Chiude la connessione Redis."""
        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
        self._available = False
