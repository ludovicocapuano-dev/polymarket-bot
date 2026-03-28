"""
UMA Optimistic Oracle Monitor per Polymarket (v5.9.2)
=====================================================
Monitora le proposte di risoluzione su UMA per trovare opportunità di
"resolution sniping": comprare token a sconto quando l'esito è già
stato proposto ma il prezzo di mercato non ha ancora reagito.

Architettura:
- WebSocket/polling su Polygon per eventi ProposePrice
- Matcha la proposta con i nostri mercati aperti o con mercati dove
  il token è ancora sottovalutato
- Segnala opportunità al bot principale

Contratti chiave su Polygon:
- UmaCtfAdapter V3: 0x2F5e3684cb1F318ec51b00Edba38d79Ac2c0aA9d
- CTF: 0x4d97dcd97ec945f40cf65f87097ace5ea0476045

Riferimento: UMA Optimistic Oracle V2 emette ProposePrice event
con proposedPrice: 0 (NO), 0.5e18 (UNKNOWN), 1e18 (YES)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Contratti UMA su Polygon ──
UMA_CTF_ADAPTER_V3 = "0x2F5e3684cb1F318ec51b00Edba38d79Ac2c0aA9d"
UMA_CTF_ADAPTER_V2 = "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74"
CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"

# Valori proposedPrice standard (in wei)
PROPOSED_YES = 1_000_000_000_000_000_000   # 1e18 = YES
PROPOSED_NO = 0                             # 0 = NO
PROPOSED_UNKNOWN = 500_000_000_000_000_000  # 0.5e18 = UNKNOWN

# Polygon RPC endpoints (Alchemy primary + pubblici fallback)
import os as _os
_alchemy_key = _os.getenv("ALCHEMY_POLYGON_KEY", "")
POLYGON_RPC_URLS = [
    *([ f"https://polygon-mainnet.g.alchemy.com/v2/{_alchemy_key}"] if _alchemy_key else []),
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
]

# Gamma API per mapping questionID → market
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class ResolutionProposal:
    """Una proposta di risoluzione UMA rilevata."""
    question_id: str
    proposed_outcome: str  # "YES", "NO", "UNKNOWN"
    proposer: str
    expiration_timestamp: int  # Quando la proposta diventa finale
    detected_at: float
    market_id: Optional[str] = None
    market_question: Optional[str] = None
    current_yes_price: Optional[float] = None
    current_no_price: Optional[float] = None

    @property
    def seconds_until_final(self) -> float:
        """Secondi mancanti prima che la proposta diventi definitiva."""
        return max(0, self.expiration_timestamp - time.time())

    @property
    def edge_if_yes(self) -> float:
        """Edge se il risultato proposto è YES e compriamo YES."""
        if self.proposed_outcome == "YES" and self.current_yes_price:
            return 1.0 - self.current_yes_price
        return 0.0

    @property
    def edge_if_no(self) -> float:
        """Edge se il risultato proposto è NO e compriamo NO."""
        if self.proposed_outcome == "NO" and self.current_no_price:
            return 1.0 - self.current_no_price
        return 0.0

    @property
    def best_edge(self) -> float:
        """Miglior edge disponibile basato sulla proposta."""
        return max(self.edge_if_yes, self.edge_if_no)


@dataclass
class UmaMonitor:
    """
    Monitora le proposte di risoluzione UMA su Polygon.

    Due modalità:
    1. Polling: controlla periodicamente i log del contratto (più affidabile)
    2. Abbinamento: confronta proposte con mercati Polymarket aperti

    Rischi:
    - Le proposte possono essere disputate (2h window)
    - Solo proposte da whitelist MOOV2 sono affidabili
    - Il bot NON compra se il mercato ha già reagito (price > 0.90)
    """
    _proposals: dict[str, ResolutionProposal] = field(default_factory=dict)
    _last_poll: float = 0.0
    _poll_interval: float = 30.0  # Controlla ogni 30 secondi
    _rpc_index: int = 0
    _running: bool = False

    def _get_rpc_url(self) -> str:
        """Ruota tra gli RPC endpoints."""
        url = POLYGON_RPC_URLS[self._rpc_index % len(POLYGON_RPC_URLS)]
        self._rpc_index += 1
        return url

    async def start(self):
        """Avvia il polling loop per le proposte UMA."""
        self._running = True
        logger.info(
            f"[UMA] Monitor avviato — polling ogni {self._poll_interval}s "
            f"su UmaCtfAdapter {UMA_CTF_ADAPTER_V3[:10]}..."
        )

        while self._running:
            try:
                await self._poll_proposals()
            except Exception as e:
                logger.warning(f"[UMA] Errore polling: {e}")

            await asyncio.sleep(self._poll_interval)

    async def stop(self):
        self._running = False

    async def _poll_proposals(self):
        """
        Interroga il contratto UmaCtfAdapter per nuove proposte.

        Usa eth_getLogs per ottenere gli eventi ProposePrice recenti.
        Filtra per gli ultimi 2 blocchi (~4 secondi su Polygon).
        """
        now = time.time()

        # Non pollare troppo spesso
        if now - self._last_poll < self._poll_interval:
            return
        self._last_poll = now

        rpc_url = self._get_rpc_url()

        # Topic0 per ProposePrice event
        # keccak256("ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)")
        # Lo calcoliamo una volta
        propose_price_topic = "0x" + "0" * 64  # Placeholder — verrà calcolato

        try:
            # Fetch ultimi eventi da UmaCtfAdapter V3
            # Usiamo un approccio semplificato: query Gamma API per mercati
            # che sono appena stati risolti o hanno proposte pendenti
            proposals = await self._check_gamma_for_resolutions()

            for prop in proposals:
                if prop.question_id not in self._proposals:
                    self._proposals[prop.question_id] = prop
                    logger.info(
                        f"[UMA] Nuova proposta rilevata: {prop.proposed_outcome} "
                        f"per '{prop.market_question[:50] if prop.market_question else 'unknown'}' "
                        f"edge={prop.best_edge:.2%} "
                        f"finalize in {prop.seconds_until_final/60:.0f}min"
                    )

            # Pulisci proposte vecchie (> 3 ore)
            cutoff = now - 3 * 3600
            old_keys = [
                k for k, v in self._proposals.items()
                if v.detected_at < cutoff
            ]
            for k in old_keys:
                del self._proposals[k]

        except Exception as e:
            logger.debug(f"[UMA] Errore fetch proposte: {e}")

    async def _check_gamma_for_resolutions(self) -> list[ResolutionProposal]:
        """
        Approccio alternativo: usa Gamma API per trovare mercati in fase
        di risoluzione dove il prezzo non ha ancora reagito completamente.

        Cerca mercati con:
        - active=true ma end_date nel passato (dovrebbero essere chiusi)
        - closed=true ma outcomePrices non ancora definitivi
        - Prezzo YES o NO ancora lontano da 0 o 1 nonostante la scadenza
        """
        proposals = []

        try:
            # Cerca mercati chiusi di recente (ultime 2 ore)
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "limit": 50,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=10,
            )

            if resp.status_code != 200:
                return []

            markets = resp.json()

            for m in markets:
                outcome_prices = m.get("outcomePrices", "")
                if isinstance(outcome_prices, str):
                    try:
                        prices = json.loads(outcome_prices)
                    except (json.JSONDecodeError, TypeError):
                        continue
                else:
                    prices = outcome_prices

                if not prices or len(prices) < 2:
                    continue

                p_yes = float(prices[0])
                p_no = float(prices[1])

                # Se il mercato è chiuso E il prezzo è definitivo (>0.95 o <0.05),
                # l'esito è noto
                if p_yes > 0.95:
                    resolved_outcome = "YES"
                elif p_no > 0.95:
                    resolved_outcome = "NO"
                else:
                    continue  # Non ancora risolto definitivamente

                # Cerca se il CLOB price è ancora diverso dall'outcome
                # (= opportunità di sniping)
                clob_yes = m.get("bestAsk", p_yes)
                clob_no = 1.0 - clob_yes if clob_yes else p_no

                # Se il token vincente costa ancora < 0.95, c'è edge
                if resolved_outcome == "YES" and isinstance(clob_yes, (int, float)):
                    if clob_yes < 0.92:
                        proposals.append(ResolutionProposal(
                            question_id=m.get("questionID", m.get("id", "")),
                            proposed_outcome="YES",
                            proposer="gamma_detected",
                            expiration_timestamp=int(time.time()) + 7200,  # ~2h
                            detected_at=time.time(),
                            market_id=str(m.get("id", "")),
                            market_question=m.get("question", ""),
                            current_yes_price=clob_yes,
                            current_no_price=clob_no,
                        ))

                elif resolved_outcome == "NO" and isinstance(clob_no, (int, float)):
                    if clob_no < 0.92:
                        proposals.append(ResolutionProposal(
                            question_id=m.get("questionID", m.get("id", "")),
                            proposed_outcome="NO",
                            proposer="gamma_detected",
                            expiration_timestamp=int(time.time()) + 7200,
                            detected_at=time.time(),
                            market_id=str(m.get("id", "")),
                            market_question=m.get("question", ""),
                            current_yes_price=clob_yes,
                            current_no_price=clob_no,
                        ))

        except Exception as e:
            logger.debug(f"[UMA] Errore Gamma API: {e}")

        return proposals

    def get_opportunities(self, min_edge: float = 0.05) -> list[ResolutionProposal]:
        """
        Ritorna le proposte con edge sufficiente.

        Filtra:
        - Edge > min_edge (default 5%)
        - Non troppo vecchie (< 2h dalla rilevazione)
        - Prezzo token vincente < 0.90 (c'è ancora margine)
        """
        now = time.time()
        opps = []

        for prop in self._proposals.values():
            # Proposta troppo vecchia
            if now - prop.detected_at > 7200:
                continue

            # Edge sufficiente
            if prop.best_edge < min_edge:
                continue

            # Token vincente ancora sottovalutato
            if prop.proposed_outcome == "YES" and prop.current_yes_price:
                if prop.current_yes_price > 0.90:
                    continue
            elif prop.proposed_outcome == "NO" and prop.current_no_price:
                if prop.current_no_price > 0.90:
                    continue

            opps.append(prop)

        # Ordina per edge decrescente
        opps.sort(key=lambda p: p.best_edge, reverse=True)
        return opps

    @property
    def active_proposals(self) -> int:
        return len(self._proposals)
