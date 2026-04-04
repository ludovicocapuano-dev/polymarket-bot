"""
Cross-Platform Probability Feed (v10.8)
========================================
Confronta le probabilita' Polymarket con Manifold Markets e Metaculus
per identificare mispricing e overconfidence.

Manifold Markets: API gratuita, no auth, play-money ma ben calibrata.
Metaculus: API richiede token (gratuito con account), forecaster esperti.

Uso nel bot:
- Sanity check: se divergenza > 15%, il SignalValidator puo' bloccare/flaggare
- Edge calibration: la differenza cross-platform e' un proxy per l'edge reale
"""

import logging
import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

import requests

logger = logging.getLogger(__name__)

# Cache TTL: 5 minuti
CACHE_TTL = 300

# Soglia minima di similarita' per matching domande (0-1)
MIN_SIMILARITY = 0.45


@dataclass
class CrossPlatformProb:
    """Probabilita' da un'altra piattaforma per confronto."""
    platform: str           # "manifold" o "metaculus"
    question: str           # Domanda originale della piattaforma
    probability: float      # Probabilita' aggregata (0-1)
    similarity: float       # Similarita' con la domanda Polymarket (0-1)
    url: str = ""
    volume: float = 0.0
    n_forecasters: int = 0


class CrossPlatformFeed:
    """
    Confronta probabilita' Polymarket con altre piattaforme.
    Usato come sanity check nel SignalValidator.
    """

    def __init__(self):
        self._manifold_cache: dict[str, tuple[float, list[CrossPlatformProb]]] = {}
        self._metaculus_token = os.getenv("METACULUS_TOKEN", "")

    def get_cross_platform_prob(
        self, question: str, polymarket_prob: float
    ) -> list[CrossPlatformProb]:
        """
        Cerca domande simili su Manifold e Metaculus.
        Ritorna lista di probabilita' cross-platform per confronto.
        """
        results = []

        # Manifold (sempre disponibile, no auth)
        manifold = self._search_manifold(question)
        results.extend(manifold)

        # Metaculus (richiede token)
        if self._metaculus_token:
            metaculus = self._search_metaculus(question)
            results.extend(metaculus)

        return results

    def check_divergence(
        self, question: str, polymarket_prob: float, min_divergence: float = 0.15
    ) -> tuple[bool, float, str]:
        """
        Controlla se la probabilita' Polymarket diverge significativamente
        da altre piattaforme.

        Returns:
            (is_divergent, max_divergence, detail_string)
        """
        probs = self.get_cross_platform_prob(question, polymarket_prob)

        if not probs:
            return False, 0.0, "no cross-platform data"

        max_div = 0.0
        detail = ""

        for p in probs:
            div = abs(polymarket_prob - p.probability)
            if div > max_div:
                max_div = div
                detail = (
                    f"{p.platform}: {p.probability:.0%} vs Polymarket {polymarket_prob:.0%} "
                    f"(div={div:.0%}, sim={p.similarity:.2f}) "
                    f"Q: '{p.question[:60]}'"
                )

        return max_div >= min_divergence, max_div, detail

    def _search_manifold(self, question: str) -> list[CrossPlatformProb]:
        """Cerca domande simili su Manifold Markets (gratuito, no auth)."""
        # Controlla cache
        cache_key = question[:50].lower()
        if cache_key in self._manifold_cache:
            ts, cached = self._manifold_cache[cache_key]
            if time.time() - ts < CACHE_TTL:
                return cached

        # Estrai keywords dalla domanda
        search_terms = self._extract_keywords(question)
        if not search_terms:
            return []

        try:
            resp = requests.get(
                "https://api.manifold.markets/v0/search-markets",
                params={
                    "term": search_terms,
                    "limit": 5,
                    "sort": "score",
                    "filter": "open",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug(f"[XPLATFORM] Manifold API error: {resp.status_code}")
                return []

            markets = resp.json()
            results = []

            for m in markets:
                if m.get("outcomeType") != "BINARY":
                    continue
                if m.get("isResolved", False):
                    continue

                mq = m.get("question", "")
                sim = self._similarity(question, mq)

                if sim >= MIN_SIMILARITY:
                    results.append(CrossPlatformProb(
                        platform="manifold",
                        question=mq,
                        probability=m.get("probability", 0.5),
                        similarity=sim,
                        url=m.get("url", ""),
                        volume=m.get("volume", 0),
                        n_forecasters=m.get("uniqueBettorCount", 0),
                    ))

            # Cache
            self._manifold_cache[cache_key] = (time.time(), results)

            if results:
                logger.debug(
                    f"[XPLATFORM] Manifold: {len(results)} match per "
                    f"'{search_terms}' (best sim={results[0].similarity:.2f})"
                )

            return results

        except Exception as e:
            logger.debug(f"[XPLATFORM] Manifold errore: {e}")
            return []

    def _search_metaculus(self, question: str) -> list[CrossPlatformProb]:
        """Cerca domande simili su Metaculus (richiede token)."""
        if not self._metaculus_token:
            return []

        search_terms = self._extract_keywords(question)
        if not search_terms:
            return []

        try:
            resp = requests.get(
                "https://www.metaculus.com/api/posts/",
                params={
                    "search": search_terms,
                    "limit": 5,
                    "status": "open",
                    "forecast_type": "binary",
                    "type": "forecast",
                },
                headers={"Authorization": f"Token {self._metaculus_token}"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug(f"[XPLATFORM] Metaculus API error: {resp.status_code}")
                return []

            data = resp.json()
            results_list = data.get("results", [])
            results = []

            for q in results_list:
                mq = q.get("title", "")
                sim = self._similarity(question, mq)

                if sim < MIN_SIMILARITY:
                    continue

                # Estrai community prediction
                forecast = q.get("question", {})
                if isinstance(forecast, dict):
                    agg = forecast.get("aggregations", {})
                    recency = agg.get("recency_weighted", {})
                    centers = recency.get("centers", [])
                    prob = centers[0] if centers else 0.5
                else:
                    prob = 0.5

                results.append(CrossPlatformProb(
                    platform="metaculus",
                    question=mq,
                    probability=prob,
                    similarity=sim,
                    url=f"https://www.metaculus.com/questions/{q.get('id', '')}/",
                    n_forecasters=q.get("nr_forecasters", 0),
                ))

            return results

        except Exception as e:
            logger.debug(f"[XPLATFORM] Metaculus errore: {e}")
            return []

    @staticmethod
    def _extract_keywords(question: str) -> str:
        """Estrai parole chiave significative dalla domanda."""
        # Rimuovi parole comuni
        stopwords = {
            "will", "the", "be", "a", "an", "in", "on", "at", "to", "of",
            "for", "is", "by", "or", "and", "not", "this", "that", "with",
            "have", "has", "had", "do", "does", "did", "was", "were",
            "highest", "temperature", "between", "what", "which", "who",
            "how", "when", "where", "march", "april", "may", "june",
            "january", "february",
        }
        words = question.lower().split()
        # Rimuovi punteggiatura
        words = [w.strip("?.,!°'\"()") for w in words]
        keywords = [w for w in words if w and w not in stopwords and len(w) > 2]
        # Prendi max 5 keywords
        return " ".join(keywords[:5])

    @staticmethod
    def _similarity(q1: str, q2: str) -> float:
        """Calcola similarita' tra due domande (0-1)."""
        return SequenceMatcher(None, q1.lower(), q2.lower()).ratio()
