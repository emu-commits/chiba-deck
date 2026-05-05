#!/usr/bin/env python3
"""
Build embeddings/intent_matrix.npz from canonical intent phrases.

Default: uses TF-IDF (no download needed, works offline).
Optional: --glove downloads Stanford GloVe 6B (822MB) for richer embeddings.

Run once at dev time. Commit the resulting .npz file.
Normal pip install never touches this script.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from chiba.nlp.intents import INTENTS


def build_tfidf() -> tuple:
    from sklearn.feature_extraction.text import TfidfVectorizer

    all_phrases, labels = [], []
    for intent, phrases in INTENTS.items():
        for phrase in phrases:
            all_phrases.append(phrase)
            labels.append(intent)

    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    X = vec.fit_transform(all_phrases).toarray()

    intent_names = list(INTENTS.keys())
    dim = X.shape[1]
    matrix = np.zeros((len(intent_names), dim), dtype=np.float32)

    for i, intent in enumerate(intent_names):
        idxs = [j for j, l in enumerate(labels) if l == intent]
        row = X[idxs].mean(axis=0)
        norm = np.linalg.norm(row)
        matrix[i] = (row / norm) if norm > 0 else row

    vocab = vec.get_feature_names_out()
    idf = vec.idf_.astype(np.float32)
    return intent_names, matrix, vocab, idf, "tfidf"


def build_glove(glove_cache: Path) -> tuple:
    import urllib.request
    import zipfile, io

    url = "https://nlp.stanford.edu/data/glove.6B.zip"
    if not glove_cache.exists():
        print(f"Downloading GloVe from {url} (~822MB)...")
        urllib.request.urlretrieve(url, glove_cache)

    print("Loading glove.6B.50d.txt...")
    glove: dict[str, np.ndarray] = {}
    with zipfile.ZipFile(glove_cache) as z:
        with z.open("glove.6B.50d.txt") as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                parts = line.strip().split()
                glove[parts[0]] = np.array(parts[1:], dtype=np.float32)

    def embed(text: str) -> np.ndarray:
        vecs = [glove[t] for t in text.lower().split() if t in glove]
        if not vecs:
            return np.zeros(50, dtype=np.float32)
        v = np.mean(vecs, axis=0)
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else v

    intent_names = list(INTENTS.keys())
    matrix = np.zeros((len(intent_names), 50), dtype=np.float32)
    for i, (intent, phrases) in enumerate(INTENTS.items()):
        row = np.mean([embed(p) for p in phrases], axis=0)
        norm = np.linalg.norm(row)
        matrix[i] = row / norm if norm > 0 else row

    return intent_names, matrix, None, None, "glove"


def main():
    parser = argparse.ArgumentParser(description="Build intent embeddings matrix")
    parser.add_argument("--glove", action="store_true", help="use GloVe 6B (downloads ~822MB)")
    parser.add_argument("--glove-cache", default="/tmp/glove.6B.zip", help="local GloVe zip path")
    args = parser.parse_args()

    out_dir = ROOT / "embeddings"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "intent_matrix.npz"

    if args.glove:
        try:
            print("Building with GloVe 50d...")
            intent_names, matrix, vocab, idf, method = build_glove(Path(args.glove_cache))
        except Exception as e:
            print(f"GloVe failed ({e}), falling back to TF-IDF")
            intent_names, matrix, vocab, idf, method = build_tfidf()
    else:
        print("Building with TF-IDF (offline, no download)...")
        intent_names, matrix, vocab, idf, method = build_tfidf()

    save = {
        "labels": np.array(intent_names),
        "matrix": matrix,
        "method": np.array([method]),
    }
    if vocab is not None:
        save["vocab"] = vocab
    if idf is not None:
        save["idf"] = idf

    np.savez_compressed(out_path, **save)
    print(f"Saved {out_path}  method={method}  shape={matrix.shape}")
    print("Commit this file — users never need to regenerate it.")


if __name__ == "__main__":
    main()
