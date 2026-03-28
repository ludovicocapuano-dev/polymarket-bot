"""
On-Chain Monitor — Rilevamento trade whale via Polygon WebSocket
================================================================
v11.0: Monitora blocchi Polygon in real-time, decodifica calldata
matchOrders per rilevare trade dei wallet tracciati (~2s latency).

Architettura:
- WebSocket connesso a un nodo Polygon (Alchemy/Infura/QuickNode)
- eth_subscribe("newHeads") per ricevere notifica ad ogni nuovo blocco
- Per ogni blocco: fetch transazioni complete, filtra per contratti
  CTF Exchange / Neg Risk Adapter / Operator
- Decodifica calldata matchOrders per estrarre Order struct
- Se il maker di un ordine e' in tracked_wallets, emette trade_event

Usa: websockets + web3 (eth_subscribe newHeads + get_block).
Dependencies: web3, eth_abi (parte di web3 deps), websockets.

Graceful degradation: se web3 non e' installato, il modulo esporta
una classe stub che non fa nulla (il bot non crasha).
"""

import asyncio
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Tentativo import web3 + eth_abi (graceful degradation) ──
try:
    from web3 import Web3
    from web3.contract import Contract
    from eth_abi import decode as abi_decode

    _HAS_WEB3 = True
except ImportError:
    _HAS_WEB3 = False
    logger.warning(
        "[ONCHAIN] web3 non installato — OnChainMonitor disabilitato. "
        "Installa con: pip install web3"
    )

# ── Tentativo import websockets (per raw fallback) ──
try:
    import websockets

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


# ── Contratti Polymarket su Polygon (da echandsome) ──
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
OPERATOR = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

MONITORED_CONTRACTS = [
    CTF_EXCHANGE.lower(),
    NEG_RISK_ADAPTER.lower(),
    OPERATOR.lower(),
]

# ── matchOrders function selector ──
# keccak256("matchOrders(
#   (uint256,address,address,address,uint256,uint256,uint256,uint256,
#    uint256,uint256,uint8,uint8,bytes),
#   (uint256,address,address,address,uint256,uint256,uint256,uint256,
#    uint256,uint256,uint8,uint8,bytes)[],
#   uint256,uint256,uint256[],uint256,uint256[]
# )") = 0x2287e350...
MATCH_ORDERS_SELECTOR = "0x2287e350"

# ── ABI della struct Order (13 campi) ──
# Usato per decodificare calldata matchOrders tramite web3 Contract o eth_abi
ORDER_TUPLE_TYPE = (
    "(uint256,address,address,address,uint256,uint256,uint256,"
    "uint256,uint256,uint256,uint8,uint8,bytes)"
)

# ABI completa della funzione matchOrders per il Contract decoder di web3
MATCH_ORDERS_ABI = [
    {
        "name": "matchOrders",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "takerOrder",
                "type": "tuple",
                "components": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "signature", "type": "bytes"},
                ],
            },
            {
                "name": "makerOrders",
                "type": "tuple[]",
                "components": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "signature", "type": "bytes"},
                ],
            },
            {"name": "takerFillAmount", "type": "uint256"},
            {"name": "takerReceiveAmount", "type": "uint256"},
            {"name": "makerFillAmounts", "type": "uint256[]"},
            {"name": "takerFeeAmount", "type": "uint256"},
            {"name": "makerFeeAmounts", "type": "uint256[]"},
        ],
        "outputs": [],
    }
]

# ── Endpoint WSS Polygon (fallback multipli) ──
import os as _os
_alchemy_key = _os.getenv("ALCHEMY_POLYGON_KEY", "")
POLYGON_WSS_ENDPOINTS = [
    *([ f"wss://polygon-mainnet.g.alchemy.com/v2/{_alchemy_key}"] if _alchemy_key else []),
    "wss://polygon-bor-rpc.publicnode.com",
    "wss://polygon.drpc.org",
]

# ── Parametri connessione ──
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_BASE_DELAY = 5.0   # secondi, backoff esponenziale fino a 60s
RECONNECT_MAX_DELAY = 60.0   # cap backoff

# USDC e token CTF hanno entrambi 6 decimali su Polymarket
DECIMALS = 1e6


class OnChainMonitor:
    """
    Monitor on-chain Polygon per rilevamento trade whale in real-time.

    Si connette via WebSocket a un nodo Polygon, sottoscrive newHeads
    (nuovi blocchi), e per ogni blocco:
    1. Fetch transazioni complete
    2. Filtra per tx.to in MONITORED_CONTRACTS
    3. Decodifica calldata matchOrders
    4. Controlla se maker/taker e' in tracked_wallets
    5. Emette trade_event via callback registrate

    Latenza tipica: ~2 secondi (vs ~120s del polling HTTP attuale).

    Uso:
        monitor = OnChainMonitor(tracked_wallets={"0xabc...", "0xdef..."})
        monitor.add_callback(my_handler)
        await monitor.start()  # in asyncio.gather col bot
    """

    def __init__(
        self,
        wss_url: str | None = None,
        tracked_wallets: set[str] | None = None,
    ):
        # URL WebSocket: da parametro, env var, o fallback a endpoint gratuiti
        self._wss_url = wss_url or os.getenv("POLYGON_WSS", "")
        # Build lista endpoint con deduplica: env var primaria + fallback gratuiti
        seen: set[str] = set()
        self._wss_endpoints: list[str] = []
        for ep in ([self._wss_url] if self._wss_url else []) + list(POLYGON_WSS_ENDPOINTS):
            if ep and ep not in seen:
                seen.add(ep)
                self._wss_endpoints.append(ep)
        if not self._wss_url:
            # Nessuna env var: usa il primo endpoint gratuito
            self._wss_url = self._wss_endpoints[0] if self._wss_endpoints else ""
        self._wss_endpoint_idx = 0
        if not self._wss_url:
            logger.warning(
                "[ONCHAIN] Nessun endpoint WSS disponibile. "
                "Imposta POLYGON_WSS o verifica POLYGON_WSS_ENDPOINTS."
            )

        # Wallet monitorati (tutti lowercase per confronto case-insensitive)
        self._tracked_wallets: set[str] = {
            w.lower() for w in (tracked_wallets or set())
        }

        # Callback registrate: chiamate per ogni trade rilevato
        self._callbacks: list[Callable[[dict], None]] = []

        # Stato
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self._task: asyncio.Task | None = None

        # Web3 provider e contract decoder (inizializzati in _connect)
        self._w3: Any = None
        self._contract: Any = None

        # Statistiche per monitoring
        self._stats: dict[str, Any] = {
            "blocks_processed": 0,
            "trades_detected": 0,
            "errors": 0,
            "last_block_number": 0,
            "last_block_time": 0.0,
            "started_at": 0.0,
            "reconnections": 0,
        }

    # ── API pubblica ──

    async def start(self) -> None:
        """
        Avvia il monitor on-chain.

        Connette il WebSocket, sottoscrive newHeads, e processa
        ogni nuovo blocco in un loop asincrono.
        Gestisce automaticamente riconnessioni con backoff esponenziale.
        """
        if not _HAS_WEB3:
            logger.warning(
                "[ONCHAIN] web3 non disponibile — monitor non avviato. "
                "Installa con: pip install web3"
            )
            return

        if not self._wss_url:
            logger.warning(
                "[ONCHAIN] Nessun URL WebSocket configurato — monitor non avviato. "
                "Imposta POLYGON_WSS nell'ambiente."
            )
            return

        self._running = True
        self._stats["started_at"] = time.time()

        logger.info(
            f"[ONCHAIN] Monitor avviato — "
            f"WSS={self._wss_url[:40]}... | "
            f"{len(self._tracked_wallets)} wallet monitorati | "
            f"{len(self._callbacks)} callback registrate"
        )

        # Loop principale con riconnessione automatica
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("[ONCHAIN] Monitor cancellato — shutdown")
                break
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"[ONCHAIN] Errore nel loop principale: {e}")

                if not self._running:
                    break

                # Riconnessione con backoff esponenziale
                await self._handle_reconnect()

        logger.info(
            f"[ONCHAIN] Monitor fermato. Stats: "
            f"blocchi={self._stats['blocks_processed']}, "
            f"trade={self._stats['trades_detected']}, "
            f"errori={self._stats['errors']}"
        )

    async def stop(self) -> None:
        """Shutdown graceful del monitor."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[ONCHAIN] Stop richiesto — shutdown in corso")

    def add_callback(self, fn: Callable[[dict], None]) -> None:
        """
        Registra una callback per i trade rilevati.

        La callback riceve un dict con:
          source, block_number, tx_hash, timestamp, maker_address,
          token_id, side, maker_amount, taker_amount, price, size_usdc
        """
        self._callbacks.append(fn)
        logger.debug(f"[ONCHAIN] Callback registrata (totale: {len(self._callbacks)})")

    def update_tracked_wallets(self, wallets: set[str]) -> None:
        """
        Aggiorna dinamicamente la lista di wallet monitorati.

        Thread-safe: il set viene sostituito atomicamente.
        """
        old_count = len(self._tracked_wallets)
        self._tracked_wallets = {w.lower() for w in wallets}
        logger.info(
            f"[ONCHAIN] Wallet aggiornati: {old_count} -> {len(self._tracked_wallets)}"
        )

    @property
    def stats(self) -> dict:
        """Ritorna statistiche del monitor per dashboard/logging."""
        result = dict(self._stats)
        result["running"] = self._running
        result["tracked_wallets"] = len(self._tracked_wallets)
        result["callbacks"] = len(self._callbacks)
        result["uptime_s"] = (
            time.time() - self._stats["started_at"]
            if self._stats["started_at"] > 0
            else 0.0
        )
        return result

    @property
    def running(self) -> bool:
        return self._running

    # ── Internals ──

    async def _connect_and_listen(self) -> None:
        """
        Connessione WebSocket + subscription newHeads + loop di ascolto.

        Usa web3 WebSocketProvider per connettersi al nodo Polygon.
        Sottoscrive eth_subscribe("newHeads") per ricevere header di ogni
        nuovo blocco, poi processa il blocco completo.
        """
        logger.info(f"[ONCHAIN] Connessione a {self._wss_url[:50]}...")

        # Crea provider Web3 WebSocket (web3 v7: LegacyWebSocketProvider)
        self._w3 = Web3(Web3.LegacyWebSocketProvider(self._wss_url))

        # Verifica connessione
        if not self._w3.is_connected():
            raise ConnectionError(
                f"[ONCHAIN] Impossibile connettersi a {self._wss_url[:40]}..."
            )

        # Inizializza il contract decoder per matchOrders
        # Usiamo un indirizzo fittizio — serve solo per decodificare calldata
        self._contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE),
            abi=MATCH_ORDERS_ABI,
        )

        chain_id = self._w3.eth.chain_id
        latest_block = self._w3.eth.block_number

        logger.info(
            f"[ONCHAIN] Connesso! chain_id={chain_id} "
            f"blocco_corrente={latest_block}"
        )

        # Reset contatore riconnessioni su connessione riuscita
        self._reconnect_attempts = 0

        # Polling blocchi via web3 (piu' robusto di eth_subscribe raw)
        # Polygon produce blocchi ogni ~2 secondi
        last_processed = latest_block

        while self._running:
            try:
                current_block = self._w3.eth.block_number

                # Processa tutti i blocchi nuovi (in caso di ritardo)
                while last_processed < current_block and self._running:
                    next_block = last_processed + 1
                    await self._process_block(next_block)
                    last_processed = next_block

                # Attendi prima del prossimo check (~1s, Polygon ~2s per blocco)
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning(
                    f"[ONCHAIN] Errore nel loop blocchi "
                    f"(blocco ~{last_processed}): {e}"
                )
                # Pausa breve per evitare spam su errori ripetuti
                await asyncio.sleep(2.0)

                # Se il provider si e' disconnesso, riconnetti
                try:
                    if not self._w3.is_connected():
                        raise ConnectionError("Provider disconnesso")
                except Exception:
                    raise ConnectionError(
                        "[ONCHAIN] Provider WebSocket disconnesso"
                    )

    async def _process_block(self, block_number: int) -> None:
        """
        Processa un singolo blocco: fetch transazioni, filtra per
        contratti monitorati, decodifica calldata matchOrders.

        Operazione asincrona (wrapped in executor per web3 sincrono).
        """
        try:
            # web3 e' sincrono — eseguiamo in executor per non bloccare il loop
            loop = asyncio.get_event_loop()
            block = await loop.run_in_executor(
                None,
                lambda: self._w3.eth.get_block(block_number, full_transactions=True),
            )

            if block is None:
                logger.debug(f"[ONCHAIN] Blocco {block_number} non trovato (reorg?)")
                return

            block_timestamp = float(block.get("timestamp", time.time()))
            transactions = block.get("transactions", [])

            # Filtra transazioni dirette ai contratti monitorati
            relevant_txs = []
            for tx in transactions:
                tx_to = tx.get("to", "")
                if tx_to and tx_to.lower() in MONITORED_CONTRACTS:
                    relevant_txs.append(tx)

            # Decodifica matchOrders per ogni TX rilevante
            for tx in relevant_txs:
                tx_input = tx.get("input", b"")
                tx_hash = tx.get("hash", b"").hex() if isinstance(
                    tx.get("hash", b""), bytes
                ) else str(tx.get("hash", ""))

                # Converti input in bytes se necessario
                if isinstance(tx_input, str):
                    if tx_input.startswith("0x"):
                        tx_input = bytes.fromhex(tx_input[2:])
                    else:
                        tx_input = bytes.fromhex(tx_input)

                # Controlla se e' una chiamata matchOrders (selector 4 byte)
                if len(tx_input) < 4:
                    continue

                selector = "0x" + tx_input[:4].hex()
                if selector != MATCH_ORDERS_SELECTOR:
                    continue

                # Decodifica e processa
                trade_events = self._decode_match_orders(
                    tx_data=tx_input,
                    tx_hash=tx_hash,
                    block_number=block_number,
                    block_timestamp=block_timestamp,
                )

                for event in trade_events:
                    self._stats["trades_detected"] += 1
                    self._emit_event(event)

            # Aggiorna statistiche
            self._stats["blocks_processed"] += 1
            self._stats["last_block_number"] = block_number
            self._stats["last_block_time"] = time.time()

            # Log periodico ogni 500 blocchi (~16 min) per confermare attività
            if self._stats["blocks_processed"] % 500 == 0:
                logger.info(
                    f"[ONCHAIN] Heartbeat: {self._stats['blocks_processed']} blocchi "
                    f"processati, {self._stats['trades_detected']} trade whale, "
                    f"ultimo blocco #{block_number}"
                )

            if relevant_txs:
                logger.debug(
                    f"[ONCHAIN] Blocco {block_number}: "
                    f"{len(transactions)} tx totali, "
                    f"{len(relevant_txs)} tx su contratti monitorati"
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(
                f"[ONCHAIN] Errore processando blocco {block_number}: {e}"
            )

    def _decode_match_orders(
        self,
        tx_data: bytes,
        tx_hash: str,
        block_number: int,
        block_timestamp: float,
    ) -> list[dict]:
        """
        Decodifica calldata matchOrders e estrae trade rilevanti.

        Struttura matchOrders:
          - takerOrder: Order struct (13 campi)
          - makerOrders: Order[] array di struct
          - takerFillAmount, takerReceiveAmount: uint256
          - makerFillAmounts: uint256[]
          - takerFeeAmount: uint256
          - makerFeeAmounts: uint256[]

        Controlla se maker o taker di qualsiasi ordine e' in
        tracked_wallets. Se si', emette un trade_event.

        Ritorna lista di trade_event dict.
        """
        events: list[dict] = []

        try:
            # Decodifica usando il contract ABI di web3 (metodo piu' robusto)
            # decode_function_input gestisce il parsing completo dell'ABI
            func_obj, decoded = self._contract.decode_function_input(tx_data)

            taker_order = decoded.get("takerOrder", ())
            maker_orders = decoded.get("makerOrders", [])

            # Raccogli tutti gli ordini da controllare
            # Ogni Order e' una tupla con 13 campi nell'ordine ABI
            all_orders = []

            if taker_order:
                all_orders.append(("taker", taker_order))

            for i, mo in enumerate(maker_orders):
                all_orders.append((f"maker_{i}", mo))

            for role, order in all_orders:
                # Estrai campi dalla struct Order.
                # web3 decode_function_input ritorna dict con chiavi nominate
                # se l'ABI ha i nomi dei componenti (il nostro caso), oppure
                # tuple posizionale per ABI senza nomi. Supportiamo entrambi.
                try:
                    if isinstance(order, dict):
                        # Dict con chiavi nominate (web3 con ABI componenti)
                        maker_addr = str(order["maker"]).lower()
                        token_id = str(order["tokenId"])
                        maker_amount = int(order["makerAmount"])
                        taker_amount = int(order["takerAmount"])
                        side = int(order["side"])  # 0=BUY, 1=SELL
                    else:
                        # Tuple posizionale (fallback per ABI senza nomi)
                        # 0=salt, 1=maker, 2=signer, 3=taker, 4=tokenId,
                        # 5=makerAmount, 6=takerAmount, 7=expiration,
                        # 8=nonce, 9=feeRateBps, 10=side, 11=signatureType,
                        # 12=signature
                        maker_addr = str(order[1]).lower()
                        token_id = str(order[4])
                        maker_amount = int(order[5])
                        taker_amount = int(order[6])
                        side = int(order[10])
                except (IndexError, KeyError, TypeError, ValueError) as e:
                    logger.debug(
                        f"[ONCHAIN] Errore parsing order {role} in tx {tx_hash[:16]}: {e}"
                    )
                    continue

                # Controlla se il maker e' un wallet tracciato
                if maker_addr not in self._tracked_wallets:
                    continue

                # Calcola prezzo e size in USDC
                price = self._calc_price(side, maker_amount, taker_amount)
                size_usdc = self._calc_size_usdc(side, maker_amount, taker_amount)

                event = {
                    "source": "onchain",
                    "block_number": block_number,
                    "tx_hash": tx_hash,
                    "timestamp": block_timestamp,
                    "maker_address": maker_addr,
                    "token_id": token_id,
                    "side": side,  # 0=BUY, 1=SELL
                    "maker_amount": maker_amount,
                    "taker_amount": taker_amount,
                    "price": price,
                    "size_usdc": size_usdc,
                }

                logger.info(
                    f"[ONCHAIN] Trade whale rilevato! "
                    f"blocco={block_number} "
                    f"tx={tx_hash[:16]}... "
                    f"wallet={maker_addr[:10]}... "
                    f"{'BUY' if side == 0 else 'SELL'} "
                    f"${size_usdc:,.2f} @{price:.4f} "
                    f"token={token_id[:16]}..."
                )

                events.append(event)

        except Exception as e:
            # La decodifica puo' fallire per ABI incompatibili, versioni
            # diverse del contratto, o TX non-matchOrders con stesso selector
            logger.debug(
                f"[ONCHAIN] Errore decodifica matchOrders tx={tx_hash[:16]}...: {e}"
            )

        return events

    @staticmethod
    def _calc_price(side: int, maker_amount: int, taker_amount: int) -> float:
        """
        Calcola il prezzo di esecuzione da maker/taker amount.

        Side 0 (BUY): il maker compra outcome token, paga USDC
          price = makerAmount / takerAmount (USDC per token)
        Side 1 (SELL): il maker vende outcome token, riceve USDC
          price = takerAmount / makerAmount (USDC per token)

        Entrambi gli importi sono in raw units (6 decimali sia USDC
        che CTF token su Polymarket).
        """
        try:
            if side == 0:  # BUY
                if taker_amount > 0:
                    return maker_amount / taker_amount
            else:  # SELL
                if maker_amount > 0:
                    return taker_amount / maker_amount
        except (ZeroDivisionError, OverflowError):
            pass
        return 0.0

    @staticmethod
    def _calc_size_usdc(side: int, maker_amount: int, taker_amount: int) -> float:
        """
        Calcola la dimensione del trade in USDC (diviso per 1e6 decimali).

        Side 0 (BUY): il maker paga makerAmount in USDC
        Side 1 (SELL): il maker riceve takerAmount in USDC
        """
        if side == 0:  # BUY — USDC e' il makerAmount
            return maker_amount / DECIMALS
        else:  # SELL — USDC e' il takerAmount
            return taker_amount / DECIMALS

    def _emit_event(self, event: dict) -> None:
        """
        Invia trade_event a tutte le callback registrate.

        Ogni callback viene chiamata in modo isolato (try/except)
        per evitare che un errore in una callback blocchi le altre.
        """
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.warning(
                    f"[ONCHAIN] Errore nella callback: {e}"
                )

    async def _handle_reconnect(self) -> None:
        """
        Gestisce la riconnessione con backoff esponenziale e rotazione endpoint.

        Delay: RECONNECT_BASE_DELAY * 2^attempts, cap a RECONNECT_MAX_DELAY.
        Dopo MAX_RECONNECT_ATTEMPTS tentativi falliti, il monitor si ferma.
        Ruota tra gli endpoint WSS disponibili ad ogni tentativo.
        """
        self._reconnect_attempts += 1
        self._stats["reconnections"] += 1

        if self._reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
            logger.error(
                f"[ONCHAIN] Superato il limite di {MAX_RECONNECT_ATTEMPTS} "
                f"tentativi di riconnessione — monitor fermato"
            )
            self._running = False
            return

        # Rotazione endpoint WSS (round-robin tra quelli disponibili)
        if len(self._wss_endpoints) > 1:
            self._wss_endpoint_idx = (
                (self._wss_endpoint_idx + 1) % len(self._wss_endpoints)
            )
            self._wss_url = self._wss_endpoints[self._wss_endpoint_idx]

        # Backoff esponenziale con cap
        delay = min(
            RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempts - 1)),
            RECONNECT_MAX_DELAY,
        )

        logger.warning(
            f"[ONCHAIN] Riconnessione {self._reconnect_attempts}/"
            f"{MAX_RECONNECT_ATTEMPTS} tra {delay:.1f}s "
            f"→ {self._wss_url[:50]}..."
        )

        await asyncio.sleep(delay)


# ── Stub class per graceful degradation (quando web3 non e' installato) ──

if not _HAS_WEB3:

    class OnChainMonitor:  # type: ignore[no-redef]
        """
        Stub: web3 non installato.

        Questa classe non fa nulla — il bot continua a funzionare
        con il polling HTTP esistente (whale_copy via data-api).
        Installa web3 per attivare il monitor on-chain:
            pip install web3
        """

        def __init__(self, wss_url: str | None = None, tracked_wallets: set | None = None):
            self._running = False
            self._tracked_wallets: set[str] = set()
            self._callbacks: list = []
            self._stats: dict = {
                "blocks_processed": 0,
                "trades_detected": 0,
                "errors": 0,
                "last_block_number": 0,
                "last_block_time": 0.0,
                "started_at": 0.0,
                "reconnections": 0,
            }
            logger.info(
                "[ONCHAIN] Stub attivo — web3 non installato, "
                "monitor on-chain disabilitato"
            )

        async def start(self) -> None:
            logger.info("[ONCHAIN] Stub: start() no-op (web3 mancante)")

        async def stop(self) -> None:
            self._running = False

        def add_callback(self, fn: Any) -> None:
            pass

        def update_tracked_wallets(self, wallets: set) -> None:
            self._tracked_wallets = {str(w).lower() for w in wallets}

        @property
        def stats(self) -> dict:
            return dict(self._stats)

        @property
        def running(self) -> bool:
            return False
