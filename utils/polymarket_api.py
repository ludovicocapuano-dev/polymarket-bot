"""
Wrapper unificato per le API Polymarket (CLOB + Gamma).
Gestisce autenticazione, fetching mercati, order book, e trading.
"""

import json
import logging
import time
from dataclasses import dataclass, field

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)

from config import PolymarketCreds

logger = logging.getLogger(__name__)


@dataclass
class Market:
    """Un mercato Polymarket generico."""
    id: str
    condition_id: str
    question: str
    slug: str
    tokens: dict  # {"yes": token_id, "no": token_id}
    prices: dict   # {"yes": float, "no": float}
    volume: float
    liquidity: float
    end_date: str
    active: bool
    tags: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    category: str = ""

    @property
    def spread(self) -> float:
        return abs(1.0 - self.prices.get("yes", 0.5) - self.prices.get("no", 0.5))

    @property
    def mispricing_score(self) -> float:
        """Quanto la somma dei prezzi devia da 1.0. Piu' alto = piu' misprezzato."""
        total = self.prices.get("yes", 0.5) + self.prices.get("no", 0.5)
        return abs(total - 1.0)


class PolymarketAPI:
    """Client unificato per tutte le interazioni con Polymarket."""

    def __init__(self, creds: PolymarketCreds):
        self.creds = creds
        self.clob: ClobClient | None = None
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._authenticated = False
        self._market_cache: dict[str, Market] = {}
        self._cache_time: float = 0

    def authenticate(self) -> bool:
        try:
            logger.info("Autenticazione Polymarket...")
            # signature_type:
            #   0 = EOA (wallet diretto, senza proxy)
            #   1 = POLY_PROXY (account email/Magic.link)
            #   2 = Browser Wallet proxy (MetaMask/WalletConnect via Polymarket.com)
            # Se FUNDER_ADDRESS e' impostato e l'account e' stato creato
            # collegando MetaMask a polymarket.com, usiamo tipo 2.
            # Strip spazi invisibili da chiavi (trailing whitespace nel .env)
            priv_key = self.creds.private_key.strip()
            funder = self.creds.funder_address.strip() if self.creds.funder_address else ""
            sig_type = 2 if funder else 0

            logger.info(
                f"[AUTH-DEBUG] key_len={len(priv_key)} key_head={priv_key[:6]} "
                f"key_tail={priv_key[-4:]} funder={funder} sig_type={sig_type}"
            )

            # v12.0.4: Builder Program — gasless tx + volume rewards
            builder_config = None
            if self.creds.builder_api_key:
                try:
                    from py_builder_signing_sdk.config import BuilderConfig
                    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
                    builder_config = BuilderConfig(
                        local_builder_creds=BuilderApiKeyCreds(
                            key=self.creds.builder_api_key.strip(),
                            secret=self.creds.builder_api_secret.strip(),
                            passphrase=self.creds.builder_api_passphrase.strip(),
                        )
                    )
                    logger.info("[BUILDER] Builder Program credentials loaded")
                except Exception as e:
                    logger.warning(f"[BUILDER] Could not load builder config: {e}")

            self.clob = ClobClient(
                host=self.creds.host,
                chain_id=self.creds.chain_id,
                key=priv_key,
                signature_type=sig_type,
                funder=funder or None,
                builder_config=builder_config,
            )

            logger.info(
                f"[AUTH-DEBUG] signer_addr={self.clob.signer.address()} "
                f"builder_funder={self.clob.builder.funder} "
                f"builder_sig_type={self.clob.builder.sig_type}"
            )

            derived = self.clob.create_or_derive_api_creds()
            logger.info(
                f"[AUTH-DEBUG] derived_api_key={derived.api_key[:8]}... "
                f"env_api_key={self.creds.api_key[:8]}..."
            )
            self.clob.set_api_creds(derived)
            self._authenticated = True
            logger.info("Autenticazione riuscita")
            return True
        except Exception as e:
            logger.error(f"Autenticazione fallita: {e}")
            return False

    # ── Balance check ────────────────────────────────────────────
    def get_usdc_balance(self) -> float:
        """Ritorna il saldo USDC disponibile (non bloccato in posizioni)."""
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.clob.get_balance_allowance(params)
            # balance e' SEMPRE in micro-USDC (6 decimali)
            # Es: '695283' = $0.695283, '2500000000' = $2500.00
            raw_balance = result.get("balance", "0")
            balance = float(raw_balance) / 1e6
            logger.info(f"[BALANCE] raw={raw_balance} → ${balance:.2f}")
            return balance
        except Exception as e:
            logger.warning(f"[BALANCE] Errore lettura saldo: {e}")
            return -1.0  # -1 = errore, non bloccare il bot

    def get_token_balance(self, token_id: str) -> float:
        """v12.1: Ritorna il numero di shares possedute per un token condizionale."""
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id
            )
            result = self.clob.get_balance_allowance(params)
            raw = result.get("balance", "0")
            # Conditional tokens hanno 6 decimali come USDC
            return float(raw) / 1e6
        except Exception:
            return -1.0  # errore, non bloccare

    # ── Fetching mercati ───────────────────────────────────────────

    def fetch_markets(
        self,
        active: bool = True,
        limit: int = 100,
        tag: str = "",
        order: str = "volume",
        offset: int = 0,
    ) -> list[Market]:
        """Fetch mercati dalla Gamma API con filtri."""
        try:
            params = {
                "active": str(active).lower(),
                "closed": "false",
                "limit": limit,
                "order": order,
                "ascending": "false",
            }
            if offset > 0:
                params["offset"] = offset
            if tag:
                params["tag_slug"] = tag

            resp = self._session.get(
                f"{self.creds.gamma_host}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()

            markets = []
            for m in raw:
                tokens_raw = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", ["Yes", "No"])
                prices_raw = m.get("outcomePrices", [])

                # L'API Gamma restituisce spesso stringhe JSON invece di liste
                # es: '"[0.75, 0.25]"' oppure '["0.75", "0.25"]'
                if isinstance(tokens_raw, str):
                    try:
                        tokens_raw = json.loads(tokens_raw)
                    except (json.JSONDecodeError, TypeError):
                        tokens_raw = []

                if outcomes is None:
                    outcomes = ["Yes", "No"]
                elif isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = ["Yes", "No"]

                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except (json.JSONDecodeError, TypeError):
                        prices_raw = []

                if len(tokens_raw) < 2:
                    continue

                tokens = {"yes": tokens_raw[0], "no": tokens_raw[1]}
                try:
                    p_yes = float(prices_raw[0]) if prices_raw else 0.5
                    p_no = float(prices_raw[1]) if len(prices_raw) > 1 else 0.5
                except (ValueError, TypeError, IndexError):
                    p_yes, p_no = 0.5, 0.5
                prices = {"yes": p_yes, "no": p_no}

                raw_tags = m.get("tags")
                tags = []
                if isinstance(raw_tags, list):
                    for t in raw_tags:
                        if isinstance(t, dict):
                            tags.append(t.get("slug", ""))
                        elif isinstance(t, str):
                            tags.append(t)
                elif isinstance(raw_tags, str):
                    try:
                        parsed_tags = json.loads(raw_tags)
                        if isinstance(parsed_tags, list):
                            for t in parsed_tags:
                                if isinstance(t, dict):
                                    tags.append(t.get("slug", ""))
                                elif isinstance(t, str):
                                    tags.append(t)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Volume e liquidity arrivano come stringhe dall'API
                try:
                    vol = float(m.get("volume") or 0)
                except (ValueError, TypeError):
                    vol = 0.0
                try:
                    liq = float(m.get("liquidity") or 0)
                except (ValueError, TypeError):
                    liq = 0.0

                market = Market(
                    id=m.get("id", ""),
                    condition_id=m.get("conditionId", m.get("id", "")),
                    question=m.get("question", m.get("title", "")),
                    slug=m.get("slug", ""),
                    tokens=tokens,
                    prices=prices,
                    volume=vol,
                    liquidity=liq,
                    end_date=m.get("endDate", ""),
                    active=m.get("active", True),
                    tags=tags,
                    outcomes=outcomes,
                    category=m.get("category", ""),
                )
                markets.append(market)
                self._market_cache[market.id] = market

            self._cache_time = time.time()
            return markets

        except Exception as e:
            logger.error(f"Errore fetch mercati: {e}")
            return []

    def fetch_crypto_markets(self) -> list[Market]:
        """Fetch specifico per mercati crypto."""
        markets = self.fetch_markets(limit=200)
        crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"]
        return [
            m for m in markets
            if any(kw in m.question.lower() or kw in " ".join(m.tags) for kw in crypto_kw)
        ]

    def fetch_events(self, limit: int = 50) -> list[dict]:
        """Fetch eventi dalla Gamma API (gruppi di mercati correlati)."""
        try:
            resp = self._session.get(
                f"{self.creds.gamma_host}/events",
                params={"limit": limit, "active": "true", "closed": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Errore fetch eventi: {e}")
            return []

    # ── Order Book & Prezzi ────────────────────────────────────────

    def get_order_book(self, token_id: str) -> dict:
        """
        Ritorna order book normalizzato come dict con bids/asks.
        py-clob-client ritorna un OrderBookSummary dataclass, qui convertiamo
        in dict con liste di {"price": float, "size": float} per uso uniforme.
        """
        try:
            if self.clob:
                book = self.clob.get_order_book(token_id)
                # Converte OrderBookSummary → dict standard
                bids = []
                asks = []
                if hasattr(book, "bids") and book.bids:
                    for o in book.bids:
                        bids.append({"price": str(o.price), "size": str(o.size)})
                if hasattr(book, "asks") and book.asks:
                    for o in book.asks:
                        asks.append({"price": str(o.price), "size": str(o.size)})
                return {"bids": bids, "asks": asks}
        except Exception as e:
            logger.debug(f"Errore order book: {e}")
        return {"bids": [], "asks": []}

    def get_price(self, token_id: str) -> float:
        try:
            if self.clob:
                p = self.clob.get_price(token_id)
                return float(p) if p else 0.0
        except Exception:
            pass
        return 0.0

    def get_midpoint(self, token_id: str) -> float:
        try:
            if self.clob:
                m = self.clob.get_midpoint(token_id)
                return float(m) if m else 0.0
        except Exception:
            pass
        return 0.0

    def get_spread(self, token_id: str) -> float:
        """Calcola lo spread bid-ask per un token."""
        book = self.get_order_book(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return 1.0
        best_bid = float(bids[0].get("price", 0))
        best_ask = float(asks[0].get("price", 1))
        return best_ask - best_bid

    # ── Fill Price Detection (v7.4) ───────────────────────────────

    def get_last_fill(self, token_id: str, side: str = None) -> dict | None:
        """v7.4: Recupera prezzo di fill reale dall'ultimo trade CLOB su questo token.

        Cerca prima nei trade come TAKER (asset_id diretto), poi nei trade
        recenti come MAKER (token nel maker_orders).

        Args:
            token_id: token da cercare
            side: "BUY" o "SELL" per filtrare (None = ultimo qualsiasi)

        Ritorna {"fill_price": float, "fill_size": float} o None.
        """
        if not self._authenticated or not self.clob:
            return None
        try:
            from py_clob_client.headers.headers import create_level_2_headers
            from py_clob_client.clob_types import RequestArgs

            request_args = RequestArgs(method="GET", request_path="/trades")
            headers = create_level_2_headers(
                self.clob.signer, self.clob.creds, request_args
            )

            # v10.5: Filtra per il NOSTRO wallet per evitare di prendere
            # fill price di altri trader sullo stesso token (bug zombie positions)
            our_addr = self.creds.funder_address.lower() if hasattr(self.creds, 'funder_address') else ""

            # 1) Cerca come TAKER: trade con asset_id = token_id
            url = (
                f"{self.creds.host}/trades"
                f"?asset_id={token_id}&next_cursor=MA=="
            )
            resp = self._session.get(url, headers=headers, timeout=5)
            data = resp.json()
            trades = data.get("data", [])
            for t in trades:
                # v10.5: Solo i nostri fill (match su taker address o maker address)
                t_taker = t.get("taker_order_id", "")
                t_maker_addr = ""
                for mo in t.get("maker_orders", []):
                    if mo.get("maker_address", "").lower() == our_addr:
                        t_maker_addr = our_addr
                        break
                # Se conosciamo il nostro address, filtra
                if our_addr and t_maker_addr != our_addr:
                    # Potrebbe essere un nostro taker trade — verifica owner
                    owner = t.get("owner", t.get("trader", "")).lower()
                    if owner and owner != our_addr:
                        continue
                if side and t.get("side", "").upper() != side.upper():
                    continue
                fp = float(t.get("price", 0))
                fs = float(t.get("size", 0))
                if fp > 0 and fs > 0:
                    return {"fill_price": fp, "fill_size": fs}

            # 2) Cerca come MAKER: il nostro token è nei maker_orders
            #    Scarica trade recenti (prima pagina) e cerca nei maker_orders
            our_addr = self.creds.funder_address.lower() if hasattr(self.creds, 'funder_address') else ""
            if not our_addr:
                return None
            url2 = f"{self.creds.host}/trades?next_cursor=MA=="
            resp2 = self._session.get(url2, headers=headers, timeout=5)
            data2 = resp2.json()
            for t in data2.get("data", []):
                for mo in t.get("maker_orders", []):
                    if mo.get("asset_id") != token_id:
                        continue
                    if side and mo.get("side", "").upper() != side.upper():
                        continue
                    fp = float(mo.get("price", 0))
                    fs = float(mo.get("matched_amount", 0))
                    if fp > 0 and fs > 0:
                        return {"fill_price": fp, "fill_size": fs}
        except Exception as e:
            logger.warning(f"[FILL] Errore recupero fill price: {e}")
        return None

    # ── Trading ────────────────────────────────────────────────────

    # Tick minimo Polymarket: $0.01
    TICK = 0.01

    def buy_market(self, token_id: str, amount: float) -> dict | None:
        """Market order (FOK) — TAKER, paga fee. Usare solo come fallback."""
        if not self._authenticated or not self.clob:
            logger.error("Non autenticato")
            return None

        # v5.9.5: Se l'ultimo ordine ha fallito per balance insufficiente,
        # blocca tutti gli ordini per 60 secondi (evita spam API)
        import time as _t
        _now = _t.time()
        if hasattr(self, '_balance_block_until') and _now < self._balance_block_until:
            _remaining = int(self._balance_block_until - _now)
            logger.info(f"[ORDER] BLOCCATO: balance insufficiente (riprovo tra {_remaining}s)")
            return None

        try:
            logger.info(
                f"[ORDER] Market BUY ${amount:.2f} su {token_id[:16]}... (TAKER)"
            )
            args = MarketOrderArgs(token_id=token_id, amount=amount, side="BUY")
            signed = self.clob.create_market_order(args)
            result = self.clob.post_order(signed, OrderType.FOK)
            logger.info(f"[ORDER] Market BUY ${amount:.2f} ESEGUITO")
            # Reset block on success
            self._balance_block_until = 0
            # v7.4: Embed actual fill price from CLOB trade history (retry con backoff)
            import time as _t2
            fill = None
            for _attempt, _delay in enumerate([0.5, 1.5, 3.0], 1):
                _t2.sleep(_delay)
                fill = self.get_last_fill(token_id)
                if fill:
                    break
            if fill and isinstance(result, dict):
                # v10.5: Sanity check — fill price deve essere plausibile
                # rispetto all'amount e size. Se fill a $0.004 su un buy da $20
                # è chiaramente il fill di un altro trader.
                expected_price = amount / max(fill["fill_size"], 0.01)
                if fill["fill_price"] < expected_price * 0.3:
                    logger.warning(
                        f"[FILL] Fill price sospetto: ${fill['fill_price']:.4f} "
                        f"vs expected ~${expected_price:.4f} — IGNORATO (altro trader?)"
                    )
                else:
                    result["_fill_price"] = fill["fill_price"]
                    result["_fill_size"] = fill["fill_size"]
                    logger.info(
                        f"[FILL] Real fill: {fill['fill_size']:.2f} shares "
                        f"@${fill['fill_price']:.4f} (attempt {_attempt})"
                    )
            else:
                logger.warning(f"[FILL] Fill non trovato per {token_id[:16]}... dopo 3 tentativi")
            return result
        except Exception as e:
            err_str = str(e)
            logger.error(f"Errore ordine market: {e}")
            # v5.9.5: Blocca ordini per 60s se balance insufficiente
            if "not enough balance" in err_str or "allowance" in err_str:
                self._balance_block_until = _now + 60
                logger.warning(f"[ORDER] Balance insufficiente — ordini bloccati per 60s")
            return None

    def buy_limit(self, token_id: str, price: float, size: float) -> dict | None:
        """Limit order GTC — MAKER, zero fee + potenziale rebate."""
        if not self._authenticated or not self.clob:
            return None
        try:
            # Arrotonda prezzo al tick
            price = round(price, 2)
            args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
            signed = self.clob.create_order(args)
            result = self.clob.post_order(signed, OrderType.GTC)
            logger.info(
                f"[ORDER] Limit BUY {size:.1f}@${price:.2f} su {token_id[:16]}... (MAKER)"
            )
            return result
        except Exception as e:
            logger.error(f"Errore ordine limit: {e}")
            return None

    def sell_limit(self, token_id: str, price: float, size: float) -> dict | None:
        """Limit SELL GTC — per chiudere posizioni."""
        if not self._authenticated or not self.clob:
            return None
        try:
            price = round(price, 2)
            args = OrderArgs(token_id=token_id, price=price, size=size, side="SELL")
            signed = self.clob.create_order(args)
            result = self.clob.post_order(signed, OrderType.GTC)
            logger.info(
                f"[ORDER] Limit SELL {size:.1f}@${price:.2f} su {token_id[:16]}..."
            )
            return result
        except Exception as e:
            logger.error(f"Errore ordine sell: {e}")
            return None

    def sell_market(self, token_id: str, amount: float) -> dict | None:
        """Market SELL (FOK) — chiude posizione al prezzo di mercato."""
        if not self._authenticated or not self.clob:
            logger.error("Non autenticato per sell_market")
            return None
        try:
            logger.info(
                f"[ORDER] Market SELL ${amount:.2f} su {token_id[:16]}... (TAKER)"
            )
            args = MarketOrderArgs(token_id=token_id, amount=amount, side="SELL")
            signed = self.clob.create_market_order(args)
            result = self.clob.post_order(signed, OrderType.FOK)
            logger.info(f"[ORDER] Market SELL ${amount:.2f} ESEGUITO")
            return result
        except Exception as e:
            logger.error(f"Errore ordine sell market: {e}")
            return None

    def smart_sell(
        self,
        token_id: str,
        shares: float,
        current_price: float,
        timeout_sec: float = 10.0,
        fallback_market: bool = True,
        aggressive: bool = False,
    ) -> dict | None:
        """
        Smart sell: tenta limit sell al best ask - tick (maker), fallback market.

        Args:
            token_id: token da vendere
            shares: numero di shares da vendere
            current_price: prezzo corrente stimato del token
            timeout_sec: timeout per il fill del limit
            fallback_market: se True, fallback a market sell
        """
        if not self._authenticated or not self.clob:
            return None

        book = self.get_order_book(token_id)
        bids = book.get("bids", [])

        if not bids:
            if fallback_market:
                amount = shares * current_price
                return self.sell_market(token_id, amount)
            return None

        best_bid = float(bids[0].get("price", 0))

        # Se il book e' vuoto/illiquido, market diretto
        if best_bid < 0.01:
            logger.info(f"[SMART-SELL] Book vuoto (bid=${best_bid:.2f}) → market")
            if fallback_market:
                amount = shares * current_price
                return self.sell_market(token_id, amount)
            return None

        # Limit sell al best_bid (maker, zero fee)
        limit_price = round(best_bid, 2)

        logger.info(
            f"[SMART-SELL] Limit SELL {shares:.1f}@${limit_price:.2f} "
            f"(bid=${best_bid:.2f}) su {token_id[:16]}..."
        )

        result = self.sell_limit(token_id, limit_price, shares)
        if not result:
            if fallback_market:
                amount = shares * current_price
                return self.sell_market(token_id, amount)
            return None

        # Poll per fill
        order_id = None
        if isinstance(result, dict):
            order_id = result.get("orderID") or result.get("id")

        if order_id:
            import time as _time
            deadline = _time.time() + timeout_sec
            while _time.time() < deadline:
                _time.sleep(2.0)
                try:
                    order = self.clob.get_order(order_id)
                    if order and hasattr(order, "status"):
                        status = str(order.status).upper()
                        if "MATCHED" in status or "FILLED" in status:
                            logger.info(f"[SMART-SELL] Limit FILL confermato")
                            return result
                except Exception:
                    pass

            # Timeout — cancella e fallback
            try:
                self.clob.cancel(order_id)
                logger.info(f"[SMART-SELL] Timeout → cancellato limit")
            except Exception:
                pass

            if fallback_market:
                amount = shares * current_price
                return self.sell_market(token_id, amount)

        return result

    def smart_buy(
        self,
        token_id: str,
        amount: float,
        target_price: float,
        timeout_sec: float = 15.0,
        fallback_market: bool = True,
        aggressive: bool = False,
        inventory_frac: float = 0.0,
        volume_24h: float = 0.0,
        vpin: float = 0.0,
    ) -> dict | None:
        """
        Smart order routing: tenta MAKER limit order, fallback a TAKER market.

        1. Legge l'order book
        2. Posta limit BUY un tick sopra il best bid (= maker, zero fee)
        3. Aspetta fino a timeout_sec per il fill
        4. Se non fillato e fallback_market=True → cancella e invia market order

        Ritorna il risultato dell'ordine eseguito, o None se fallisce.
        """
        if not self._authenticated or not self.clob:
            logger.error("Non autenticato")
            return None

        # 1. Leggi order book per determinare prezzo ottimale
        book = self.get_order_book(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if bids:
            best_bid = float(bids[0].get("price", 0))
        else:
            best_bid = target_price - self.TICK

        if asks:
            best_ask = float(asks[0].get("price", 1))
        else:
            best_ask = target_price + self.TICK

        # v5.9.8: Rewritten spread handling for illiquid markets.
        # The old check (spread > 20% of target) rejected 100% of crypto 5-min
        # markets because their books show bid=$0.01, ask=$0.99 (spread=$0.98).
        # On illiquid markets, place limit at target_price and wait for fill.
        # This is correct maker behavior per Becker 2026.
        spread = best_ask - best_bid

        # Only skip on truly empty book (no bids AND no asks)
        if not bids and not asks:
            logger.info(
                f"[SMART] Book vuoto (no bids/asks) target=${target_price:.2f} "
                f"su {token_id[:16]}..."
            )
            if fallback_market:
                return self.buy_market(token_id, amount)
            return None

        # Determine limit price based on book state
        if spread <= 0.10:
            # Tight spread: standard maker pricing from order book
            if aggressive:
                limit_price = round(min(best_ask - self.TICK, target_price), 2)
                limit_price = max(limit_price, round(best_bid + self.TICK, 2))
            elif inventory_frac > 0 or volume_24h > 0 or vpin > 0:
                # v10.3: Avellaneda-Stoikov optimal bid
                from utils.avellaneda_stoikov import optimal_bid as as_bid
                mid = (best_bid + best_ask) / 2.0
                limit_price = as_bid(
                    mid, best_bid, best_ask, target_price,
                    inventory_frac=inventory_frac,
                    volume_24h=volume_24h,
                    vpin=vpin,
                )
                naive = round(min(best_bid + self.TICK, target_price), 2)
                logger.info(
                    f"[AS-EXEC] bid=${limit_price:.2f} vs naive=${naive:.2f} "
                    f"delta={limit_price - naive:+.3f}"
                )
            else:
                limit_price = round(min(best_bid + self.TICK, target_price), 2)
        else:
            # Wide spread (illiquid): place limit at target_price (maker, wait for fill)
            limit_price = round(target_price, 2)
            # Safety: never at or above best_ask (would cross spread = taker)
            if asks and limit_price >= best_ask:
                limit_price = round(best_ask - self.TICK, 2)
            logger.info(
                f"[SMART] Wide spread (bid=${best_bid:.2f} ask=${best_ask:.2f}) "
                f"→ maker limit @${limit_price:.2f} (target=${target_price:.2f})"
            )

        # Calcola shares da comprare: amount / price
        if limit_price <= 0:
            limit_price = self.TICK
        shares = round(amount / limit_price, 2)
        if shares < 1:
            shares = 1.0

        mode = "AGGRESSIVE" if aggressive else "STANDARD"
        fb = "->taker" if fallback_market else "->skip"
        logger.info(
            f"[SMART-MAKER] {mode} Limit BUY {shares:.1f}@${limit_price:.2f} "
            f"(bid=${best_bid:.2f} ask=${best_ask:.2f} spread=${spread:.3f} "
            f"target=${target_price:.2f} fallback={fb}) su {token_id[:16]}..."
        )

        # 2. Posta limit order (GTC maker)
        result = self.buy_limit(token_id, limit_price, shares)
        if not result:
            # Limit fallito → prova market se consentito
            if fallback_market:
                logger.info("[SMART] Limit fallito, fallback a market order")
                return self.buy_market(token_id, amount)
            return None

        # 3. Estrai order_id per monitoraggio
        order_id = None
        if isinstance(result, dict):
            order_id = result.get("orderID") or result.get("id") or result.get("order_id")
        if not order_id:
            logger.info("[SMART] Limit postato ma nessun orderID — assumo fill immediato")
            return result

        # 4. Attendi fill con polling
        start = time.time()
        poll_interval = 1.0  # Controlla ogni secondo
        filled = False

        while time.time() - start < timeout_sec:
            try:
                order_status = self.clob.get_order(order_id)
                if isinstance(order_status, dict):
                    status = order_status.get("status", "").upper()
                    size_matched = float(order_status.get("size_matched", 0) or 0)

                    if status in ("MATCHED", "FILLED", "CLOSED") or size_matched > 0:
                        filled = True
                        logger.info(
                            f"[SMART] Limit FILLATO! matched={size_matched:.1f} "
                            f"status={status}"
                        )
                        break
                    elif status in ("CANCELLED", "REJECTED"):
                        logger.info(f"[SMART] Ordine {status}")
                        break
            except Exception as e:
                logger.debug(f"[SMART] Errore polling: {e}")
            time.sleep(poll_interval)

        # 5. Se non fillato → cancella e fallback
        if not filled:
            try:
                self.clob.cancel(order_id)
                logger.info(f"[SMART] Limit non fillato in {timeout_sec}s, cancellato")
            except Exception:
                pass

            if fallback_market:
                # v13.1: Price-capped fallback — never pay more than target + 10%
                max_price = round(target_price * 1.10, 2)
                logger.info(
                    f"[SMART] Fallback a limit order @${max_price:.2f} "
                    f"(target=${target_price:.2f} + 10% cap)"
                )
                fallback_result = self.buy_limit(token_id, max_price, shares)
                # CRITICAL: embed the REAL price so PnL is calculated correctly
                if fallback_result and isinstance(fallback_result, dict):
                    fallback_result["_fill_price"] = max_price
                    fallback_result["_fallback_from"] = target_price
                    logger.info(f"[SMART] Fallback fill price recorded: ${max_price:.2f} (was target ${target_price:.2f})")
                return fallback_result
            return None

        # v7.4: Embed actual fill price for limit fills (retry con backoff)
        import time as _t2
        fill = None
        for _attempt, _delay in enumerate([0.5, 1.5, 3.0], 1):
            _t2.sleep(_delay)
            fill = self.get_last_fill(token_id)
            if fill:
                break
        if fill and isinstance(result, dict):
            # v12.9: ALWAYS record actual fill price — even if suspicious
            # The trade happened at this price, PnL must reflect reality
            if fill["fill_price"] < target_price * 0.3:
                logger.warning(
                    f"[FILL] Fill price BASSO: ${fill['fill_price']:.4f} "
                    f"vs target ~${target_price:.4f}"
                )
            elif fill["fill_price"] > target_price * 1.5:
                logger.warning(
                    f"[FILL] Fill price ALTO (slippage): ${fill['fill_price']:.4f} "
                    f"vs target ~${target_price:.4f}"
                )
            result["_fill_price"] = fill["fill_price"]
            result["_fill_size"] = fill["fill_size"]
            if True:
                logger.info(
                    f"[FILL] Real fill: {fill['fill_size']:.2f} shares "
                    f"@${fill['fill_price']:.4f} (attempt {_attempt})"
                )
        else:
            logger.warning(f"[FILL] Fill non trovato per limit {token_id[:16]}... dopo 3 tentativi")
        return result

    # ── Order Management ─────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancella un singolo ordine."""
        try:
            if self.clob:
                self.clob.cancel(order_id)
                return True
        except Exception as e:
            logger.error(f"Errore cancellazione ordine {order_id}: {e}")
        return False

    def cancel_all(self) -> bool:
        """Cancella tutti gli ordini aperti."""
        try:
            if self.clob:
                self.clob.cancel_all()
                return True
        except Exception as e:
            logger.error(f"Errore cancellazione: {e}")
        return False

    def get_open_orders(self) -> list[dict]:
        """Restituisce tutti gli ordini aperti."""
        try:
            if self.clob:
                from py_clob_client.clob_types import OpenOrderParams
                orders = self.clob.get_orders(OpenOrderParams())
                return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.debug(f"Errore get ordini aperti: {e}")
        return []
