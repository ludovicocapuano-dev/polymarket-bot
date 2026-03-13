"""
Cross-Platform Arbitrage Scanner — trova opportunita' di arbitraggio
tra Polymarket e Kalshi (e altre exchange supportate da PMXT).

Se lo stesso evento ha prezzi diversi su due piattaforme, c'e' un'opportunita'
di arbitraggio: comprare il lato cheap su una e vendere (o comprare il complemento)
sull'altra.

Esempio:
    Polymarket: "Trump wins 2028?" YES @ $0.55
    Kalshi:     "Trump wins 2028?" YES @ $0.62
    → Edge = $0.07 = 12.7% (compra YES su Polymarket, vendi YES su Kalshi)

Limitazioni:
    - Non e' un arb risk-free: le posizioni sono su piattaforme diverse, quindi
      il settlement risk e la liquidita' vanno considerate.
    - Kalshi richiede API key per trading (qui usiamo solo lettura).
    - Il matching e' fuzzy (basato su keyword overlap nel titolo del mercato).

Uso:
    from utils.cross_platform_scanner import CrossPlatformScanner
    scanner = CrossPlatformScanner(polymarket_api=api)
    scanner.connect()
    opps = scanner.scan_cross_platform_arb()
    for opp in opps:
        print(f"{opp['question']} | edge={opp['edge']:.1%} | {opp['direction']}")
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    """Un'opportunita' di arbitraggio cross-platform."""
    question: str
    polymarket_question: str
    kalshi_question: str
    polymarket_yes_price: float
    kalshi_yes_price: float
    polymarket_no_price: float
    kalshi_no_price: float
    edge: float  # differenza di prezzo assoluta
    edge_pct: float  # edge come percentuale
    direction: str  # "BUY_YES_POLY" o "BUY_YES_KALSHI" etc.
    polymarket_id: str
    kalshi_id: str
    polymarket_volume: float
    kalshi_volume: float
    similarity_score: float  # 0-1, quanto sono simili i mercati
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "polymarket_question": self.polymarket_question,
            "kalshi_question": self.kalshi_question,
            "pm_yes": self.polymarket_yes_price,
            "kalshi_yes": self.kalshi_yes_price,
            "pm_no": self.polymarket_no_price,
            "kalshi_no": self.kalshi_no_price,
            "edge": self.edge,
            "edge_pct": self.edge_pct,
            "direction": self.direction,
            "pm_id": self.polymarket_id,
            "kalshi_id": self.kalshi_id,
            "pm_volume": self.polymarket_volume,
            "kalshi_volume": self.kalshi_volume,
            "similarity": self.similarity_score,
            "timestamp": self.timestamp,
        }


class CrossPlatformScanner:
    """
    Scanner per arbitraggio cross-platform Polymarket <-> Kalshi.

    Fasi:
    1. Fetch mercati da entrambe le piattaforme
    2. Match mercati simili tramite fuzzy title matching
    3. Confronta prezzi e calcola edge
    4. Filtra per edge minimo e volume minimo
    """

    # Parametri di matching
    MIN_SIMILARITY = 0.50  # soglia minima per considerare due mercati "uguali"
    MIN_EDGE = 0.03  # 3 centesimi minimo di edge
    MIN_EDGE_PCT = 0.04  # 4% minimo
    MIN_VOLUME = 1000  # $1K volume minimo per lato

    # Parole da ignorare nel matching (stopwords)
    STOPWORDS = {
        "will", "the", "be", "a", "an", "in", "on", "at", "to", "of",
        "by", "for", "or", "and", "is", "it", "this", "that", "what",
        "which", "who", "how", "when", "where", "than", "before", "after",
        "above", "below", "between", "during", "through", "market",
    }

    def __init__(
        self,
        polymarket_api=None,
        pmxt_polymarket=None,
        min_edge: float = 0.03,
        min_edge_pct: float = 0.04,
        min_volume: float = 1000,
    ):
        """
        Args:
            polymarket_api: istanza di PolymarketAPI (per fetch Polymarket)
            pmxt_polymarket: istanza di PMXTClient (alternativa a polymarket_api)
            min_edge: edge minimo in dollari per segnalare opportunita'
            min_edge_pct: edge minimo percentuale
            min_volume: volume minimo per mercato ($)
        """
        self._pm_api = polymarket_api
        self._pmxt_pm = pmxt_polymarket
        self._kalshi = None  # PMXTKalshiClient, creato in connect()
        self._connected = False

        self.MIN_EDGE = min_edge
        self.MIN_EDGE_PCT = min_edge_pct
        self.MIN_VOLUME = min_volume

        # Cache
        self._pm_markets: list[dict] = []
        self._kalshi_markets: list[dict] = []
        self._last_scan: float = 0
        self._cache_ttl: float = 300  # 5 minuti

    def connect(self) -> bool:
        """Connetti a Kalshi via PMXT per scanning."""
        try:
            from utils.pmxt_client import PMXTKalshiClient
            self._kalshi = PMXTKalshiClient(demo=False)
            ok = self._kalshi.connect()
            if ok:
                self._connected = True
                logger.info("[CROSS-PLATFORM] Scanner connesso a Kalshi via PMXT")
            else:
                logger.warning("[CROSS-PLATFORM] Connessione Kalshi fallita — scanner disabilitato")
            return ok
        except Exception as e:
            logger.error(f"[CROSS-PLATFORM] Errore connessione: {e}")
            return False

    def _fetch_polymarket_markets(self) -> list[dict]:
        """Fetch mercati Polymarket dalla fonte disponibile."""
        if self._pmxt_pm:
            return self._pmxt_pm.get_markets(limit=200)
        elif self._pm_api:
            markets = self._pm_api.fetch_markets(limit=200)
            from utils.pmxt_client import PMXTClient
            return [PMXTClient._market_obj_to_dict(m) for m in markets]
        else:
            logger.warning("[CROSS-PLATFORM] Nessun client Polymarket configurato")
            return []

    def _fetch_kalshi_markets(self) -> list[dict]:
        """Fetch mercati Kalshi."""
        if not self._kalshi or not self._kalshi.is_connected:
            return []
        return self._kalshi.get_markets(limit=200)

    # ── Fuzzy Matching ───────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenizza un titolo in parole normalizzate."""
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        words = text.split()
        return {
            w for w in words
            if w not in CrossPlatformScanner.STOPWORDS and len(w) > 1
        }

    @classmethod
    def _similarity(cls, title_a: str, title_b: str) -> float:
        """
        Calcola similarita' Jaccard tra due titoli di mercato.
        Ritorna 0-1 (1 = identici).
        """
        tokens_a = cls._tokenize(title_a)
        tokens_b = cls._tokenize(title_b)

        if not tokens_a or not tokens_b:
            return 0.0

        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b

        if not union:
            return 0.0

        jaccard = len(intersection) / len(union)

        # Bonus per overlap di entita' (numeri, nomi propri)
        # I numeri sono importanti (es. "2028", "$100K", "60F")
        nums_a = {w for w in tokens_a if any(c.isdigit() for c in w)}
        nums_b = {w for w in tokens_b if any(c.isdigit() for c in w)}
        if nums_a and nums_b:
            num_overlap = len(nums_a & nums_b) / max(len(nums_a | nums_b), 1)
            # Se i numeri non matchano, penalizza pesantemente
            if nums_a & nums_b:
                jaccard = jaccard * 0.7 + num_overlap * 0.3
            else:
                jaccard *= 0.3  # numeri diversi = probabilmente mercato diverso

        return jaccard

    def _find_matches(
        self,
        pm_markets: list[dict],
        kalshi_markets: list[dict],
    ) -> list[tuple[dict, dict, float]]:
        """
        Trova coppie di mercati simili tra Polymarket e Kalshi.
        Ritorna lista di (pm_market, kalshi_market, similarity_score).
        """
        matches = []

        for pm in pm_markets:
            pm_q = pm.get("question", "")
            if not pm_q:
                continue

            best_match = None
            best_score = 0.0

            for k in kalshi_markets:
                k_q = k.get("question", "")
                if not k_q:
                    continue

                score = self._similarity(pm_q, k_q)
                if score > best_score:
                    best_score = score
                    best_match = k

            if best_match and best_score >= self.MIN_SIMILARITY:
                matches.append((pm, best_match, best_score))

        return matches

    # ── Arbitrage Detection ──────────────────────────────────────

    def _analyze_arb(
        self,
        pm_market: dict,
        kalshi_market: dict,
        similarity: float,
    ) -> Optional[ArbOpportunity]:
        """
        Analizza una coppia di mercati per opportunita' di arbitraggio.
        Ritorna ArbOpportunity se edge sufficiente, None altrimenti.
        """
        pm_prices = pm_market.get("prices", {})
        k_prices = kalshi_market.get("prices", {})

        pm_yes = pm_prices.get("yes", 0.5)
        pm_no = pm_prices.get("no", 0.5)
        k_yes = k_prices.get("yes", 0.5)
        k_no = k_prices.get("no", 0.5)

        # Sanity check: prezzi validi
        if not (0 < pm_yes < 1 and 0 < k_yes < 1):
            return None

        # Volume check
        pm_vol = pm_market.get("volume", 0)
        k_vol = kalshi_market.get("volume", 0)
        if pm_vol < self.MIN_VOLUME and k_vol < self.MIN_VOLUME:
            return None

        # Calcola edge per entrambe le direzioni
        # Direzione 1: BUY YES su Polymarket (cheap), BUY NO su Kalshi (= sell YES)
        # Edge = kalshi_yes - polymarket_yes (positivo se PM e' piu' cheap)
        edge_buy_pm = k_yes - pm_yes

        # Direzione 2: BUY YES su Kalshi (cheap), BUY NO su Polymarket (= sell YES)
        edge_buy_k = pm_yes - k_yes

        # Prendi la direzione con edge positivo
        if edge_buy_pm > edge_buy_k:
            edge = edge_buy_pm
            direction = "BUY_YES_POLY_SELL_KALSHI"
            explanation = f"PM YES @${pm_yes:.2f} < Kalshi YES @${k_yes:.2f}"
        else:
            edge = edge_buy_k
            direction = "BUY_YES_KALSHI_SELL_POLY"
            explanation = f"Kalshi YES @${k_yes:.2f} < PM YES @${pm_yes:.2f}"

        # Edge percentuale (rispetto al prezzo di acquisto)
        buy_price = min(pm_yes, k_yes)
        edge_pct = edge / buy_price if buy_price > 0 else 0

        # Anche controllare arb sulla somma: se PM_YES + Kalshi_NO < 1.0 (o vice versa)
        # Questo e' un arb piu' robusto perche' copri entrambi i lati
        cross_sum_1 = pm_yes + k_no  # compra YES su PM + compra NO su Kalshi
        cross_sum_2 = k_yes + pm_no  # compra YES su Kalshi + compra NO su PM

        if cross_sum_1 < 1.0 - self.MIN_EDGE:
            edge = 1.0 - cross_sum_1
            edge_pct = edge  # gia' percentuale (base 1.0)
            direction = "BUY_YES_POLY_BUY_NO_KALSHI"
        elif cross_sum_2 < 1.0 - self.MIN_EDGE:
            edge = 1.0 - cross_sum_2
            edge_pct = edge
            direction = "BUY_YES_KALSHI_BUY_NO_POLY"

        # Filtra per edge minimo
        if edge < self.MIN_EDGE or edge_pct < self.MIN_EDGE_PCT:
            return None

        pm_q = pm_market.get("question", "")
        k_q = kalshi_market.get("question", "")

        return ArbOpportunity(
            question=pm_q,
            polymarket_question=pm_q,
            kalshi_question=k_q,
            polymarket_yes_price=pm_yes,
            kalshi_yes_price=k_yes,
            polymarket_no_price=pm_no,
            kalshi_no_price=k_no,
            edge=edge,
            edge_pct=edge_pct,
            direction=direction,
            polymarket_id=pm_market.get("id", ""),
            kalshi_id=kalshi_market.get("id", ""),
            polymarket_volume=pm_vol,
            kalshi_volume=k_vol,
            similarity_score=similarity,
            timestamp=time.time(),
        )

    # ── Public API ───────────────────────────────────────────────

    def scan_cross_platform_arb(self) -> list[dict]:
        """
        Scansiona Polymarket e Kalshi per opportunita' di arbitraggio.

        Ritorna lista di dict con:
            question, pm_yes, kalshi_yes, edge, edge_pct, direction,
            pm_id, kalshi_id, pm_volume, kalshi_volume, similarity, timestamp

        Ordinate per edge decrescente.
        """
        # Cache check
        if time.time() - self._last_scan < self._cache_ttl:
            logger.debug("[CROSS-PLATFORM] Cache valida, skip scan")
            # Riesegui comunque se cache vuota
            if self._pm_markets or self._kalshi_markets:
                pass  # usa cache sotto

        # Fetch mercati
        logger.info("[CROSS-PLATFORM] Scanning mercati Polymarket + Kalshi...")
        t0 = time.time()

        pm_markets = self._fetch_polymarket_markets()
        kalshi_markets = self._fetch_kalshi_markets()

        if not pm_markets:
            logger.warning("[CROSS-PLATFORM] Nessun mercato Polymarket fetchato")
            return []
        if not kalshi_markets:
            logger.warning("[CROSS-PLATFORM] Nessun mercato Kalshi fetchato — serve KALSHI_API_KEY?")
            return []

        self._pm_markets = pm_markets
        self._kalshi_markets = kalshi_markets

        # Match mercati simili
        matches = self._find_matches(pm_markets, kalshi_markets)
        logger.info(
            f"[CROSS-PLATFORM] {len(pm_markets)} PM + {len(kalshi_markets)} Kalshi "
            f"→ {len(matches)} matches (similarity >= {self.MIN_SIMILARITY})"
        )

        # Analizza arb per ogni coppia
        opportunities = []
        for pm, k, sim in matches:
            opp = self._analyze_arb(pm, k, sim)
            if opp:
                opportunities.append(opp)

        # Ordina per edge decrescente
        opportunities.sort(key=lambda x: x.edge, reverse=True)

        elapsed = time.time() - t0
        self._last_scan = time.time()

        if opportunities:
            logger.info(
                f"[CROSS-PLATFORM] {len(opportunities)} opportunita' trovate "
                f"(top edge: {opportunities[0].edge:.1%}) in {elapsed:.1f}s"
            )
            for opp in opportunities[:5]:
                logger.info(
                    f"  → {opp.question[:60]}... | edge={opp.edge:.2f} ({opp.edge_pct:.1%}) "
                    f"| PM YES=${opp.polymarket_yes_price:.2f} vs K YES=${opp.kalshi_yes_price:.2f} "
                    f"| {opp.direction} | sim={opp.similarity_score:.2f}"
                )
        else:
            logger.info(
                f"[CROSS-PLATFORM] 0 opportunita' (edge min ${self.MIN_EDGE}, "
                f"{self.MIN_EDGE_PCT:.0%}) in {elapsed:.1f}s"
            )

        return [opp.to_dict() for opp in opportunities]

    def close(self):
        """Chiudi connessioni."""
        if self._kalshi:
            self._kalshi.close()
        self._connected = False
