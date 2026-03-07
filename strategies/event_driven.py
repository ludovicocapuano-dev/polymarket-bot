"""
Strategia 3: Event-Driven Trading v3 — News-Reactive + Panic Fade
===================================================================
v10.8: Aggiunto segnale PANIC FADE (liquidity vacuum trade).

Segnali attivi:
1. Glint-Reactive: segnali pre-matchati da Glint.trade (se disponibile)
2. News-Reactive: breaking news + market underreaction (5-30 min window)
3. Structural: overconfidence + political mispricing + news conferma
4. PANIC FADE (v10.8): mean reversion su mercati con prezzo estremo
   Logica: quando il prezzo si muove a estremi (YES>0.82 o YES<0.18)
   con strong breaking news, il mercato sta over-reacting.
   Trade CONTRO la folla: il panico rientra, il prezzo torna alla media.
   Ispirato a: "liquidity vacuum trade" (contrarian mean reversion).

Fonti:
- Research: prediction markets overreact 70-80% del tempo su breaking news
- Top 668 wallet Polymarket: event-driven ROI 1025%
- Mercati non-crypto sono FEE-FREE → min_edge puo' essere basso
"""

import logging
import random
import time
from dataclasses import dataclass

from utils.polymarket_api import Market, PolymarketAPI
from utils.finlight_feed import FinlightFeed, NewsSentiment
from utils.gdelt_feed import GDELTFeed
from utils.glint_feed import GlintFeed
from utils.twitter_feed import TwitterFeed
from utils.risk_manager import RiskManager, Trade

logger = logging.getLogger(__name__)

STRATEGY_NAME = "event_driven"

# ── Filtro prezzo ──
MIN_TOKEN_PRICE = 0.05
MAX_TOKEN_PRICE = 0.95
MAX_PAYOFF_MULT = 20.0

# Keywords per eventi ad alto impatto
# v8.0: Config per-categoria basata su Becker Dataset
CATEGORY_CONFIG = {
    "political": {
        "min_edge": 0.02,           # Ridotto da 0.03 — Becker: politics molto profittevole
        "confidence_boost": 0.10,   # Piu' fiducia nei mercati politici
    },
    "crypto_regulatory": {
        "min_edge": 0.05,           # Aumentato — Becker: crypto ben calibrato, hard to beat
        "confidence_boost": 0.0,
    },
    "geopolitical": {
        "min_edge": 0.04,           # Standard
        "confidence_boost": 0.05,
    },
    "macro": {
        "min_edge": 0.03,           # Standard
        "confidence_boost": 0.0,
    },
    "tech": {
        "min_edge": 0.04,
        "confidence_boost": 0.0,
    },
}

EVENT_KEYWORDS = {
    "macro": [
        "fed", "fomc", "rate", "interest", "cpi", "inflation",
        "gdp", "jobs", "employment", "unemployment", "nonfarm",
        "bls", "treasury", "tariff", "recession", "debt", "deficit",
        "stimulus", "shutdown", "default", "bond", "yield",
    ],
    "crypto_regulatory": [
        "etf", "sec", "approval", "regulation", "ban",
        "stablecoin", "cbdc", "gensler", "cftc", "defi",
        "exchange", "binance", "coinbase", "custody", "token",
    ],
    "political": [
        "election", "president", "congress", "senate",
        "vote", "poll", "nominee", "primary", "governor",
        "supreme court", "impeach", "trump", "biden",
        "democrat", "republican", "party", "legislation",
    ],
    "tech": [
        "earnings", "revenue", "ipo", "acquisition",
        "nvidia", "apple", "google", "microsoft", "ai",
        "openai", "meta", "tesla", "amazon", "chip",
        "semiconductor", "antitrust", "layoff",
    ],
    "geopolitical": [
        "war", "ukraine", "russia", "china", "taiwan",
        "nato", "sanction", "missile", "ceasefire", "peace",
        "iran", "israel", "oil", "opec",
    ],
}


@dataclass
class EventOpportunity:
    market: Market
    event_type: str
    edge: float
    side: str
    confidence: float
    reasoning: str
    signal_type: str = "structural"   # "news_reactive", "overconfidence", "structural"
    news_sentiment: float = 0.0
    news_volume: str = "NONE"
    news_label: str = "NEUTRAL"
    news_strength: float = 0.0        # 0.0-1.0 forza complessiva delle news


class EventDrivenStrategy:
    """
    v5.9.3: News-first event-driven strategy.

    Priorita' segnali (dal piu' forte al piu' debole):
    1. NEWS-REACTIVE: Finlight rileva breaking news → mercato non ha reagito
       Edge: 3-15%. Win rate atteso: 60-70%. Questo e' il segnale primario.
    2. OVERCONFIDENCE + NEWS: Prezzo estremo + news contraddicono consenso
       Edge: 3-8%. Win rate atteso: 55-65%.
    3. STRUCTURAL: Mercati politici/geo con mispricing YES+NO != 1.0
       Edge: 3-5%. Win rate atteso: 52-58%.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        risk: RiskManager,
        finlight: FinlightFeed | None = None,
        gdelt: GDELTFeed | None = None,
        glint: GlintFeed | None = None,
        twitter: TwitterFeed | None = None,
        min_edge: float = 0.03,
    ):
        self.api = api
        self.risk = risk
        self.finlight = finlight
        self.gdelt = gdelt
        self.glint = glint
        self.twitter = twitter
        self.min_edge = min_edge
        self._trades_executed = 0
        self._recently_traded: dict[str, float] = {}
        self._TRADE_COOLDOWN = 300
        # Traccia le breaking news per non ri-reagire
        self._last_breaking_check: float = 0.0
        self._breaking_cache: list[tuple[str, object]] = []

    async def scan(self, shared_markets: list[Market] | None = None) -> list[EventOpportunity]:
        """
        Scansiona mercati per opportunita' event-driven.
        v10.5: Step 0 Glint-reactive, poi news-reactive, poi strutturali.
        """
        opportunities = []
        markets = shared_markets or self.api.fetch_markets(limit=200)

        if not markets:
            logger.info("[EVENT] Scan: 0 mercati disponibili")
            return []

        # ── 0. GLINT-REACTIVE: segnali pre-matchati da Glint.trade ──
        glint_opps = self._check_glint_opportunities(markets)
        opportunities.extend(glint_opps)
        glint_market_ids = {o.market.id for o in glint_opps}

        # ── 1. NEWS-REACTIVE: controlla breaking news (priorita' massima) ──
        news_opps = self._check_news_reactive(markets)
        # Deduplica: escludi mercati già coperti da Glint
        news_opps = [o for o in news_opps if o.market.id not in glint_market_ids]
        opportunities.extend(news_opps)

        # ── 2.5 PANIC FADE: mean reversion su prezzi estremi ──
        covered_ids = glint_market_ids | {o.market.id for o in news_opps}
        panic_opps = self._check_panic_fade(markets, covered_ids)
        opportunities.extend(panic_opps)
        covered_ids.update(o.market.id for o in panic_opps)

        # ── 3. Segnali strutturali per mercati classificati ──
        classified = 0
        for m in markets:
            event_type = self._classify_event(m)
            if not event_type:
                continue
            classified += 1

            opp = self._evaluate_structural(m, event_type)
            if opp:
                # Evita duplicati
                if m.id not in covered_ids:
                    opportunities.append(opp)

        # Ordina per edge * confidence (expected value)
        opportunities.sort(key=lambda o: o.edge * o.confidence, reverse=True)

        if opportunities:
            by_type = {}
            for o in opportunities:
                by_type[o.signal_type] = by_type.get(o.signal_type, 0) + 1
            type_str = " ".join(f"{k}:{v}" for k, v in by_type.items())
            logger.info(
                f"[EVENT] Scan {len(markets)} mercati ({classified} classificati) → "
                f"{len(opportunities)} opportunita' [{type_str}] "
                f"migliore: edge={opportunities[0].edge:.4f} "
                f"tipo={opportunities[0].signal_type}"
            )
        else:
            logger.info(
                f"[EVENT] Scan {len(markets)} mercati ({classified} classificati) → "
                f"0 opportunita'"
            )

        return opportunities

    # ── SEGNALE 2.5: PANIC FADE (liquidity vacuum / mean reversion) ──

    def _check_panic_fade(
        self, markets: list[Market], exclude_ids: set[str]
    ) -> list[EventOpportunity]:
        """
        v10.8: Panic Fade — trade contrarian su mercati con prezzo estremo.

        Logica: quando il prezzo raggiunge estremi (YES>0.82 o YES<0.18)
        su mercati con volume e breaking news, il mercato sta over-reacting.
        La maggioranza dei retail chiude in perdita, noi prendiamo il lato opposto.

        Condizioni di ingresso:
        1. Prezzo estremo (YES > 0.82 o YES < 0.18)
        2. Volume significativo (> $50K) — mercato attivo, non illiquido
        3. Breaking news nella stessa categoria — conferma che il move ha causa
        4. Classificabile come evento (political/geo/macro/tech/crypto)

        Edge = proporzionale a quanto estremo e' il prezzo.
        Trade CONTRO il consenso: se YES>0.82 → BUY NO (fade the panic).
        """
        if not markets:
            return []

        # Raccogli breaking news attive (una volta per scan)
        breaking_categories: set[str] = set()
        breaking_by_cat: dict[str, float] = {}  # category → |sentiment|

        # Twitter breaking
        if self.twitter:
            try:
                for cat, ns in self.twitter.detect_breaking_news(
                    min_articles=1, min_sentiment=0.15
                ):
                    breaking_categories.add(cat)
                    breaking_by_cat[cat] = max(
                        breaking_by_cat.get(cat, 0), abs(ns.sentiment)
                    )
            except Exception:
                pass

        # GDELT breaking
        if self.gdelt:
            try:
                for cat, ns in self.gdelt.detect_breaking_events(
                    min_articles=2, min_sentiment=0.20
                ):
                    breaking_categories.add(cat)
                    breaking_by_cat[cat] = max(
                        breaking_by_cat.get(cat, 0), abs(ns.sentiment)
                    )
            except Exception:
                pass

        # Glint breaking
        if self.glint and self.glint.available:
            try:
                for cat, avg_sent, n_signals in self.glint.detect_breaking_news(
                    min_articles=1, min_sentiment=0.15
                ):
                    breaking_categories.add(cat)
                    breaking_by_cat[cat] = max(
                        breaking_by_cat.get(cat, 0), abs(avg_sent)
                    )
            except Exception:
                pass

        if not breaking_categories:
            return []

        opportunities: list[EventOpportunity] = []

        for m in markets:
            if m.id in exclude_ids:
                continue

            # Cooldown
            if time.time() - self._recently_traded.get(m.id, 0) < 120:
                continue

            # Classifica evento
            event_type = self._classify_event(m)
            if not event_type:
                continue

            # Il mercato deve avere breaking news nella sua categoria
            if event_type not in breaking_categories:
                continue

            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            # Volume minimo: mercato attivo (non illiquido)
            if m.volume < 50000:
                continue

            # ── Condizione core: prezzo estremo ──
            side = ""
            edge = 0.0
            confidence = 0.0

            if p_yes > 0.82:
                # Mercato troppo bullish → fade, BUY NO
                # Edge: quanto più estremo il prezzo, più forte il mean reversion
                # YES@0.82 → edge 3%, YES@0.90 → edge 7%, YES@0.95 → edge 10%
                overshoot = p_yes - 0.75  # distanza dal "fair range"
                edge = min(overshoot * 0.50, 0.12)  # cap a 12%
                side = "NO"
                # Confidence: più alta se volume alto + strong sentiment
                confidence = 0.55
                if m.volume > 200000:
                    confidence += 0.05
                news_strength = breaking_by_cat.get(event_type, 0)
                if news_strength > 0.40:
                    confidence += 0.05

            elif p_yes < 0.18:
                # Mercato troppo bearish → fade, BUY YES
                overshoot = 0.25 - p_yes  # distanza dal "fair range"
                edge = min(overshoot * 0.50, 0.12)
                side = "YES"
                confidence = 0.55
                if m.volume > 200000:
                    confidence += 0.05
                news_strength = breaking_by_cat.get(event_type, 0)
                if news_strength > 0.40:
                    confidence += 0.05

            if not side or edge < 0.03:
                continue

            # Filtro prezzo token
            buy_price = p_no if side == "NO" else p_yes
            if buy_price < MIN_TOKEN_PRICE or buy_price > MAX_TOKEN_PRICE:
                continue

            # Filtro payoff: non comprare token troppo caro (>0.70)
            if buy_price > 0.70:
                continue

            confidence = min(confidence, 0.70)  # cap — mean reversion non e' garantito

            news_sent_val = breaking_by_cat.get(event_type, 0)
            # Sentiment opposto al side (stiamo fading)
            if side == "NO":
                news_sent_val = -abs(news_sent_val)
            else:
                news_sent_val = abs(news_sent_val)

            opportunities.append(EventOpportunity(
                market=m,
                event_type=event_type,
                edge=edge,
                side=side,
                confidence=confidence,
                reasoning=(
                    f"PANIC FADE: YES@{p_yes:.3f} (extreme) "
                    f"vol=${m.volume:,.0f} "
                    f"breaking={event_type} "
                    f"→ BUY {side} (mean reversion)"
                ),
                signal_type="panic_fade",
                news_sentiment=news_sent_val,
                news_volume="HIGH",
                news_label="CONTRARIAN",
                news_strength=breaking_by_cat.get(event_type, 0),
            ))

        if opportunities:
            logger.info(
                f"[EVENT] Panic fade: {len(opportunities)} opportunita' "
                f"(breaking in: {', '.join(breaking_categories)})"
            )

        return opportunities

    # ── SEGNALE 0: GLINT-REACTIVE (pre-matchato) ─────────────────

    def _check_glint_opportunities(self, markets: list[Market]) -> list[EventOpportunity]:
        """
        v10.5: Drain segnali Glint e cross-ref con shared_markets.
        Glint fornisce market match pre-computed (condition_id/slug),
        noi usiamo i NOSTRI prezzi per calcolare edge.
        """
        if not self.glint or not self.glint.available:
            return []

        glint_opps = self.glint.drain_opportunities()
        if not glint_opps:
            return []

        signals = []

        # Indici per cross-ref veloce
        by_condition = {}
        by_slug = {}
        for m in markets:
            cid = getattr(m, "condition_id", "")
            if cid:
                by_condition[cid] = m
            slug = getattr(m, "slug", "")
            if slug:
                by_slug[slug] = m

        for gopp in glint_opps:
            match = gopp.market_match
            # Cross-ref: trova il mercato nei nostri shared_markets
            m = by_condition.get(match.condition_id) or by_slug.get(match.slug)
            if not m:
                # Fallback: match per question substring
                q_lower = match.question.lower()
                if q_lower:
                    for mkt in markets:
                        if q_lower[:30] in mkt.question.lower():
                            m = mkt
                            break
            if not m:
                continue

            p_yes = m.prices.get("yes", 0.5)
            p_no = m.prices.get("no", 0.5)

            # Filtro prezzo
            if p_yes < MIN_TOKEN_PRICE or p_yes > MAX_TOKEN_PRICE:
                continue
            if p_no < MIN_TOKEN_PRICE or p_no > MAX_TOKEN_PRICE:
                continue

            sent = gopp.inferred_sentiment
            if abs(sent) < 0.10:
                continue

            # Determina side e calcola edge
            event_type = gopp.event_type or self._classify_event(m)
            news_str = self.glint.get_news_strength(event_type) if event_type else 0.5

            if sent > 0.15 and p_yes < 0.85:
                side = "YES"
                price_discount = max(0, 0.85 - p_yes) / 0.85
                edge = news_str * 0.15 * (0.5 + 0.5 * price_discount)
            elif sent < -0.15 and p_no < 0.85:
                side = "NO"
                price_discount = max(0, 0.85 - p_no) / 0.85
                edge = news_str * 0.15 * (0.5 + 0.5 * price_discount)
            else:
                continue

            # min_edge per-categoria
            cat_cfg = CATEGORY_CONFIG.get(
                event_type, {"min_edge": self.min_edge, "confidence_boost": 0.0}
            )
            if edge < cat_cfg["min_edge"]:
                continue

            # Confidence: base 0.58 + relevance boost + impact boost
            confidence = 0.58
            # Relevance boost: score 7→+0.03, 8→+0.06, 9→+0.09, 10→+0.12
            if match.relevance_score >= 7:
                confidence += (match.relevance_score - 7) * 0.03
            # Impact boost
            if gopp.signal.impact_level == "high":
                confidence += 0.08
            elif gopp.signal.impact_level == "medium":
                confidence += 0.04
            # Per-category boost
            confidence += cat_cfg.get("confidence_boost", 0.0)
            confidence = min(confidence, 0.90)

            signals.append(EventOpportunity(
                market=m,
                event_type=event_type,
                edge=edge,
                side=side,
                confidence=confidence,
                reasoning=(
                    f"GLINT-REACTIVE: '{gopp.signal.title[:35]}' "
                    f"rel={match.relevance_score:.0f} "
                    f"impact={gopp.signal.impact_level} "
                    f"sent={sent:+.2f} | "
                    f"{side}@{p_yes if side == 'YES' else p_no:.3f}"
                ),
                signal_type="glint_reactive",
                news_sentiment=sent,
                news_volume="HIGH" if len(glint_opps) >= 5 else "MODERATE" if len(glint_opps) >= 2 else "LOW",
                news_label="BULLISH" if sent > 0.3 else "BEARISH" if sent < -0.3 else "NEUTRAL",
                news_strength=news_str,
            ))

        if signals:
            logger.info(
                f"[EVENT] Glint-reactive: {len(signals)} opportunita' "
                f"da {len(glint_opps)} segnali Glint"
            )

        return signals

    # ── SEGNALE 1: NEWS-REACTIVE (primario) ──────────────────────

    def _check_news_reactive(self, markets: list[Market]) -> list[EventOpportunity]:
        """
        Segnale piu' forte: rileva breaking news e trova mercati
        il cui prezzo non ha ancora reagito.

        Pattern "underreaction": dopo una news importante, i prediction
        markets impiegano 5-30 minuti per assorbire l'informazione.
        Comprando nel primo minuto dopo la detection, catturiamo l'edge.

        Logica:
        1. Finlight rileva news con alto volume + sentiment forte
        2. Per ogni categoria con breaking news, cerca mercati correlati
        3. Se il prezzo del mercato non riflette il sentiment → compra

        Esempio: Finlight rileva 8 articoli "FED holds rates" sentiment +0.6
        → cerca mercati con keyword "fed"/"fomc"/"rate" dove YES < 0.70
        → compra YES perche' la news suggerisce outcome positivo
        """
        if not self.finlight and not self.gdelt:
            return []

        signals = []
        now = time.time()

        # Controlla breaking news ogni 60 secondi (non ad ogni scan)
        if now - self._last_breaking_check < 60:
            breaking = self._breaking_cache
        else:
            breaking = self._merge_breaking_news()
            # Pulizia cache: tieni solo ultimi 60 secondi
            self._breaking_cache = [
                (et, ns) for et, ns in breaking
                if ns.fetched_at > now - 60
            ]
            breaking = self._breaking_cache
            self._last_breaking_check = now

        if not breaking:
            return []

        for event_type, ns in breaking:
            # Trova mercati correlati a questa categoria
            for m in markets:
                mtype = self._classify_event(m)
                if mtype != event_type:
                    continue

                # v7.0: Verifica sentiment SPECIFICO per questo mercato.
                # Se non ci sono articoli specifici sul mercato, SKIP.
                # v8.1: Usa merge multi-fonte (Finlight + GDELT)
                mkt_ns = self._get_merged_market_sentiment(
                    m.question, event_type
                )
                if mkt_ns.n_articles < 2 or abs(mkt_ns.avg_sentiment) < 0.15:
                    # Nessun articolo specifico su questo mercato → SKIP
                    continue
                # Usa il sentiment SPECIFICO del mercato, non quello globale
                market_sent = mkt_ns.avg_sentiment
                # Se il sentiment specifico contraddice quello globale → SKIP
                global_sent = ns.avg_sentiment
                if (global_sent > 0.25 and market_sent < -0.10) or \
                   (global_sent < -0.25 and market_sent > 0.10):
                    continue
                market_specific_discount = 1.0

                p_yes = m.prices.get("yes", 0.5)
                p_no = m.prices.get("no", 0.5)

                # Filtro prezzo
                if p_yes < MIN_TOKEN_PRICE or p_yes > MAX_TOKEN_PRICE:
                    continue
                if p_no < MIN_TOKEN_PRICE or p_no > MAX_TOKEN_PRICE:
                    continue

                # Determina direzione dalla news
                sent = ns.avg_sentiment
                hc_sent = ns.high_confidence_sentiment
                # Usa high-confidence se disponibile e piu' forte
                if abs(hc_sent) > abs(sent):
                    sent = hc_sent

                # News positiva → il mercato dovrebbe salire (YES)
                # News negativa → il mercato dovrebbe scendere (NO)
                if sent > 0.25 and p_yes < 0.80:
                    # News bullish ma prezzo YES ancora basso → underreaction
                    side = "YES"
                    # Edge = quanto il prezzo DOVREBBE salire in base al sentiment
                    # Conservativo: news strength * 10-15% max
                    news_str = self._get_merged_news_strength(event_type)
                    edge = news_str * 0.12  # max ~12% edge
                    # Aggiusta per quanto il prezzo e' gia' alto
                    # Se YES e' gia' a 0.75, l'edge residuo e' minore
                    price_discount = max(0, 0.80 - p_yes) / 0.80
                    edge = edge * (0.5 + 0.5 * price_discount)
                    # Ridurre edge se news non specifiche per questo mercato
                    edge *= market_specific_discount

                elif sent < -0.25 and p_no < 0.80:
                    # News bearish ma prezzo NO ancora basso → underreaction
                    side = "NO"
                    news_str = self._get_merged_news_strength(event_type)
                    edge = news_str * 0.12
                    price_discount = max(0, 0.80 - p_no) / 0.80
                    edge = edge * (0.5 + 0.5 * price_discount)
                    # Ridurre edge se news non specifiche per questo mercato
                    edge *= market_specific_discount

                else:
                    continue

                # v8.0: min_edge per-categoria (Becker Dataset)
                cat_cfg = CATEGORY_CONFIG.get(event_type, {"min_edge": self.min_edge, "confidence_boost": 0.0})
                if edge < cat_cfg["min_edge"]:
                    continue

                # Confidence basata su forza news + volume articoli
                confidence = 0.55  # base per news-reactive
                if ns.n_articles >= 10:
                    confidence += 0.10
                elif ns.n_articles >= 5:
                    confidence += 0.05
                if abs(sent) > 0.5:
                    confidence += 0.08
                if abs(hc_sent) > 0.5:
                    confidence += 0.05
                confidence = min(confidence, 0.82)
                # v8.0: confidence boost per-categoria
                confidence += cat_cfg.get("confidence_boost", 0.0)
                confidence = min(confidence, 0.90)

                signals.append(EventOpportunity(
                    market=m,
                    event_type=event_type,
                    edge=edge,
                    side=side,
                    confidence=confidence,
                    reasoning=(
                        f"NEWS-REACTIVE: {ns.sentiment_label} "
                        f"sent={sent:+.2f} n={ns.n_articles} "
                        f"str={news_str:.2f} | "
                        f"{side}@{p_yes if side == 'YES' else p_no:.3f} "
                        f"'{m.question[:35]}'"
                    ),
                    signal_type="news_reactive",
                    news_sentiment=sent,
                    news_volume=ns.news_volume,
                    news_label=ns.sentiment_label,
                    news_strength=news_str,
                ))

        return signals

    # ── SEGNALE 2 & 3: STRUTTURALI ──────────────────────────────

    def _evaluate_structural(
        self, market: Market, event_type: str
    ) -> EventOpportunity | None:
        """
        Segnali strutturali (v5.9.3):
        - Overconfidence reversal: ORA richiede conferma news
        - Political/geo mispricing: YES+NO != 1.0 con news volume alto
        """
        price_yes = market.prices.get("yes", 0.5)
        price_no = market.prices.get("no", 0.5)

        if price_yes < MIN_TOKEN_PRICE or price_yes > MAX_TOKEN_PRICE:
            return None
        if price_no < MIN_TOKEN_PRICE or price_no > MAX_TOKEN_PRICE:
            return None

        edge = 0.0
        side = ""
        confidence = 0.0
        reasoning = ""
        signal_type = "structural"
        news_sent_val = 0.0
        news_vol = "NONE"
        news_lbl = "NEUTRAL"
        news_str = 0.0

        # ── Fetch news per questo evento (multi-fonte) ──
        has_news = False
        event_ns = NewsSentiment(event_type=event_type)

        # 1. Prova Finlight prima (piu' preciso)
        if self.finlight:
            event_ns = self.finlight.get_event_sentiment(event_type)
            if event_ns.n_articles < 3:
                market_ns = self.finlight.get_market_sentiment(
                    market.question, event_type
                )
                if market_ns.n_articles > event_ns.n_articles:
                    event_ns = market_ns

        # 2. Se Finlight ha < 3 articoli, prova GDELT come fallback
        # v9.2.2: Solo query per-categoria (cachata). Query per-mercato solo se feed healthy
        # per evitare che 168 mercati × 10s = 28 min blocchino il ciclo
        if event_ns.n_articles < 3 and self.gdelt:
            gdelt_ns = self.gdelt.get_event_sentiment(event_type)
            if gdelt_ns.n_articles < 3 and self.gdelt.is_healthy:
                gdelt_mkt_ns = self.gdelt.get_market_sentiment(
                    market.question, event_type
                )
                if gdelt_mkt_ns.n_articles > gdelt_ns.n_articles:
                    gdelt_ns = gdelt_mkt_ns
            # 3. Se GDELT ha significativamente piu' dati (>2 articoli in piu'), usalo
            if gdelt_ns.n_articles > event_ns.n_articles + 2:
                event_ns = gdelt_ns

        if event_ns.is_fresh and event_ns.n_articles > 0:
            has_news = True
            news_sent_val = event_ns.avg_sentiment
            news_vol = event_ns.news_volume
            news_lbl = event_ns.sentiment_label
            news_str = self._get_merged_news_strength(event_type)

            hc_sent = event_ns.high_confidence_sentiment
            if abs(hc_sent) > abs(news_sent_val):
                news_sent_val = hc_sent

        # --- Segnale: Overconfidence reversal + NEWS ---
        # v5.9.3: ORA richiede che le news contraddicano il prezzo estremo.
        # Prima scommetteva alla cieca contro il consenso → troppo rischioso.
        # Con news conferma, il win rate sale significativamente.
        if price_yes > 0.85 and market.volume < 150000:
            # YES molto alto. Contrarian solo se news dicono il contrario.
            if has_news and news_sent_val < -0.15:
                # News bearish + prezzo YES troppo alto → contrarian confermato
                # Fair value dinamico: non scommettere contro mercati con YES > 0.90 senza news fortemente contrarie
                fair_value = min(price_yes, 0.90)
                regression_edge = (price_yes - fair_value) * 0.40
                news_boost = min(abs(news_sent_val) * 0.03, 0.05)
                regression_edge += news_boost
                if regression_edge > edge:
                    edge = regression_edge
                    side = "NO"
                    confidence = 0.60 if price_yes > 0.92 else 0.55
                    if news_vol == "HIGH":
                        confidence += 0.05
                    signal_type = "overconfidence"
                    reasoning = (
                        f"Overconfidence+News: YES@{price_yes:.3f} "
                        f"vol=${market.volume:,.0f} "
                        f"news={news_lbl}({news_sent_val:+.2f})"
                    )

        elif price_yes < 0.15 and market.volume < 150000:
            # YES molto basso. Contrarian solo se news sono bullish.
            if has_news and news_sent_val > 0.15:
                regression_edge = (0.20 - price_yes) * 0.40
                news_boost = min(abs(news_sent_val) * 0.03, 0.05)
                regression_edge += news_boost
                if regression_edge > edge:
                    edge = regression_edge
                    side = "YES"
                    confidence = 0.60 if price_yes < 0.08 else 0.55
                    if news_vol == "HIGH":
                        confidence += 0.05
                    signal_type = "overconfidence"
                    reasoning = (
                        f"Overconfidence+News: YES@{price_yes:.3f} "
                        f"vol=${market.volume:,.0f} "
                        f"news={news_lbl}({news_sent_val:+.2f})"
                    )

        # --- Segnale: Political/Geo con mispricing + news attive ---
        # v5.9.3: Richiede news volume > 0 per evitare falsi segnali
        # su mercati politici dormienti.
        if event_type in ("political", "geopolitical"):
            if 0.30 < price_yes < 0.70:
                total = price_yes + price_no
                if abs(total - 1.0) > 0.005:
                    pol_edge = abs(total - 1.0) * 0.55
                    if pol_edge > edge:
                        # Determina side in base a news se disponibili
                        if has_news and abs(news_sent_val) > 0.10:
                            side = "YES" if news_sent_val > 0 else "NO"
                            confidence = 0.55
                        else:
                            side = "YES" if total < 1.0 and price_yes < 0.5 else "NO"
                            confidence = 0.50

                        # Boost se c'e' volume di news
                        if has_news and news_vol in ("HIGH", "MODERATE"):
                            confidence += 0.05
                            pol_edge += 0.005

                        edge = pol_edge
                        signal_type = "structural"
                        reasoning = (
                            f"Political misprice: YES+NO={total:.4f} "
                            f"{side}@{price_yes:.3f}"
                        )
                        if has_news:
                            reasoning += f" news={news_lbl}({news_sent_val:+.2f})"

        # v8.0: min_edge per-categoria (Becker Dataset)
        cat_cfg = CATEGORY_CONFIG.get(event_type, {"min_edge": self.min_edge, "confidence_boost": 0.0})
        if edge < cat_cfg["min_edge"]:
            return None

        # v8.0: confidence boost per-categoria
        confidence += cat_cfg.get("confidence_boost", 0.0)
        confidence = min(confidence, 0.90)

        return EventOpportunity(
            market=market,
            event_type=event_type,
            edge=edge,
            side=side,
            confidence=confidence,
            reasoning=reasoning,
            signal_type=signal_type,
            news_sentiment=news_sent_val,
            news_volume=news_vol,
            news_label=news_lbl,
            news_strength=news_str,
        )

    # ── MERGE MULTI-FONTE (Finlight + GDELT) ─────────────────────

    def _merge_breaking_news(self) -> list[tuple[str, NewsSentiment]]:
        """
        Unifica breaking da Finlight + GDELT + Glint per categoria.
        Per ogni categoria: prende la fonte con segnale piu' forte
        (|sentiment| * n_articles). Se 2+ concordano (stesso segno
        sentiment), applica boost 10% alla news_strength.
        v10.5: Glint come terza fonte con bonus 1.2x e min_articles=1.
        """
        finlight_breaking: dict[str, NewsSentiment] = {}
        gdelt_breaking: dict[str, NewsSentiment] = {}
        glint_breaking: dict[str, tuple[float, int]] = {}  # event_type → (sentiment, n_signals)

        if self.finlight:
            for et, ns in self.finlight.detect_breaking_news(
                min_articles=3, min_sentiment=0.25
            ):
                finlight_breaking[et] = ns

        if self.gdelt:
            for et, ns in self.gdelt.detect_breaking_events(
                min_articles=3, min_sentiment=0.25
            ):
                gdelt_breaking[et] = ns

        # v10.5: Glint breaking (min_articles=1, più reattivo)
        if self.glint and self.glint.available:
            for et, avg_sent, n_signals in self.glint.detect_breaking_news(
                min_articles=1, min_sentiment=0.20
            ):
                glint_breaking[et] = (avg_sent, n_signals)

        # v10.6: Twitter breaking (4a fonte, NO bonus — VADER meno preciso)
        twitter_breaking: dict[str, NewsSentiment] = {}
        if self.twitter and self.twitter.is_healthy:
            for et, ns in self.twitter.detect_breaking_news(
                min_articles=3, min_sentiment=0.25
            ):
                twitter_breaking[et] = ns

        # Merge: per ogni categoria prendi la fonte con segnale piu' forte
        all_categories = (
            set(finlight_breaking) | set(gdelt_breaking)
            | set(glint_breaking) | set(twitter_breaking)
        )
        merged: list[tuple[str, NewsSentiment]] = []

        for cat in all_categories:
            f_ns = finlight_breaking.get(cat)
            g_ns = gdelt_breaking.get(cat)
            gl_data = glint_breaking.get(cat)
            t_ns = twitter_breaking.get(cat)

            # Raccogli tutti i candidati con il loro signal strength
            candidates: list[tuple[float, NewsSentiment]] = []
            if f_ns:
                candidates.append((abs(f_ns.avg_sentiment) * f_ns.n_articles, f_ns))
            if g_ns:
                candidates.append((abs(g_ns.avg_sentiment) * g_ns.n_articles, g_ns))
            # v10.6: Twitter come 4a fonte, NO bonus (1.0x — VADER meno preciso)
            if t_ns:
                candidates.append((abs(t_ns.avg_sentiment) * t_ns.n_articles, t_ns))
            if gl_data:
                gl_sent, gl_n = gl_data
                # Glint bonus 1.2x (segnali pre-matchati, più affidabili per relevance)
                gl_signal = abs(gl_sent) * gl_n * 1.2
                # Crea NewsSentiment sintetico per Glint
                gl_ns = NewsSentiment(event_type=cat, fetched_at=time.time())
                # Popola con articoli sintetici per compatibilità
                from utils.finlight_feed import NewsArticle
                for _ in range(gl_n):
                    gl_ns.articles.append(NewsArticle(
                        title="Glint signal",
                        sentiment="positive" if gl_sent > 0 else "negative",
                        confidence=min(abs(gl_sent), 0.9),
                    ))
                candidates.append((gl_signal, gl_ns))

            if not candidates:
                continue

            # Prendi la fonte con segnale più forte
            candidates.sort(key=lambda x: x[0], reverse=True)
            merged.append((cat, candidates[0][1]))

        merged.sort(
            key=lambda x: abs(x[1].avg_sentiment) * x[1].n_articles,
            reverse=True,
        )
        return merged

    def _get_merged_market_sentiment(
        self, question: str, event_type: str
    ) -> NewsSentiment:
        """
        Sentiment specifico per mercato dalle fonti disponibili.
        Prende la fonte con piu' articoli. A parita': Finlight (piu' preciso).
        Priorità: Finlight > GDELT > Twitter.
        """
        f_ns = None
        g_ns = None
        t_ns = None

        if self.finlight:
            f_ns = self.finlight.get_market_sentiment(question, event_type)
        # v9.2.2: Query per-mercato GDELT solo se feed healthy
        if self.gdelt and self.gdelt.is_healthy:
            g_ns = self.gdelt.get_market_sentiment(question, event_type)
        # v10.6: Twitter come 3° fallback
        if self.twitter and self.twitter.is_healthy:
            t_ns = self.twitter.get_market_sentiment(question, event_type)

        # Raccogli fonti con dati
        sources = []
        if f_ns and f_ns.n_articles > 0:
            sources.append(("finlight", f_ns))
        if g_ns and g_ns.n_articles > 0:
            sources.append(("gdelt", g_ns))
        if t_ns and t_ns.n_articles > 0:
            sources.append(("twitter", t_ns))

        if not sources:
            return NewsSentiment(event_type=event_type)

        # Ordina per n_articles desc; a parita' Finlight > GDELT > Twitter
        priority = {"finlight": 0, "gdelt": 1, "twitter": 2}
        sources.sort(key=lambda x: (-x[1].n_articles, priority.get(x[0], 9)))
        return sources[0][1]

    def _get_merged_news_strength(self, event_type: str) -> float:
        """
        max(finlight, gdelt, glint, twitter). Se 2+ concordano (>0.3): +10%.
        """
        f_str = 0.0
        g_str = 0.0
        gl_str = 0.0
        t_str = 0.0

        if self.finlight:
            f_str = self.finlight.get_news_strength(event_type)
        if self.gdelt:
            g_str = self.gdelt.get_news_strength(event_type)
        if self.glint and self.glint.available:
            gl_str = self.glint.get_news_strength(event_type)
        if self.twitter and self.twitter.is_healthy:
            t_str = self.twitter.get_news_strength(event_type)

        strength = max(f_str, g_str, gl_str, t_str)

        # Boost 10% se 2+ fonti concordano (segnale forte)
        sources_above = sum(1 for s in (f_str, g_str, gl_str, t_str) if s > 0.3)
        if sources_above >= 2:
            strength = min(strength * 1.10, 1.0)

        return strength

    def _classify_event(self, market: Market) -> str:
        """Classifica un mercato per tipo di evento."""
        q = market.question.lower()
        tags = " ".join(market.tags).lower()
        combined = f"{q} {tags}"

        for event_type, keywords in EVENT_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                return event_type

        return ""

    async def execute(self, opp: EventOpportunity, paper: bool = True) -> bool:
        """
        Esegui un trade event-driven.
        v5.9.3: Size aumentata per news-reactive (segnale piu' forte).
        """
        now = time.time()
        market_id = opp.market.id
        last_traded = self._recently_traded.get(market_id, 0)

        # Cooldown: panic_fade 120s (veloce), news/glint 180s, structural 300s
        if opp.signal_type == "panic_fade":
            cooldown = 120
        elif opp.signal_type in ("news_reactive", "glint_reactive"):
            cooldown = 180
        else:
            cooldown = self._TRADE_COOLDOWN
        if now - last_traded < cooldown:
            return False

        # Non ri-comprare mercati con posizione aperta
        for open_t in self.risk.open_trades:
            if open_t.market_id == market_id:
                return False

        token_key = "yes" if opp.side == "YES" else "no"
        token_id = opp.market.tokens[token_key]
        price = opp.market.prices[token_key]

        if price < MIN_TOKEN_PRICE or price > MAX_TOKEN_PRICE:
            return False

        win_prob = min(price + opp.edge, 0.95)
        size = self.risk.kelly_size(
            win_prob=win_prob,
            price=price,
            strategy=STRATEGY_NAME,
            is_maker=True,
        )

        if size == 0:
            logger.info(
                f"[EVENT] kelly_size=0 '{opp.market.question[:35]}' "
                f"p={price:.3f} wp={win_prob:.3f} e={opp.edge:.3f}"
            )
            return False

        # v5.9.3: Size boost per news-reactive/glint-reactive (segnale forte + fee-free)
        if opp.signal_type in ("news_reactive", "glint_reactive") and opp.news_strength > 0.5:
            size = min(size * 1.3, self.risk.config.max_bet_size)

        # v10.8: Panic fade — size conservativa (50% Kelly), mean reversion non garantito
        if opp.signal_type == "panic_fade":
            size = size * 0.50

        allowed, reason = self.risk.can_trade(
            STRATEGY_NAME, size, price=price,
            side=f"BUY_{opp.side}", market_id=opp.market.id,
        )
        if not allowed:
            logger.info(f"[EVENT] Trade bloccato: {reason}")
            return False

        trade = Trade(
            timestamp=time.time(),
            strategy=STRATEGY_NAME,
            market_id=opp.market.id,
            token_id=token_id,
            side=f"BUY_{opp.side}",
            size=size,
            price=price,
            edge=opp.edge,
            reason=f"[{opp.event_type}/{opp.signal_type}] {opp.reasoning}",
        )

        if paper:
            news_tag = ""
            if opp.news_volume != "NONE":
                news_tag = f" news={opp.news_label}({opp.news_sentiment:+.2f})"
            logger.info(
                f"[PAPER] EVENT-{opp.signal_type.upper()}: BUY {opp.side} "
                f"'{opp.market.question[:35]}' "
                f"${size:.2f} @{price:.4f} edge={opp.edge:.4f} "
                f"({opp.event_type}){news_tag}"
            )
            self.risk.open_trade(trade)

            # Simulazione: win prob basata sull'edge calcolato
            sim_win_prob = min(max(price + opp.edge * 0.5, 0.40), 0.75)

            won = random.random() < sim_win_prob
            slippage = 0.93 + random.random() * 0.05
            if won:
                raw_mult = (1.0 / price) - 1.0
                capped_mult = min(raw_mult, MAX_PAYOFF_MULT)
                pnl = size * capped_mult * slippage
            else:
                pnl = -size * slippage
            self.risk.close_trade(token_id, won=won, pnl=pnl)
        else:
            # v5.9.3: Timeout ridotto per news-reactive/glint-reactive (velocita')
            timeout = 8.0 if opp.signal_type in ("news_reactive", "glint_reactive") else 12.0
            from utils.avellaneda_stoikov import market_inventory_frac
            inv = market_inventory_frac(self.risk.open_trades, opp.market.id, self.risk._strategy_budgets.get(STRATEGY_NAME, 1))
            vpin_val = self.risk.vpin_monitor.get_vpin(opp.market.id) if self.risk.vpin_monitor else 0.0
            result = self.api.smart_buy(
                token_id, size, target_price=price,
                timeout_sec=timeout, fallback_market=True,
                inventory_frac=inv, volume_24h=opp.market.volume, vpin=vpin_val,
            )
            if result:
                # v7.4: Aggiorna prezzo con fill reale dal CLOB
                if isinstance(result, dict) and result.get("_fill_price"):
                    trade.price = result["_fill_price"]
                self.risk.open_trade(trade)

        self._recently_traded[market_id] = time.time()
        self._trades_executed += 1
        return True

    @property
    def stats(self) -> dict:
        return {
            "trades_executed": self._trades_executed,
            "breaking_news_cached": len(self._breaking_cache),
        }
