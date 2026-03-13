"""
Market Embeddings — Semantic intelligence via Hyperspace local embeddings.
==========================================================================
Usa il server Hyperspace locale (localhost:8080) con modello all-minilm-l6-v2
per generare embedding semantici dei titoli dei mercati Polymarket.

Funzionalita':
- embed_market(): embedding singolo titolo
- find_similar_markets(): trova mercati simili per cosine similarity
- detect_correlation_clusters(): raggruppa mercati correlati
- discover_uncovered_markets(): trova mercati non coperti dal bot

Cache TTL-based per evitare re-computazioni inutili.
"""

import logging
import time
from typing import Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ── Configurazione ──────────────────────────────────────────────

EMBEDDINGS_URL = "http://localhost:11434/v1/embeddings"
EMBEDDINGS_MODEL = "all-minilm"
DEFAULT_CACHE_TTL = 3600  # 1 ora
REQUEST_TIMEOUT = 10  # secondi


# ── Cache ───────────────────────────────────────────────────────

class EmbeddingCache:
    """Cache TTL-based per embedding. Evita chiamate ripetute al server."""

    def __init__(self, ttl: int = DEFAULT_CACHE_TTL):
        self.ttl = ttl
        self._cache: dict[str, tuple[list[float], float]] = {}  # text -> (embedding, timestamp)

    def get(self, text: str) -> Optional[list[float]]:
        if text in self._cache:
            embedding, ts = self._cache[text]
            if time.time() - ts < self.ttl:
                return embedding
            else:
                del self._cache[text]
        return None

    def put(self, text: str, embedding: list[float]):
        self._cache[text] = (embedding, time.time())

    def clear_expired(self):
        now = time.time()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts >= self.ttl]
        for k in expired:
            del self._cache[k]

    @property
    def size(self) -> int:
        return len(self._cache)


# Singola istanza globale
_cache = EmbeddingCache()


# ── Core functions ──────────────────────────────────────────────

def embed_market(title: str) -> list[float]:
    """
    Genera l'embedding per un titolo di mercato via Hyperspace.

    Args:
        title: titolo/question del mercato

    Returns:
        Lista di float (384 dimensioni per all-minilm-l6-v2)

    Raises:
        RuntimeError: se il server non risponde o ritorna errore
    """
    # Check cache
    cached = _cache.get(title)
    if cached is not None:
        return cached

    try:
        resp = requests.post(
            EMBEDDINGS_URL,
            json={"model": EMBEDDINGS_MODEL, "input": title},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenAI-compatible format: data[0].embedding
        embedding = data["data"][0]["embedding"]
        _cache.put(title, embedding)
        return embedding

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Hyperspace non raggiungibile su {EMBEDDINGS_URL}. "
            "Verificare che il server sia in esecuzione."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Timeout dopo {REQUEST_TIMEOUT}s su {EMBEDDINGS_URL}")
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Risposta inattesa dal server embeddings: {e}")
    except Exception as e:
        raise RuntimeError(f"Errore embedding per '{title[:50]}...': {e}")


def embed_batch(titles: list[str], batch_size: int = 32) -> list[list[float]]:
    """
    Embedding batch — invia piu' titoli in una singola richiesta.
    Fallback a singoli se il server non supporta batch.

    Args:
        titles: lista di titoli
        batch_size: dimensione batch (default 32)

    Returns:
        Lista di embedding (stesso ordine dei titoli)
    """
    results: list[tuple[int, list[float]]] = []
    uncached_indices = []
    uncached_titles = []

    # Separa cached da uncached
    for i, title in enumerate(titles):
        cached = _cache.get(title)
        if cached is not None:
            results.append((i, cached))
        else:
            uncached_indices.append(i)
            uncached_titles.append(title)

    # Fetch uncached in batch
    for start in range(0, len(uncached_titles), batch_size):
        batch = uncached_titles[start:start + batch_size]
        batch_idx = uncached_indices[start:start + batch_size]

        try:
            resp = requests.post(
                EMBEDDINGS_URL,
                json={"model": EMBEDDINGS_MODEL, "input": batch},
                timeout=REQUEST_TIMEOUT * 2,
            )
            resp.raise_for_status()
            data = resp.json()

            for j, item in enumerate(data["data"]):
                emb = item["embedding"]
                _cache.put(batch[j], emb)
                results.append((batch_idx[j], emb))

        except Exception:
            # Fallback: richieste singole
            logger.debug("[EMBEDDINGS] Batch fallito, fallback a singole richieste")
            for j, title in enumerate(batch):
                try:
                    emb = embed_market(title)
                    results.append((batch_idx[j], emb))
                except RuntimeError as e:
                    logger.warning(f"[EMBEDDINGS] Skip '{title[:40]}': {e}")
                    # Empty list come placeholder
                    results.append((batch_idx[j], []))

    # Riordina per indice originale
    results.sort(key=lambda x: x[0])
    return [emb for _, emb in results]


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity tra due vettori."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _cosine_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Matrice di cosine similarity NxN."""
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)  # evita divisione per zero
    normalized = embeddings / norms
    return normalized @ normalized.T


def find_similar_markets(
    target: str,
    market_titles: list[str],
    top_k: int = 5,
) -> list[tuple[str, float]]:
    """
    Trova i mercati piu' simili a un target per significato semantico.

    Args:
        target: titolo del mercato di riferimento
        market_titles: lista di titoli candidati
        top_k: quanti risultati ritornare

    Returns:
        Lista di (titolo, similarity_score) ordinata per similarita' decrescente
    """
    if not market_titles:
        return []

    target_emb = embed_market(target)
    target_vec = np.array(target_emb)

    all_titles = [t for t in market_titles if t != target]
    if not all_titles:
        return []

    embeddings = embed_batch(all_titles)
    valid = [(i, emb) for i, emb in enumerate(embeddings) if emb]

    if not valid:
        return []

    scores = []
    for i, emb in valid:
        sim = _cosine_similarity(target_vec, np.array(emb))
        scores.append((all_titles[i], sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_k]


def detect_correlation_clusters(
    markets: list[dict],
    threshold: float = 0.85,
) -> list[list[str]]:
    """
    Raggruppa mercati semanticamente correlati usando Union-Find clustering.

    I mercati nello stesso cluster probabilmente hanno outcome correlati
    (es. "Temperature in NYC March 15" e "Temperature in NYC March 16").
    Utile per il risk manager: evitare sovraesposizione su mercati correlati.

    Args:
        markets: lista di dict con almeno 'question' (o 'title')
        threshold: soglia di similarita' per considerare due mercati correlati

    Returns:
        Lista di cluster, ogni cluster e' una lista di titoli
    """
    if len(markets) < 2:
        return []

    titles = [m.get("question", m.get("title", "")) for m in markets]
    titles = [t for t in titles if t]

    if len(titles) < 2:
        return []

    embeddings = embed_batch(titles)
    valid_pairs = [(i, titles[i], emb) for i, emb in enumerate(embeddings) if emb]

    if len(valid_pairs) < 2:
        return []

    valid_titles = [t for _, t, _ in valid_pairs]
    emb_matrix = np.array([e for _, _, e in valid_pairs])
    sim_matrix = _cosine_similarity_matrix(emb_matrix)

    # Union-Find clustering
    n = len(valid_titles)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if sim_matrix[i, j] >= threshold:
                union(i, j)

    # Raggruppa per root
    clusters_map: dict[int, list[str]] = {}
    for i in range(n):
        root = find(i)
        clusters_map.setdefault(root, []).append(valid_titles[i])

    # Ritorna solo cluster con 2+ mercati
    return [titles_list for titles_list in clusters_map.values() if len(titles_list) >= 2]


def discover_uncovered_markets(
    active_markets: list[str],
    all_markets: list[str],
    min_similarity: float = 0.5,
) -> list[str]:
    """
    Trova mercati weather (o affini) che il bot non sta coprendo.

    Logica: per ogni mercato in all_markets, calcola la max similarity
    con i mercati attivi. Se >= min_similarity ma il mercato non e' tra
    gli attivi, e' un candidato "uncovered" — semanticamente simile
    a quelli che gia' tradiamo ma non ancora coperto.

    Args:
        active_markets: titoli dei mercati su cui il bot ha posizioni/trade
        all_markets: tutti i mercati disponibili su Polymarket
        min_similarity: soglia minima di similarita' per considerare un mercato affine

    Returns:
        Lista di titoli di mercati non coperti ma potenzialmente interessanti,
        ordinati per max_similarity decrescente
    """
    if not active_markets or not all_markets:
        return []

    # Filtra candidati non gia' attivi
    active_set = set(active_markets)
    candidates = [m for m in all_markets if m not in active_set]

    if not candidates:
        return []

    # Embed tutto
    active_embeddings = embed_batch(active_markets)
    candidate_embeddings = embed_batch(candidates)

    # Filtra embedding validi
    valid_active = [(i, emb) for i, emb in enumerate(active_embeddings) if emb]
    if not valid_active:
        return []

    active_matrix = np.array([e for _, e in valid_active])

    uncovered = []
    for j, emb in enumerate(candidate_embeddings):
        if not emb:
            continue
        cand_vec = np.array(emb)
        # Max similarity con qualsiasi mercato attivo
        norms_active = np.linalg.norm(active_matrix, axis=1)
        norm_cand = np.linalg.norm(cand_vec)
        denom = norms_active * norm_cand + 1e-10
        sims = active_matrix @ cand_vec / denom
        max_sim = float(np.max(sims))
        if max_sim >= min_similarity:
            uncovered.append((candidates[j], max_sim))

    # Ordina per similarita' decrescente
    uncovered.sort(key=lambda x: x[1], reverse=True)
    return [title for title, _ in uncovered]


def get_cache_stats() -> dict:
    """Ritorna statistiche della cache embedding."""
    _cache.clear_expired()
    return {
        "cached_embeddings": _cache.size,
        "ttl_seconds": _cache.ttl,
    }
