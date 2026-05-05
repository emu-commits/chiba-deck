import logging
import re
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class IntentClassifier:
    def __init__(self, embeddings_path: str):
        self._path = Path(embeddings_path)
        self._loaded = False
        self._labels: list[str] = []
        self._matrix: np.ndarray | None = None
        self._vectorizer = None
        self._default_threshold = 0.40

    # TF-IDF cosine sims are naturally lower than GloVe — use a method-aware threshold
    _METHOD_THRESHOLD = {"tfidf": 0.35, "glove": 0.65}

    def load(self):
        try:
            data = np.load(self._path, allow_pickle=True)
            self._labels = list(data["labels"])
            self._matrix = data["matrix"]
            method = str(data["method"][0])
            self._default_threshold = self._METHOD_THRESHOLD.get(method, 0.40)

            vocab = list(data["vocab"])
            idf = data["idf"]
            self._vectorizer = self._rebuild_vectorizer(vocab, idf)
            self._loaded = True
            log.info(f"classifier loaded: {len(self._labels)} intents, dim={self._matrix.shape[1]}, method={method}")
        except Exception as e:
            log.error(f"classifier load failed: {e} — falling back to keyword match")
            self._loaded = False

    def _rebuild_vectorizer(self, vocab: list[str], idf: np.ndarray):
        from sklearn.feature_extraction.text import TfidfVectorizer

        vocab_dict = {w: i for i, w in enumerate(vocab)}
        vec = TfidfVectorizer(ngram_range=(1, 2), vocabulary=vocab_dict)
        vec.idf_ = idf
        return vec

    def _embed(self, text: str) -> np.ndarray:
        v = self._vectorizer.transform([text]).toarray()[0]
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else v

    def classify(self, text: str) -> tuple[str, float]:
        text = re.sub(r'^\?', '', text.lower().strip())

        if not self._loaded or self._matrix is None:
            return self._keyword_fallback(text)

        try:
            vec = self._embed(text)
            sims = self._matrix @ vec
            best = int(np.argmax(sims))
            return self._labels[best], float(sims[best])
        except Exception as e:
            log.debug(f"classify error: {e}")
            return self._keyword_fallback(text)

    @property
    def threshold(self) -> float:
        return self._default_threshold

    def _keyword_fallback(self, text: str) -> tuple[str, float]:
        keywords = {
            "balance": "check_balance",
            "pay": "send_payment",
            "send": "send_payment",
            "wiki": "query_wiki",
            "look": "query_wiki",
            "what is": "query_wiki",
            "market": "list_market",
            "sale": "list_market",
            "buy": "buy_item",
            "sell": "post_listing",
            "nodes": "list_nodes",
            "online": "list_nodes",
            "ping": "ping_node",
            "history": "check_history",
            "help": "get_help",
            "status": "show_status",
        }
        for kw, intent in keywords.items():
            if kw in text:
                return intent, 0.7
        return "get_help", 0.3
