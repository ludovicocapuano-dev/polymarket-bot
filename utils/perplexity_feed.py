"""
Perplexity Feed (v10.8)
========================
Verifica in tempo reale se un evento di un mercato Polymarket
e' gia' accaduto, usando Perplexity Sonar (web search + AI).

Use case principali:
1. Resolution Sniper: verificare se un evento e' realmente accaduto
   prima di comprare il token vincente
2. Bond/Event: sanity check su mercati quasi certi
3. Weather: non usato (abbiamo gia' multi-provider meteo)

Costo: ~$0.005 per query (Sonar). Budget: ~100-200 query/giorno = $15-30/mese
"""

import logging
import os
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# Cache TTL: 10 minuti per risultati di verification
CACHE_TTL = 600

# Rate limit: max 1 query ogni 2 secondi
MIN_INTERVAL = 2.0


@dataclass
class VerificationResult:
    """Risultato della verifica Perplexity."""
    question: str
    answer: str           # "YES", "NO", "UNCERTAIN"
    confidence: float     # 0-1
    explanation: str
    citations: list[str]
    cost: float           # costo in USD


class PerplexityFeed:
    """Client Perplexity API per verification mercati."""

    def __init__(self):
        self.api_key = os.getenv("PERPLEXITY_API_KEY", "")
        self.enabled = bool(self.api_key)
        self._cache: dict[str, tuple[float, VerificationResult]] = {}
        self._last_call: float = 0.0
        self._total_cost: float = 0.0

        if not self.enabled:
            logger.info("[PERPLEXITY] Disabilitato — PERPLEXITY_API_KEY mancante")

    def verify_event(self, question: str) -> VerificationResult | None:
        """
        Verifica se l'evento descritto dalla domanda e' gia' accaduto.
        Usa Perplexity Sonar per ricerca web + AI.

        Returns:
            VerificationResult o None se errore/disabilitato
        """
        if not self.enabled:
            return None

        # Cache check
        cache_key = question[:100].lower().strip()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if time.time() - ts < CACHE_TTL:
                return cached

        # Rate limit
        now = time.time()
        wait = MIN_INTERVAL - (now - self._last_call)
        if wait > 0:
            import time as t
            t.sleep(wait)

        try:
            resp = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "sonar",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a fact-checker for prediction markets. "
                                "Answer the user's question about whether an event has occurred. "
                                "Start your answer with exactly one of: YES, NO, or UNCERTAIN. "
                                "Then provide a brief 1-2 sentence explanation with evidence. "
                                "Be precise about dates and facts."
                            ),
                        },
                        {
                            "role": "user",
                            "content": question,
                        },
                    ],
                },
                timeout=15,
            )
            self._last_call = time.time()

            if resp.status_code != 200:
                logger.warning(f"[PERPLEXITY] API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            citations = [c for c in data.get("citations", []) if isinstance(c, str)]
            cost = data.get("usage", {}).get("cost", {}).get("total_cost", 0.005)
            self._total_cost += cost

            # Parse risposta
            answer, confidence, explanation = self._parse_response(content)

            result = VerificationResult(
                question=question,
                answer=answer,
                confidence=confidence,
                explanation=explanation,
                citations=citations[:3],
                cost=cost,
            )

            # Cache
            self._cache[cache_key] = (time.time(), result)

            logger.info(
                f"[PERPLEXITY] {answer} (conf={confidence:.0%}) — "
                f"'{question[:60]}' — ${cost:.4f} "
                f"(totale=${self._total_cost:.3f})"
            )

            return result

        except Exception as e:
            logger.warning(f"[PERPLEXITY] Errore: {e}")
            return None

    def verify_resolution(
        self, market_question: str, proposed_outcome: str
    ) -> tuple[bool, float, str]:
        """
        Verifica se la risoluzione proposta e' corretta.

        Args:
            market_question: domanda del mercato Polymarket
            proposed_outcome: "YES" o "NO"

        Returns:
            (is_confirmed, confidence, explanation)
        """
        result = self.verify_event(market_question)

        if not result:
            return False, 0.0, "perplexity unavailable"

        # Confronta la risposta Perplexity con l'outcome proposto
        if result.answer == proposed_outcome:
            return True, result.confidence, result.explanation
        elif result.answer == "UNCERTAIN":
            return False, 0.3, f"uncertain: {result.explanation}"
        else:
            return False, 1.0 - result.confidence, f"contradicts: {result.explanation}"

    @staticmethod
    def _parse_response(content: str) -> tuple[str, float, str]:
        """Parse la risposta Perplexity in (answer, confidence, explanation)."""
        content = content.strip()
        upper = content.upper()

        if upper.startswith("YES"):
            answer = "YES"
            confidence = 0.85
        elif upper.startswith("NO"):
            answer = "NO"
            confidence = 0.85
        elif upper.startswith("UNCERTAIN") or upper.startswith("UNKNOWN"):
            answer = "UNCERTAIN"
            confidence = 0.40
        else:
            # Cerca YES/NO nel primo paragrafo
            first_line = content.split("\n")[0].upper()
            if "YES" in first_line and "NO" not in first_line:
                answer = "YES"
                confidence = 0.70
            elif "NO" in first_line and "YES" not in first_line:
                answer = "NO"
                confidence = 0.70
            else:
                answer = "UNCERTAIN"
                confidence = 0.30

        # Aggiusta confidence basandosi su parole chiave
        lower = content.lower()
        if any(w in lower for w in ["confirmed", "officially", "announced", "reported"]):
            confidence = min(confidence + 0.10, 0.95)
        if any(w in lower for w in ["unclear", "not yet", "no reports", "no evidence"]):
            confidence = max(confidence - 0.15, 0.20)

        # Explanation = tutto dopo la prima riga
        lines = content.split("\n", 1)
        explanation = lines[1].strip() if len(lines) > 1 else content

        return answer, confidence, explanation[:200]

    @property
    def total_cost(self) -> float:
        return self._total_cost
