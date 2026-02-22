"""
Feed FinBERT — Sentiment Analysis NLP avanzata per news finanziarie
===================================================================
v1.0: Analisi sentiment con FinBERT (ProsusAI/finbert) e fallback VADER.

Architettura a 2 livelli con graceful degradation:
  1. FinBERT  — modello transformer fine-tuned su testi finanziari
               (richiede transformers + torch)
  2. VADER   — rule-based sentiment (nltk), fallback leggero
  3. Neutro  — se nessun backend e' disponibile, ritorna sentiment 0

FinBERT e' specificamente addestrato su linguaggio finanziario:
- Distingue "acquisition" positivo da "hostile takeover" negativo
- Capisce che "rate cut" e' positivo per mercati, "rate hike" negativo
- Confidence calibrata su testi finanziari (non generici)

Uso nel bot:
- Analisi sentiment di singoli testi o batch
- Valutazione rilevanza e sentiment per un mercato Polymarket specifico
- Complementare a Finlight: Finlight da' il raw data, FinBERT lo analizza
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    """Risultato dell'analisi sentiment di un singolo testo."""
    sentiment: str      # "positive", "negative", "neutral"
    confidence: float   # 0.0 - 1.0
    scores: dict = field(default_factory=dict)  # {"positive": x, "negative": y, "neutral": z}

    @property
    def sentiment_score(self) -> float:
        """Segnale numerico: -1.0 (negativo) a +1.0 (positivo), pesato per confidence."""
        if self.sentiment == "positive":
            return self.confidence
        elif self.sentiment == "negative":
            return -self.confidence
        return 0.0


@dataclass
class MarketSentimentResult:
    """Risultato dell'analisi sentiment aggregata per un mercato."""
    relevance: float    # 0.0 - 1.0 quanto le news sono rilevanti per il mercato
    sentiment: float    # -1.0 a +1.0 sentiment aggregato
    confidence: float   # 0.0 - 1.0 confidenza nell'analisi
    n_articles: int = 0
    backend: str = "none"


class FinBERTFeed:
    """
    Feed di sentiment analysis avanzata con FinBERT.

    Graceful degradation:
    1. Prova FinBERT (transformers + torch) — migliore per testi finanziari
    2. Fallback VADER (nltk) — rule-based, leggero
    3. Nessun backend — ritorna sentiment neutro con confidence 0

    Uso:
        feed = FinBERTFeed()
        result = feed.analyze("Fed raises interest rates by 50bps")
        # result.sentiment = "negative", result.confidence = 0.87

        batch = feed.analyze_batch(["text1", "text2", ...])

        market_sent = feed.analyze_news_for_market(
            articles=[{"title": "...", "summary": "..."}],
            market_question="Will the Fed raise rates in March?"
        )
    """

    def __init__(self):
        self._backend = "none"
        self._tokenizer = None
        self._model = None
        self._sia = None

        # Prova FinBERT
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            self._tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
            self._model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
            self._backend = "finbert"
            logger.info("[FINBERT] Backend FinBERT caricato (ProsusAI/finbert)")
        except (ImportError, OSError) as e:
            logger.info(f"[FINBERT] FinBERT non disponibile ({e}), provo VADER...")

            # Fallback VADER
            try:
                import nltk
                try:
                    nltk.data.find("sentiment/vader_lexicon.zip")
                except LookupError:
                    nltk.download("vader_lexicon", quiet=True)
                from nltk.sentiment import SentimentIntensityAnalyzer
                self._sia = SentimentIntensityAnalyzer()
                self._backend = "vader"
                logger.info("[FINBERT] Backend VADER caricato (fallback)")
            except (ImportError, OSError) as e2:
                logger.warning(
                    f"[FINBERT] Nessun backend sentiment disponibile ({e2}) — "
                    f"ritornera' sempre sentiment neutro con confidence 0"
                )
                self._backend = "none"

    @property
    def backend(self) -> str:
        """Backend attivo: 'finbert', 'vader', o 'none'."""
        return self._backend

    @property
    def available(self) -> bool:
        """True se almeno un backend e' disponibile."""
        return self._backend != "none"

    def analyze(self, text: str) -> SentimentResult:
        """
        Analizza il sentiment di un singolo testo.

        Returns: SentimentResult con sentiment, confidence e scores dettagliati.
        """
        if not text or not text.strip():
            return SentimentResult(sentiment="neutral", confidence=0.0, scores={})

        if self._backend == "finbert":
            return self._analyze_finbert(text)
        elif self._backend == "vader":
            return self._analyze_vader(text)
        else:
            return SentimentResult(
                sentiment="neutral",
                confidence=0.0,
                scores={"positive": 0.33, "negative": 0.33, "neutral": 0.34},
            )

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        """
        Analisi batch per efficienza.

        Con FinBERT, processa tutti i testi in un singolo forward pass.
        Con VADER, itera sequenzialmente (non ha batching nativo).
        """
        if not texts:
            return []

        if self._backend == "finbert":
            return self._analyze_finbert_batch(texts)
        else:
            # VADER e none: analisi sequenziale
            return [self.analyze(text) for text in texts]

    def analyze_news_for_market(
        self, articles: list, market_question: str
    ) -> MarketSentimentResult:
        """
        Analizza rilevanza e sentiment delle news per un mercato specifico.

        1. Filtra articoli rilevanti (keyword matching con la domanda del mercato)
        2. Analizza sentiment degli articoli rilevanti
        3. Aggrega in un risultato pesato per rilevanza e confidence

        Args:
            articles: lista di dict con almeno "title" e/o "summary"
            market_question: domanda del mercato Polymarket

        Returns: MarketSentimentResult con relevance, sentiment, confidence
        """
        if not articles or not market_question:
            return MarketSentimentResult(
                relevance=0.0, sentiment=0.0, confidence=0.0,
                backend=self._backend,
            )

        # Estrai keyword dalla domanda del mercato
        market_keywords = self._extract_keywords(market_question)

        # Filtra articoli rilevanti e calcola rilevanza
        relevant_articles: list[tuple[dict, float]] = []  # (article, relevance)
        for article in articles:
            title = article.get("title", "") if isinstance(article, dict) else str(article)
            summary = article.get("summary", "") if isinstance(article, dict) else ""
            article_text = f"{title} {summary}".lower()

            # Calcola rilevanza: quante keyword del mercato appaiono nell'articolo
            if not market_keywords:
                relevance = 0.0
            else:
                matches = sum(1 for kw in market_keywords if kw in article_text)
                relevance = min(matches / max(len(market_keywords) * 0.5, 1), 1.0)

            if relevance > 0.1:
                relevant_articles.append((article, relevance))

        if not relevant_articles:
            return MarketSentimentResult(
                relevance=0.0, sentiment=0.0, confidence=0.0,
                n_articles=0, backend=self._backend,
            )

        # Analizza sentiment degli articoli rilevanti
        texts = []
        for article, _ in relevant_articles:
            title = article.get("title", "") if isinstance(article, dict) else str(article)
            summary = article.get("summary", "") if isinstance(article, dict) else ""
            # Usa title + summary (troncato) per analisi
            text = f"{title}. {summary}"[:512]
            texts.append(text)

        results = self.analyze_batch(texts)

        # Aggrega: media pesata per rilevanza e confidence
        total_weight = 0.0
        weighted_sentiment = 0.0
        weighted_confidence = 0.0
        avg_relevance = 0.0

        for (article, relevance), result in zip(relevant_articles, results):
            weight = relevance * result.confidence
            if weight > 0:
                weighted_sentiment += result.sentiment_score * weight
                weighted_confidence += result.confidence * relevance
                total_weight += weight
                avg_relevance += relevance

        n = len(relevant_articles)
        if total_weight > 0 and n > 0:
            final_sentiment = weighted_sentiment / total_weight
            final_confidence = weighted_confidence / n
            final_relevance = avg_relevance / n
        else:
            final_sentiment = 0.0
            final_confidence = 0.0
            final_relevance = 0.0

        return MarketSentimentResult(
            relevance=min(final_relevance, 1.0),
            sentiment=max(min(final_sentiment, 1.0), -1.0),
            confidence=min(final_confidence, 1.0),
            n_articles=n,
            backend=self._backend,
        )

    # ── Backend: FinBERT ──────────────────────────────────────────

    def _analyze_finbert(self, text: str) -> SentimentResult:
        """Analisi con FinBERT (singolo testo)."""
        try:
            import torch

            inputs = self._tokenizer(
                text, return_tensors="pt",
                truncation=True, max_length=512, padding=True,
            )
            with torch.no_grad():
                outputs = self._model(**inputs)

            probabilities = torch.nn.functional.softmax(outputs.logits, dim=-1)
            probs = probabilities[0].tolist()

            # FinBERT labels: ["positive", "negative", "neutral"]
            labels = self._model.config.id2label
            scores = {}
            for i, prob in enumerate(probs):
                label = labels.get(i, f"label_{i}").lower()
                scores[label] = round(prob, 4)

            # Determina sentiment dominante
            max_label = max(scores, key=scores.get)
            confidence = scores[max_label]

            return SentimentResult(
                sentiment=max_label,
                confidence=round(confidence, 4),
                scores=scores,
            )

        except Exception as e:
            logger.debug(f"[FINBERT] Errore analisi: {e}")
            return SentimentResult(sentiment="neutral", confidence=0.0, scores={})

    def _analyze_finbert_batch(self, texts: list[str]) -> list[SentimentResult]:
        """Batch analysis con FinBERT (singolo forward pass)."""
        try:
            import torch

            # Filtra testi vuoti
            valid_texts = [t if t and t.strip() else "neutral" for t in texts]

            inputs = self._tokenizer(
                valid_texts, return_tensors="pt",
                truncation=True, max_length=512,
                padding=True,
            )
            with torch.no_grad():
                outputs = self._model(**inputs)

            probabilities = torch.nn.functional.softmax(outputs.logits, dim=-1)
            labels = self._model.config.id2label

            results = []
            for i in range(len(valid_texts)):
                probs = probabilities[i].tolist()
                scores = {}
                for j, prob in enumerate(probs):
                    label = labels.get(j, f"label_{j}").lower()
                    scores[label] = round(prob, 4)

                max_label = max(scores, key=scores.get)
                confidence = scores[max_label]

                results.append(SentimentResult(
                    sentiment=max_label,
                    confidence=round(confidence, 4),
                    scores=scores,
                ))

            return results

        except Exception as e:
            logger.debug(f"[FINBERT] Errore batch: {e}")
            return [self.analyze(t) for t in texts]

    # ── Backend: VADER ────────────────────────────────────────────

    def _analyze_vader(self, text: str) -> SentimentResult:
        """Analisi con VADER (rule-based, fallback)."""
        try:
            scores_raw = self._sia.polarity_scores(text)

            # VADER scores: neg, neu, pos, compound (-1 a +1)
            compound = scores_raw.get("compound", 0.0)

            if compound >= 0.05:
                sentiment = "positive"
            elif compound <= -0.05:
                sentiment = "negative"
            else:
                sentiment = "neutral"

            # Mappa compound a confidence (0.0 - 1.0)
            confidence = min(abs(compound), 1.0)

            scores = {
                "positive": round(scores_raw.get("pos", 0.0), 4),
                "negative": round(scores_raw.get("neg", 0.0), 4),
                "neutral": round(scores_raw.get("neu", 0.0), 4),
                "compound": round(compound, 4),
            }

            return SentimentResult(
                sentiment=sentiment,
                confidence=round(confidence, 4),
                scores=scores,
            )

        except Exception as e:
            logger.debug(f"[FINBERT] Errore VADER: {e}")
            return SentimentResult(sentiment="neutral", confidence=0.0, scores={})

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        """
        Estrai keyword significative da una domanda Polymarket.
        Rimuove stop words e parole troppo corte.
        """
        stop_words = {
            "will", "the", "be", "by", "on", "in", "at", "to", "of",
            "a", "an", "is", "or", "and", "for", "this", "that", "it",
            "yes", "no", "before", "after", "end", "day", "what",
            "when", "where", "how", "who", "which", "do", "does",
            "did", "has", "have", "been", "was", "were", "are",
        }
        words = question.lower().split()
        return [w for w in words if w not in stop_words and len(w) > 2]
