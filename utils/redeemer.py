"""
Auto-Redeem per posizioni Polymarket risolte.
==============================================

Monitora i mercati risolti e riscuote automaticamente le vincite
chiamando redeemPositions sul contratto Conditional Tokens (CTF)
attraverso il proxy wallet (Gnosis Safe 1-of-1).

Flusso:
1. Controlla la Gamma API per mercati risolti dove abbiamo posizioni
2. Per ogni mercato risolto, chiama redeemPositions sul CTF
3. La chiamata passa attraverso il Safe proxy via execTransaction

Richiede: web3 (pip install web3)
"""

import json
import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# ── Contratti Polymarket su Polygon ─────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
# v5.9.3: Multiple RPC endpoints con fallback per evitare rate limit
# v7.1.1: RPC aggiornati 2026-02-19 — i vecchi erano tutti morti
POLYGON_RPCS = [
    "https://polygon.gateway.tenderly.co",
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.api.onfinality.io/public",
]
POLYGON_RPC = POLYGON_RPCS[0]  # backward compat
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# ABI minimale per redeemPositions
REDEEM_ABI = json.loads("""[{
    "constant": false,
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"}
    ],
    "name": "redeemPositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
}]""")

# ABI minimale per Gnosis Safe execTransaction
SAFE_EXEC_ABI = json.loads("""[{
    "constant": false,
    "inputs": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
        {"name": "operation", "type": "uint8"},
        {"name": "safeTxGas", "type": "uint256"},
        {"name": "baseGas", "type": "uint256"},
        {"name": "gasPrice", "type": "uint256"},
        {"name": "gasToken", "type": "address"},
        {"name": "refundReceiver", "type": "address"},
        {"name": "signatures", "type": "bytes"}
    ],
    "name": "execTransaction",
    "outputs": [{"name": "success", "type": "bool"}],
    "stateMutability": "payable",
    "type": "function"
}, {
    "constant": true,
    "inputs": [],
    "name": "nonce",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}, {
    "constant": true,
    "inputs": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
        {"name": "operation", "type": "uint8"},
        {"name": "safeTxGas", "type": "uint256"},
        {"name": "baseGas", "type": "uint256"},
        {"name": "gasPrice", "type": "uint256"},
        {"name": "gasToken", "type": "address"},
        {"name": "refundReceiver", "type": "address"},
        {"name": "_nonce", "type": "uint256"}
    ],
    "name": "getTransactionHash",
    "outputs": [{"name": "", "type": "bytes32"}],
    "stateMutability": "view",
    "type": "function"
}]""")

HASH_ZERO = b"\x00" * 32


@dataclass
class ResolvedPosition:
    """Una posizione in un mercato risolto."""
    market_id: str
    condition_id: str
    question: str
    outcome: str  # "YES" o "NO"
    won: bool
    neg_risk: bool


class Redeemer:
    """Riscuote automaticamente le vincite da mercati risolti."""

    def __init__(self, private_key: str, proxy_address: str):
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._w3 = None
        self._account = None
        self._ctf = None
        self._safe = None
        self._last_check: float = 0
        self._redeemed: set[str] = set()  # condition_id gia' riscossi
        self._available = False
        self._init_web3()

    def _init_web3(self):
        """Inizializza web3 e contratti. Prova multipli RPC con fallback."""
        try:
            from web3 import Web3
            from eth_account import Account

            # v5.9.3: Prova RPC in ordine finché uno funziona
            for rpc_url in POLYGON_RPCS:
                self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
                if self._w3.is_connected():
                    logger.info(f"[REDEEM] Connesso a RPC: {rpc_url}")
                    break
            else:
                logger.warning("[REDEEM] Impossibile connettersi a nessun Polygon RPC")
                return

            self._account = Account.from_key(self._private_key)
            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=REDEEM_ABI,
            )
            self._safe = self._w3.eth.contract(
                address=Web3.to_checksum_address(self._proxy_address),
                abi=SAFE_EXEC_ABI,
            )
            self._available = True
            logger.info(
                f"[REDEEM] Inizializzato — proxy={self._proxy_address[:10]}... "
                f"EOA={self._account.address[:10]}..."
            )
        except ImportError:
            logger.warning(
                "[REDEEM] web3 non installato. Installa con: "
                "pip install web3 --break-system-packages"
            )
        except Exception as e:
            logger.warning(f"[REDEEM] Inizializzazione fallita: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def fetch_redeemable_positions(self) -> list[dict]:
        """
        v9.2.3: Query Data API per posizioni redeemable.
        Ritorna lista di dict con conditionId, market info, redeemable flag.
        """
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": self._proxy_address},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"[REDEEM] Data API HTTP {resp.status_code}")
                return []
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("positions", []))
            redeemable = [p for p in items if p.get("redeemable", False)]
            logger.info(f"[REDEEM] Data API: {len(items)} posizioni, {len(redeemable)} redeemable")
            return redeemable
        except Exception as e:
            logger.warning(f"[REDEEM] Data API errore: {e}")
            return []

    def check_and_redeem(self, open_trades: list) -> list[dict]:
        """
        Controlla mercati risolti e riscuote le vincite.
        Ritorna lista di dict con {market_id, condition_id, won} per i mercati processati.
        """
        if not self._available:
            return []

        # Controlla ogni 5 minuti
        now = time.time()
        if now - self._last_check < 300:
            return []
        self._last_check = now

        results = []
        # Raccogli i market_id delle posizioni aperte
        market_ids = {t.market_id for t in open_trades}
        if not market_ids:
            return []

        # Controlla quali mercati sono risolti
        resolved = self._find_resolved_markets(market_ids, open_trades)
        if not resolved:
            return []

        logger.info(f"[REDEEM] Trovati {len(resolved)} mercati risolti con posizioni aperte")

        for pos in resolved:
            if pos.condition_id in self._redeemed:
                continue

            if pos.won:
                logger.info(
                    f"[REDEEM] Riscuoto vincita: '{pos.question[:40]}' "
                    f"outcome={pos.outcome} cond={pos.condition_id[:16]}..."
                )
                success = self._redeem_position(pos)
                if success:
                    self._redeemed.add(pos.condition_id)
                    results.append({"market_id": pos.market_id, "condition_id": pos.condition_id, "won": True})
                    logger.info(f"[REDEEM] Vincita riscossa con successo!")
                else:
                    logger.warning(f"[REDEEM] Riscossione fallita per {pos.condition_id[:16]}")
            else:
                # Posizione perdente — segna come riscossa
                self._redeemed.add(pos.condition_id)
                results.append({"market_id": pos.market_id, "condition_id": pos.condition_id, "won": False})
                logger.info(
                    f"[REDEEM] Posizione perdente chiusa: '{pos.question[:40]}' "
                    f"outcome={pos.outcome}"
                )

        return results

    def _find_resolved_markets(self, market_ids: set[str], trades: list) -> list[ResolvedPosition]:
        """
        v9.2.3: Cerca mercati risolti via Data API (primario) + Gamma API (fallback).
        Data API fornisce flag `redeemable` autoritativo, eliminando inferenza dai prezzi.
        """
        resolved = []

        # Mappa market_id -> lato del nostro trade (BUY_YES o BUY_NO)
        trade_sides = {}
        trade_by_condition = {}  # conditionId -> trade
        for t in trades:
            if t.market_id in market_ids:
                trade_sides[t.market_id] = t.side  # "BUY_YES" o "BUY_NO"

        # ── Fonte primaria: Data API redeemable ──
        found_condition_ids = set()
        try:
            redeemable = self.fetch_redeemable_positions()
            for pos in redeemable:
                cid = pos.get("conditionId", "") or pos.get("condition_id", "")
                if not cid:
                    continue

                # Matcha con i nostri trade via conditionId o market slug
                matched_mid = None
                for mid in market_ids:
                    # Il conditionId potrebbe essere nei dati del trade
                    trade = trade_sides.get(mid)
                    if trade is not None:
                        matched_mid = mid
                        break

                # Fallback: matcha via asset/title se presente nella response
                if not matched_mid:
                    slug = pos.get("slug", "") or pos.get("market_slug", "")
                    title = pos.get("title", "") or pos.get("question", "")
                    for t in trades:
                        if t.market_id in market_ids:
                            t_title = getattr(t, "question", "") or getattr(t, "title", "")
                            if slug and hasattr(t, "slug") and t.slug == slug:
                                matched_mid = t.market_id
                                break
                            if t_title and title and t_title.lower() == title.lower():
                                matched_mid = t.market_id
                                break

                if not matched_mid:
                    continue

                found_condition_ids.add(cid)
                our_side = trade_sides.get(matched_mid, "BUY_YES")
                we_bet_yes = "YES" in our_side.upper()

                # Data API: outcome dalla posizione
                outcome = pos.get("outcome", "") or pos.get("resolution", "")
                if outcome:
                    res_lower = outcome.lower().strip()
                    resolved_yes = res_lower in ("yes", "y", "1", "true")
                    won = (we_bet_yes and resolved_yes) or (not we_bet_yes and not resolved_yes)
                else:
                    # Se redeemable=true, assumiamo che abbiamo vinto
                    won = True

                resolved.append(ResolvedPosition(
                    market_id=matched_mid,
                    condition_id=cid,
                    question=pos.get("title", pos.get("question", "?")),
                    outcome=outcome or "redeemable",
                    won=won,
                    neg_risk=pos.get("negRisk", pos.get("neg_risk", False)),
                ))

        except Exception as e:
            logger.warning(f"[REDEEM] Data API fallback a Gamma: {e}")

        # ── Fallback: Gamma API per mercati non trovati via Data API ──
        remaining_ids = market_ids - {r.market_id for r in resolved}
        if not remaining_ids:
            return resolved

        logger.info(f"[REDEEM] Gamma fallback per {len(remaining_ids)} mercati non trovati via Data API")
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "limit": 100,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            markets = resp.json()

            for m in markets:
                mid = m.get("id", "")
                if mid not in remaining_ids:
                    continue

                condition_id = m.get("conditionId", "")
                if not condition_id:
                    continue

                resolution = m.get("resolution", "")
                neg_risk = m.get("negRisk", False)

                if resolution:
                    our_side = trade_sides.get(mid, "BUY_YES")
                    res_lower = resolution.lower().strip()
                    we_bet_yes = "YES" in our_side.upper()
                    resolved_yes = res_lower in ("yes", "y", "1", "true")
                    won = (we_bet_yes and resolved_yes) or (not we_bet_yes and not resolved_yes)

                    resolved.append(ResolvedPosition(
                        market_id=mid,
                        condition_id=condition_id,
                        question=m.get("question", "?"),
                        outcome=resolution,
                        won=won,
                        neg_risk=neg_risk,
                    ))

        except Exception as e:
            logger.warning(f"[REDEEM] Errore fetch mercati risolti (Gamma): {e}")

        return resolved

    def _redeem_position(self, pos: ResolvedPosition) -> bool:
        """Riscuote una posizione chiamando redeemPositions via Safe proxy."""
        try:
            from web3 import Web3

            # Encode la chiamata redeemPositions
            # v9.2.3: Strict validation (ispirato da Polymarket CLI B256 type)
            # MAI pad silenzioso — un conditionId troncato indica bug nella fonte dati
            condition_id_hex = pos.condition_id.replace("0x", "")
            if len(condition_id_hex) != 64 or not all(c in '0123456789abcdefABCDEF' for c in condition_id_hex):
                logger.error(
                    f"[REDEEM] conditionId malformato (len={len(condition_id_hex)}, "
                    f"attesi 64 hex chars): {pos.condition_id!r}"
                )
                return False
            condition_id_bytes = bytes.fromhex(condition_id_hex)
            collateral = Web3.to_checksum_address(USDC_ADDRESS)

            # v10.0.1: Sempre via CTF direttamente (anche neg_risk).
            # Il Safe proxy ha un bug ABI (GS013) con certi indirizzi target
            # (es. NegRiskAdapter). CTF.redeemPositions gestisce tutte le
            # condizioni, incluse neg_risk — verificato via simulazione on-chain.
            target = Web3.to_checksum_address(CTF_ADDRESS)

            # Encode dei dati della chiamata
            # web3.py v6+: encode_abi (snake_case), v5: encodeABI (camelCase)
            # web3.py v7: encode_abi(fn_name, args=[...])  — positional
            # web3.py v5: encodeABI(fn_name=..., args=[...]) — keyword
            redeem_args = [
                collateral,
                HASH_ZERO,
                condition_id_bytes,
                [1, 2],  # indexSets per mercati binari YES/NO
            ]
            if hasattr(self._ctf, 'encode_abi'):
                redeem_data = self._ctf.encode_abi("redeemPositions", redeem_args)
            else:
                redeem_data = self._ctf.encodeABI(fn_name="redeemPositions", args=redeem_args)

            # Esegui attraverso il Safe proxy (con retry su revert)
            return self._exec_safe_transaction(target, redeem_data)

        except Exception as e:
            logger.error(f"[REDEEM] Errore redeem: {e}")
            return False

    def _exec_safe_transaction(self, to: str, data: str) -> bool:
        """
        Esegue una transazione attraverso il Gnosis Safe proxy.
        v5.9.3: Retry con backoff + fallback RPC per rate limit.
        v9.2.2: Gas estimation + retry on revert con gas incrementato.
        """
        max_retries = 3
        gas_override = 0  # 0 = usa estimation
        for attempt in range(max_retries):
            try:
                from web3 import Web3

                w3 = self._w3
                zero_addr = "0x0000000000000000000000000000000000000000"

                data_bytes = bytes.fromhex(data.replace("0x", ""))

                # v10.0.1: Firma v=1 (approved hash / msg.sender == owner).
                # In Safe v1.3.0 checkNSignatures, v=1 verifica:
                #   require(msg.sender == currentOwner || approvedHashes[...])
                # Poiche' il tx e' inviato dall'EOA (owner), il check passa
                # senza bisogno di ECDSA. Piu' robusto del signing tradizionale
                # che soffriva di GS013 con certi target address.
                signature = (
                    int(self._account.address, 16).to_bytes(32, "big")
                    + b"\x00" * 32
                    + b"\x01"
                )

                exec_fn = self._safe.functions.execTransaction(
                    Web3.to_checksum_address(to),
                    0,
                    data_bytes,
                    0,   # operation
                    0,   # safeTxGas
                    0,   # baseGas
                    0,   # gasPrice
                    zero_addr,
                    zero_addr,
                    signature,
                )

                # v9.2.2: Gas estimation con fallback
                if gas_override > 0:
                    gas_limit = gas_override
                else:
                    try:
                        estimated = exec_fn.estimate_gas({"from": self._account.address})
                        gas_limit = int(estimated * 1.3)  # 30% buffer
                        logger.info(f"[REDEEM] Gas stimato: {estimated}, usando: {gas_limit}")
                    except Exception as gas_err:
                        gas_limit = 500_000
                        logger.warning(f"[REDEEM] Gas estimation fallita ({gas_err}), fallback {gas_limit}")

                # Costruisci e invia la transazione
                tx = exec_fn.build_transaction({
                    "from": self._account.address,
                    "nonce": w3.eth.get_transaction_count(self._account.address),
                    "gas": gas_limit,
                    "gasPrice": w3.eth.gas_price,
                    "chainId": 137,
                })

                # Firma e invia la transazione
                signed_tx = self._account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                logger.info(f"[REDEEM] TX inviata: {tx_hash.hex()} (gas={gas_limit})")

                # Attendi conferma con retry su rate limit
                for wait_attempt in range(3):
                    try:
                        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                        if receipt["status"] == 1:
                            logger.info(
                                f"[REDEEM] TX confermata! Block: {receipt['blockNumber']}, "
                                f"gasUsed: {receipt['gasUsed']}"
                            )
                            return True
                        else:
                            # v9.2.2: Retry on revert con gas incrementato
                            gas_used = receipt.get("gasUsed", 0)
                            logger.warning(
                                f"[REDEEM] TX reverted (gasUsed={gas_used}, "
                                f"gasLimit={gas_limit}, attempt={attempt+1}/{max_retries})"
                            )
                            if attempt < max_retries - 1:
                                # Incrementa gas del 50% per il prossimo tentativo
                                gas_override = int(gas_limit * 1.5)
                                logger.info(f"[REDEEM] Retry con gas={gas_override}")
                                time.sleep(3)
                                break  # esce dal wait loop, rientra nel for attempt
                            return False
                    except Exception as wait_err:
                        err_str = str(wait_err).lower()
                        if "rate limit" in err_str or "too many" in err_str:
                            wait_secs = 15 * (wait_attempt + 1)
                            logger.warning(
                                f"[REDEEM] Rate limit su receipt wait, "
                                f"retry in {wait_secs}s ({wait_attempt+1}/3)"
                            )
                            time.sleep(wait_secs)
                            # Prova un RPC diverso
                            self._switch_rpc()
                        else:
                            raise
                else:
                    # Se il wait loop completa senza break (tutti i wait falliti)
                    logger.warning(
                        f"[REDEEM] TX inviata ma conferma non verificabile. "
                        f"Controlla su polygonscan: 0x{tx_hash.hex()}"
                    )
                    return True  # Assume successo — la TX è on-chain
                continue  # Il break dal wait loop ci porta qui per il retry

            except Exception as e:
                err_str = str(e).lower()
                if ("rate limit" in err_str or "too many" in err_str) and attempt < max_retries - 1:
                    wait_secs = 15 * (attempt + 1)
                    logger.warning(
                        f"[REDEEM] Rate limit, retry in {wait_secs}s "
                        f"(tentativo {attempt+1}/{max_retries})"
                    )
                    time.sleep(wait_secs)
                    self._switch_rpc()
                else:
                    logger.error(f"[REDEEM] Errore execTransaction: {e}")
                    return False

        return False

    def _switch_rpc(self):
        """Switcha a un RPC alternativo dopo rate limit."""
        try:
            from web3 import Web3
            current = self._w3.provider.endpoint_uri
            for rpc in POLYGON_RPCS:
                if rpc != current:
                    new_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                    if new_w3.is_connected():
                        self._w3 = new_w3
                        # Ricostruisci i contratti con il nuovo provider
                        self._ctf = new_w3.eth.contract(
                            address=Web3.to_checksum_address(CTF_ADDRESS),
                            abi=REDEEM_ABI,
                        )
                        self._safe = new_w3.eth.contract(
                            address=Web3.to_checksum_address(self._proxy_address),
                            abi=SAFE_EXEC_ABI,
                        )
                        logger.info(f"[REDEEM] Switchato a RPC: {rpc}")
                        return
            logger.warning("[REDEEM] Nessun RPC alternativo disponibile")
        except Exception as e:
            logger.warning(f"[REDEEM] Errore switch RPC: {e}")
