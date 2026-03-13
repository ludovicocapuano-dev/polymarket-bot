"""
PMXT Client — wrapper unificato attorno a pmxt (il CCXT per prediction markets).

Offre la stessa interfaccia di polymarket_api.py ma usa pmxt internamente.
Fallback automatico a polymarket_api.py se PMXT fallisce.

PMXT supporta: Polymarket, Kalshi, KalshiDemo, Baozi, Limitless, Myriad, Probable.
Qui usiamo Polymarket per trading e Kalshi per scanning cross-platform.

PMXT usa un server locale (localhost:3847) per proxare le richieste alle exchange.
Il server viene avviato automaticamente da pmxt alla prima connessione.

Env vars necessarie per Kalshi (opzionali, solo per cross-platform scanner):
    KALSHI_API_KEY — API key Kalshi
    KALSHI_PRIVATE_KEY — private key RSA per Kalshi (path al file .pem)
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import — non bloccare il bot se pmxt non è installato
_pmxt = None
_pmxt_available = None


def _ensure_pmxt():
    """Importa pmxt al primo utilizzo."""
    global _pmxt, _pmxt_available
    if _pmxt_available is not None:
        return _pmxt_available
    try:
        import pmxt
        _pmxt = pmxt
        _pmxt_available = True
        logger.info(f"[PMXT] pmxt v{pmxt.__version__} caricato")
        return True
    except ImportError:
        _pmxt_available = False
        logger.warning("[PMXT] pmxt non installato — pip install pmxt")
        return False


class PMXTClient:
    """
    Client PMXT per Polymarket con fallback a polymarket_api.PolymarketAPI.

    Uso:
        from utils.pmxt_client import PMXTClient
        client = PMXTClient(fallback_api=existing_polymarket_api)
        client.connect()
        markets = client.get_markets()
        book = client.get_orderbook(token_id)
    """

    def __init__(self, fallback_api=None):
        """
        Args:
            fallback_api: istanza di PolymarketAPI per fallback se PMXT fallisce.
                          Se None, nessun fallback (errore propagato).
        """
        self._fallback = fallback_api
        self._pm = None  # pmxt.Polymarket instance
        self._connected = False
        self._error_count = 0
        self._max_errors_before_fallback = 3
        self._last_error_reset = time.time()

    def connect(self) -> bool:
        """Connetti a Polymarket via PMXT. Ritorna True se connesso."""
        if not _ensure_pmxt():
            logger.warning("[PMXT] pmxt non disponibile, uso fallback")
            return False

        try:
            # Leggi credenziali da env (stesse usate da polymarket_api.py)
            private_key = os.getenv("PRIVATE_KEY", "").strip()
            api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
            api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
            api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()
            funder = os.getenv("FUNDER_ADDRESS", "").strip()

            # Determina signature_type
            sig_type = "gnosis-safe" if funder else "eoa"

            self._pm = _pmxt.Polymarket(
                api_key=api_key or None,
                api_secret=api_secret or None,
                passphrase=api_passphrase or None,
                private_key=private_key or None,
                proxy_address=funder or None,
                signature_type=sig_type,
                auto_start_server=True,
            )

            self._connected = True
            self._error_count = 0
            logger.info("[PMXT] Connesso a Polymarket via pmxt")
            return True

        except Exception as e:
            logger.error(f"[PMXT] Errore connessione: {e}")
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._pm is not None

    def _should_fallback(self) -> bool:
        """Ritorna True se troppi errori PMXT consecutivi → usa fallback."""
        if self._error_count >= self._max_errors_before_fallback:
            # Reset ogni 5 minuti
            if time.time() - self._last_error_reset > 300:
                self._error_count = 0
                self._last_error_reset = time.time()
                return False
            return True
        return False

    def _record_error(self, method: str, error: Exception):
        """Registra errore PMXT per decidere fallback."""
        self._error_count += 1
        logger.warning(
            f"[PMXT] Errore in {method}: {error} "
            f"(errori consecutivi: {self._error_count})"
        )

    # ── Markets ──────────────────────────────────────────────────

    def get_markets(
        self,
        limit: int = 100,
        active: bool = True,
        params: Optional[dict] = None,
    ) -> list[dict]:
        """
        Fetch mercati attivi. Ritorna lista di dict normalizzati compatibili
        con il formato usato dal bot.

        Ogni dict ha: id, question, tokens, prices, volume, liquidity,
        end_date, active, tags, outcomes, category, slug, condition_id.
        """
        # Fallback check
        if self._should_fallback() and self._fallback:
            logger.debug("[PMXT] Troppi errori, fallback a polymarket_api")
            markets = self._fallback.fetch_markets(active=active, limit=limit)
            return [self._market_obj_to_dict(m) for m in markets]

        if not self.is_connected:
            if self._fallback:
                markets = self._fallback.fetch_markets(active=active, limit=limit)
                return [self._market_obj_to_dict(m) for m in markets]
            return []

        try:
            _params = params or {}
            if limit:
                _params["limit"] = limit

            pmxt_markets = self._pm.fetch_markets(params=_params)
            self._error_count = 0  # reset on success

            result = []
            for m in pmxt_markets:
                normalized = self._normalize_market(m)
                if normalized:
                    result.append(normalized)

            logger.info(f"[PMXT] Fetched {len(result)} mercati via pmxt")
            return result

        except Exception as e:
            self._record_error("get_markets", e)
            if self._fallback:
                logger.info("[PMXT] Fallback a polymarket_api.fetch_markets()")
                markets = self._fallback.fetch_markets(active=active, limit=limit)
                return [self._market_obj_to_dict(m) for m in markets]
            return []

    def _normalize_market(self, m) -> Optional[dict]:
        """Converte UnifiedMarket di pmxt nel formato dict usato dal bot."""
        try:
            tokens = {}
            prices = {}
            outcomes = []

            # Estrai YES/NO outcomes
            if m.yes:
                tokens["yes"] = m.yes.outcome_id
                prices["yes"] = m.yes.price
                outcomes.append("Yes")
            if m.no:
                tokens["no"] = m.no.outcome_id
                prices["no"] = m.no.price
                outcomes.append("No")

            # Fallback: prova outcomes list
            if not tokens and m.outcomes:
                for i, outcome in enumerate(m.outcomes):
                    label = outcome.label.lower()
                    tokens[label] = outcome.outcome_id
                    prices[label] = outcome.price
                    outcomes.append(outcome.label)

            if len(tokens) < 2:
                return None

            return {
                "id": m.market_id,
                "condition_id": m.market_id,
                "question": m.title,
                "slug": m.url.split("/")[-1] if m.url else "",
                "tokens": tokens,
                "prices": prices,
                "volume": m.volume or m.volume_24h or 0.0,
                "liquidity": m.liquidity or 0.0,
                "end_date": m.resolution_date.isoformat() if m.resolution_date else "",
                "active": True,
                "tags": m.tags or [],
                "outcomes": outcomes,
                "category": m.category or "",
            }
        except Exception as e:
            logger.debug(f"[PMXT] Errore normalizzazione mercato: {e}")
            return None

    @staticmethod
    def _market_obj_to_dict(m) -> dict:
        """Converte un Market dataclass di polymarket_api in dict."""
        return {
            "id": m.id,
            "condition_id": m.condition_id,
            "question": m.question,
            "slug": m.slug,
            "tokens": m.tokens,
            "prices": m.prices,
            "volume": m.volume,
            "liquidity": m.liquidity,
            "end_date": m.end_date,
            "active": m.active,
            "tags": m.tags,
            "outcomes": m.outcomes,
            "category": m.category,
        }

    # ── Order Book ───────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        """
        Ritorna order book normalizzato: {"bids": [...], "asks": [...]}.
        Ogni entry ha {"price": str, "size": str} (stesso formato di polymarket_api).
        """
        if self._should_fallback() and self._fallback:
            return self._fallback.get_order_book(token_id)

        if not self.is_connected:
            if self._fallback:
                return self._fallback.get_order_book(token_id)
            return {"bids": [], "asks": []}

        try:
            book = self._pm.fetch_order_book(token_id)
            self._error_count = 0

            bids = [
                {"price": str(level.price), "size": str(level.size)}
                for level in (book.bids or [])
            ]
            asks = [
                {"price": str(level.price), "size": str(level.size)}
                for level in (book.asks or [])
            ]

            logger.debug(
                f"[PMXT] Orderbook {token_id[:16]}... "
                f"bids={len(bids)} asks={len(asks)}"
            )
            return {"bids": bids, "asks": asks}

        except Exception as e:
            self._record_error("get_orderbook", e)
            if self._fallback:
                return self._fallback.get_order_book(token_id)
            return {"bids": [], "asks": []}

    # ── Price ────────────────────────────────────────────────────

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        """
        Ritorna il miglior prezzo per side BUY o SELL.
        BUY = best ask (prezzo che pagherai), SELL = best bid (prezzo che riceverai).
        """
        book = self.get_orderbook(token_id)

        if side.upper() == "BUY":
            asks = book.get("asks", [])
            if asks:
                return float(asks[0]["price"])
        else:
            bids = book.get("bids", [])
            if bids:
                return float(bids[0]["price"])

        return 0.0

    # ── Order Placement ──────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> Optional[dict]:
        """
        Piazza un limit order via PMXT.

        Args:
            token_id: outcome_id del token
            side: "BUY" o "SELL"
            price: prezzo limite
            size: numero di shares

        Ritorna dict con order info o None se fallisce.
        Fallback a polymarket_api.buy_limit/sell_limit se PMXT fallisce.
        """
        if not self.is_connected:
            if self._fallback:
                if side.upper() == "BUY":
                    return self._fallback.buy_limit(token_id, price, size)
                else:
                    return self._fallback.sell_limit(token_id, price, size)
            return None

        try:
            order = self._pm.create_order(
                outcome_id=token_id,
                side=side.lower(),
                type="limit",
                amount=size,
                price=price,
            )
            self._error_count = 0

            result = {
                "orderID": order.id,
                "id": order.id,
                "market_id": order.market_id,
                "side": order.side,
                "price": order.price,
                "size": order.amount,
                "status": order.status,
                "filled": order.filled,
                "remaining": order.remaining,
            }

            logger.info(
                f"[PMXT] Limit {side.upper()} {size:.1f}@${price:.2f} "
                f"su {token_id[:16]}... → {order.status}"
            )
            return result

        except Exception as e:
            self._record_error("place_order", e)
            if self._fallback:
                logger.info("[PMXT] Fallback a polymarket_api per ordine")
                if side.upper() == "BUY":
                    return self._fallback.buy_limit(token_id, price, size)
                else:
                    return self._fallback.sell_limit(token_id, price, size)
            return None

    # ── Cancel Order ─────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancella un ordine. Ritorna True se cancellato."""
        if not self.is_connected:
            if self._fallback:
                return self._fallback.cancel_order(order_id)
            return False

        try:
            self._pm.cancel_order(order_id)
            self._error_count = 0
            logger.info(f"[PMXT] Ordine {order_id[:16]}... cancellato")
            return True

        except Exception as e:
            self._record_error("cancel_order", e)
            if self._fallback:
                return self._fallback.cancel_order(order_id)
            return False

    # ── Shutdown ─────────────────────────────────────────────────

    def close(self):
        """Chiudi connessione PMXT e server."""
        if self._pm:
            try:
                self._pm.close()
                logger.info("[PMXT] Connessione chiusa")
            except Exception:
                pass
        self._connected = False
        self._pm = None


class PMXTKalshiClient:
    """
    Client PMXT per Kalshi — SOLO lettura (scanning mercati, no trading).
    Usato dal cross-platform scanner per trovare opportunita' di arbitraggio.

    Env vars:
        KALSHI_API_KEY — API key Kalshi
        KALSHI_PRIVATE_KEY — private key RSA (path al file .pem)
    """

    def __init__(self, demo: bool = False):
        """
        Args:
            demo: se True, usa KalshiDemo invece di Kalshi (mercati demo).
        """
        self._kalshi = None
        self._connected = False
        self._demo = demo

    def connect(self) -> bool:
        """Connetti a Kalshi via PMXT."""
        if not _ensure_pmxt():
            return False

        try:
            api_key = os.getenv("KALSHI_API_KEY", "").strip()
            private_key = os.getenv("KALSHI_PRIVATE_KEY", "").strip()

            exchange_cls = _pmxt.KalshiDemo if self._demo else _pmxt.Kalshi

            self._kalshi = exchange_cls(
                api_key=api_key or None,
                private_key=private_key or None,
                auto_start_server=True,
            )

            self._connected = True
            logger.info(
                f"[PMXT] Connesso a {'KalshiDemo' if self._demo else 'Kalshi'} via pmxt"
                + (" (senza auth — solo lettura)" if not api_key else "")
            )
            return True

        except Exception as e:
            logger.error(f"[PMXT] Errore connessione Kalshi: {e}")
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._kalshi is not None

    def get_markets(self, limit: int = 100, params: Optional[dict] = None) -> list[dict]:
        """
        Fetch mercati Kalshi. Ritorna lista di dict normalizzati.
        Ogni dict ha: id, question, tokens, prices, volume, category, platform.
        """
        if not self.is_connected:
            return []

        try:
            _params = params or {}
            if limit:
                _params["limit"] = limit

            markets = self._kalshi.fetch_markets(params=_params)

            result = []
            for m in markets:
                normalized = self._normalize_market(m)
                if normalized:
                    result.append(normalized)

            logger.info(f"[PMXT] Fetched {len(result)} mercati Kalshi")
            return result

        except Exception as e:
            logger.warning(f"[PMXT] Errore fetch mercati Kalshi: {e}")
            return []

    def get_orderbook(self, token_id: str) -> dict:
        """Ritorna order book Kalshi normalizzato."""
        if not self.is_connected:
            return {"bids": [], "asks": []}

        try:
            book = self._kalshi.fetch_order_book(token_id)
            bids = [
                {"price": str(level.price), "size": str(level.size)}
                for level in (book.bids or [])
            ]
            asks = [
                {"price": str(level.price), "size": str(level.size)}
                for level in (book.asks or [])
            ]
            return {"bids": bids, "asks": asks}
        except Exception as e:
            logger.debug(f"[PMXT] Errore orderbook Kalshi: {e}")
            return {"bids": [], "asks": []}

    def _normalize_market(self, m) -> Optional[dict]:
        """Converte UnifiedMarket Kalshi in dict normalizzato."""
        try:
            prices = {}
            outcomes = []

            if m.yes:
                prices["yes"] = m.yes.price
                outcomes.append("Yes")
            if m.no:
                prices["no"] = m.no.price
                outcomes.append("No")

            if not prices and m.outcomes:
                for outcome in m.outcomes:
                    label = outcome.label.lower()
                    prices[label] = outcome.price
                    outcomes.append(outcome.label)

            return {
                "id": m.market_id,
                "question": m.title,
                "description": m.description or "",
                "prices": prices,
                "volume": m.volume or m.volume_24h or 0.0,
                "liquidity": m.liquidity or 0.0,
                "end_date": m.resolution_date.isoformat() if m.resolution_date else "",
                "category": m.category or "",
                "tags": m.tags or [],
                "outcomes": outcomes,
                "platform": "kalshi",
                "url": m.url or "",
            }
        except Exception as e:
            logger.debug(f"[PMXT] Errore normalizzazione mercato Kalshi: {e}")
            return None

    def close(self):
        """Chiudi connessione Kalshi."""
        if self._kalshi:
            try:
                self._kalshi.close()
            except Exception:
                pass
        self._connected = False
        self._kalshi = None
