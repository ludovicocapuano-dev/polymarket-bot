"""
Hyperspace Local LLM Wrapper (v12.3)
=====================================
Wrapper che usa Hyperspace (localhost:8080) come LLM locale gratuito
per le verifiche di risoluzione mercati, con fallback a Perplexity.

Risparmio stimato: 80-95% delle query Perplexity (~$10-25/mese).
Hyperspace supporta API OpenAI-compatible su localhost:8080/v1/chat/completions.
Modelli disponibili: qwen2.5-coder-7b, gemma-3-4b, llama-3.1-8b-instruct, gemini-2.0-flash (via network).
"""

import json
import logging
import os
import time
from dataclasses import dataclass

import requests

from utils.perplexity_feed import PerplexityFeed, VerificationResult

logger = logging.getLogger(__name__)

# Hyperspace/Ollama config — prefer Ollama (reliable local CPU inference)
HYPERSPACE_URL = os.getenv("HYPERSPACE_URL", "http://localhost:11434")
HYPERSPACE_MODEL = os.getenv("HYPERSPACE_MODEL", "qwen2.5:0.5b")
HYPERSPACE_TIMEOUT = 60  # secondi — CPU inference è lento
CONFIDENCE_FALLBACK_THRESHOLD = 0.60  # sotto questa confidence -> fallback Perplexity

# Stats file per tracking savings
STATS_FILE = "logs/hyperspace_stats.json"


@dataclass
class HyperspaceStats:
    """Statistiche uso Hyperspace vs Perplexity."""
    local_calls: int = 0
    local_success: int = 0
    fallback_calls: int = 0
    local_errors: int = 0
    estimated_savings: float = 0.0  # USD risparmiati (~$0.005/query)

    def to_dict(self) -> dict:
        return {
            "local_calls": self.local_calls,
            "local_success": self.local_success,
            "fallback_calls": self.fallback_calls,
            "local_errors": self.local_errors,
            "estimated_savings": round(self.estimated_savings, 4),
            "local_rate": (
                f"{self.local_success / max(self.local_calls + self.fallback_calls, 1):.0%}"
            ),
        }

    def save(self):
        try:
            with open(STATS_FILE, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception:
            pass

    @classmethod
    def load(cls) -> "HyperspaceStats":
        try:
            with open(STATS_FILE) as f:
                d = json.load(f)
            return cls(
                local_calls=d.get("local_calls", 0),
                local_success=d.get("local_success", 0),
                fallback_calls=d.get("fallback_calls", 0),
                local_errors=d.get("local_errors", 0),
                estimated_savings=d.get("estimated_savings", 0.0),
            )
        except Exception:
            return cls()


# System prompt identico a Perplexity per consistenza parsing
SYSTEM_PROMPT = (
    "You are a fact-checker for prediction markets. "
    "Answer the user's question about whether an event has occurred. "
    "Start your answer with exactly one of: YES, NO, or UNCERTAIN. "
    "Then provide a brief 1-2 sentence explanation with evidence. "
    "Be precise about dates and facts."
)


class HyperspaceLLM:
    """
    Wrapper LLM che usa Hyperspace locale come prima scelta,
    con fallback a Perplexity se la risposta locale non e' affidabile.

    Compatibile come drop-in replacement per PerplexityFeed:
    stessi metodi verify_event() e verify_resolution().
    """

    def __init__(self, perplexity: PerplexityFeed | None = None):
        self.perplexity = perplexity
        self._hyperspace_url = f"{HYPERSPACE_URL}/v1/chat/completions"
        self._model = HYPERSPACE_MODEL
        self._stats = HyperspaceStats.load()
        self._hyperspace_available = True  # circuit breaker
        self._consecutive_errors = 0
        self._circuit_open_until = 0.0

        # Expose same interface as PerplexityFeed
        self.enabled = True  # sempre enabled (local e' gratis)
        self._total_cost = 0.0

        # Verifica connettivita' Hyperspace al boot
        self._check_hyperspace()

    def _check_hyperspace(self):
        """Verifica che Hyperspace sia raggiungibile."""
        try:
            resp = requests.get(
                f"{HYPERSPACE_URL}/v1/models",
                timeout=5,
            )
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                model_ids = [m.get("id", "") for m in models]
                logger.info(
                    f"[HYPERSPACE] Connesso — modelli: {', '.join(model_ids[:5])}"
                )
                self._hyperspace_available = True
            else:
                logger.warning(
                    f"[HYPERSPACE] Risposta {resp.status_code} — "
                    f"fallback Perplexity attivo"
                )
                self._hyperspace_available = False
        except Exception as e:
            logger.warning(
                f"[HYPERSPACE] Non raggiungibile ({e}) — "
                f"fallback Perplexity attivo"
            )
            self._hyperspace_available = False

    def _query_hyperspace(self, question: str) -> VerificationResult | None:
        """
        Invia query al LLM locale Hyperspace.
        Returns VerificationResult o None se errore.
        """
        # Circuit breaker: se troppi errori consecutivi, skip per 5 min
        if self._consecutive_errors >= 3:
            if time.time() < self._circuit_open_until:
                return None
            # Reset circuit breaker e riprova
            self._consecutive_errors = 0
            self._hyperspace_available = True
            logger.info("[HYPERSPACE] Circuit breaker reset — riprovo")

        try:
            resp = requests.post(
                self._hyperspace_url,
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question},
                    ],
                    "temperature": 0.1,  # bassa per risposte deterministiche
                    "max_tokens": 300,
                },
                timeout=HYPERSPACE_TIMEOUT,
            )

            if resp.status_code != 200:
                logger.warning(
                    f"[HYPERSPACE] API error {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._circuit_open_until = time.time() + 300
                    logger.warning(
                        "[HYPERSPACE] Circuit breaker aperto — "
                        "fallback Perplexity per 5 min"
                    )
                self._stats.local_errors += 1
                return None

            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            if not content.strip():
                logger.warning("[HYPERSPACE] Risposta vuota")
                self._stats.local_errors += 1
                return None

            # Reset errori consecutivi su successo
            self._consecutive_errors = 0

            # Parse con lo stesso metodo di Perplexity
            answer, confidence, explanation = PerplexityFeed._parse_response(
                content
            )

            result = VerificationResult(
                question=question,
                answer=answer,
                confidence=confidence,
                explanation=explanation,
                citations=[],  # LLM locale non ha citations
                cost=0.0,  # gratuito
            )

            return result

        except requests.exceptions.Timeout:
            logger.warning(
                f"[HYPERSPACE] Timeout ({HYPERSPACE_TIMEOUT}s) — "
                f"'{question[:50]}'"
            )
            self._consecutive_errors += 1
            self._stats.local_errors += 1
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("[HYPERSPACE] Connection refused — server down?")
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._circuit_open_until = time.time() + 300
            self._stats.local_errors += 1
            return None
        except Exception as e:
            logger.warning(f"[HYPERSPACE] Errore: {e}")
            self._consecutive_errors += 1
            self._stats.local_errors += 1
            return None

    def verify_event(self, question: str) -> VerificationResult | None:
        """
        Verifica evento — prima locale, poi Perplexity se necessario.
        Drop-in replacement per PerplexityFeed.verify_event().
        """
        # 1. Prova Hyperspace locale (gratuito)
        if self._hyperspace_available or self._consecutive_errors < 3:
            self._stats.local_calls += 1
            local_result = self._query_hyperspace(question)

            if local_result and local_result.answer != "UNCERTAIN":
                if local_result.confidence >= CONFIDENCE_FALLBACK_THRESHOLD:
                    # Successo locale
                    self._stats.local_success += 1
                    self._stats.estimated_savings += 0.005
                    self._stats.save()

                    logger.info(
                        f"[HYPERSPACE] {local_result.answer} "
                        f"(conf={local_result.confidence:.0%}) — "
                        f"'{question[:60]}' — FREE "
                        f"(risparmiati ${self._stats.estimated_savings:.3f})"
                    )
                    return local_result
                else:
                    logger.info(
                        f"[HYPERSPACE] Confidence bassa "
                        f"({local_result.confidence:.0%} < "
                        f"{CONFIDENCE_FALLBACK_THRESHOLD:.0%}) — "
                        f"fallback Perplexity"
                    )

        # 2. Fallback a Perplexity
        if self.perplexity and self.perplexity.enabled:
            self._stats.fallback_calls += 1
            self._stats.save()

            logger.info(
                f"[PERPLEXITY-FALLBACK] Query: '{question[:60]}'"
            )
            result = self.perplexity.verify_event(question)

            if result:
                self._total_cost += result.cost

            return result

        # Nessun provider disponibile
        logger.warning(
            "[HYPERSPACE] Locale non disponibile e Perplexity disabilitato"
        )
        return None

    def verify_resolution(
        self, market_question: str, proposed_outcome: str
    ) -> tuple[bool, float, str]:
        """
        Verifica risoluzione — drop-in replacement per
        PerplexityFeed.verify_resolution().
        """
        result = self.verify_event(market_question)

        if not result:
            return False, 0.0, "no LLM available"

        if result.answer == proposed_outcome:
            return True, result.confidence, result.explanation
        elif result.answer == "UNCERTAIN":
            return False, 0.3, f"uncertain: {result.explanation}"
        else:
            return (
                False,
                1.0 - result.confidence,
                f"contradicts: {result.explanation}",
            )

    @property
    def total_cost(self) -> float:
        """Costo totale Perplexity (locale e' gratis)."""
        return self._total_cost

    @property
    def stats(self) -> dict:
        """Statistiche Hyperspace vs Perplexity."""
        return self._stats.to_dict()
