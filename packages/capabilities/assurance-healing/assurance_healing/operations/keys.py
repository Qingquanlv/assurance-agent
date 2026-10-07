"""Domain key derivation for coverage repair."""

from __future__ import annotations

import hashlib


def mint_coverage_attempt_token(
    *,
    change_id: str,
    attempt: int,
    test_tree_sha256: str,
    product_tree_sha256: str,
    declaration_tree_sha256: str,
) -> str:
    preimage = f"{change_id}:{attempt}:{test_tree_sha256}:{product_tree_sha256}:{declaration_tree_sha256}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]
