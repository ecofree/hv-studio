# -*- coding: utf-8 -*-
"""Pin the image retry allowance: failures must not starve the plates behind them.

Why: xlc-01 2026-10-13, first image run spent its whole 27-slot budget and lost
4 plates -- two 400 policy rejections plus their retries consumed the success
quota, and BudgetExceeded blocked everything behind them. Retries are real
spend (they stay in `used`), but they draw from a separate, explicit cap.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"D:\hv")
from app.services.provider_budget import ProviderBudget, BudgetExceeded, for_run

pytestmark = pytest.mark.production


def test_retries_draw_from_their_own_cap(tmp_path):
    budget = ProviderBudget(tmp_path / "b.json", image_requests=2, image_retries=2)
    budget.reserve("image", "a")
    budget.reserve("image", "b")
    # Success quota is spent, but the explicit retry allowance still admits
    # two more attempts instead of starving the queue.
    budget.reserve("image", "c-retry")
    budget.reserve("image", "d-retry")
    with pytest.raises(BudgetExceeded, match="image_requests.*2\\+2 retries"):
        budget.reserve("image", "e-too-many")
    assert budget.snapshot()["used"]["image_requests"] == 4


def test_no_retry_allowance_keeps_the_old_behavior(tmp_path):
    """Ledgers without the new key behave exactly as before: every attempt,
    success or failure, counts against image_requests."""
    budget = ProviderBudget(tmp_path / "b.json", image_requests=1)
    assert "image_retries" not in budget.snapshot()["limits"]
    budget.reserve("image", "only")
    with pytest.raises(BudgetExceeded, match="image_requests.*1/1"):
        budget.reserve("image", "starved")
    # Old ledgers on disk (no image_retries key) reopen the same way.
    reopened = ProviderBudget(tmp_path / "b.json", image_requests=1)
    assert "image_retries" not in reopened.snapshot()["limits"]


def test_for_run_threads_the_retry_allowance(tmp_path):
    budget = for_run(tmp_path, ["ep", "retry"], dict(max_images=3, max_image_retries=2))
    assert budget.snapshot()["limits"] == dict(
        tts_requests=0, characters=0, image_requests=3, image_retries=2)
    for i in range(5):
        budget.reserve("image", f"r{i}")
    with pytest.raises(BudgetExceeded):
        budget.reserve("image", "r5")


def test_negative_retry_allowance_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid budget limits"):
        ProviderBudget(tmp_path / "b.json", image_requests=1, image_retries=-1)
