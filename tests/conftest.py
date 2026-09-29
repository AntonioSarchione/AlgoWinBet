from datetime import timedelta

import pytest

from algowinbet.config import Config
from algowinbet.engine import Engine
from algowinbet.providers import MockProvider


@pytest.fixture(scope="session")
def provider():
    return MockProvider(seed=11, open_noise=0.0, close_noise=0.0)


@pytest.fixture(scope="session")
def analysis(provider):
    eng = Engine(provider, Config())
    cutoff = provider.as_of
    return eng.analyze(None, cutoff, cutoff + timedelta(days=8), cutoff)
