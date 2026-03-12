"""Tests for write_gateway.py — rate limiting and pipeline logic."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from write_gateway import RateLimiter, _compute_checksum


def test_rate_limiter_allows_within_limit():
    rl = RateLimiter()
    for i in range(5):
        assert rl.check("test", 5, 60) is True

def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter()
    for i in range(5):
        rl.check("test_block", 5, 60)
    assert rl.check("test_block", 5, 60) is False

def test_rate_limiter_separate_scopes():
    rl = RateLimiter()
    for i in range(5):
        rl.check("scope_a", 5, 60)
    # Different scope should still allow
    assert rl.check("scope_b", 5, 60) is True

def test_checksum_deterministic():
    c1 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    c2 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    assert c1 == c2

def test_checksum_changes_with_data():
    c1 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    c2 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 2})
    assert c1 != c2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
