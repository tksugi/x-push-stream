"""Keep SDK unit tests offline; standalone generator tests use their real class."""

import pytest

from .fakes import FakeTransactionGenerator


@pytest.fixture(autouse=True)
def offline_sdk_transaction_sources(monkeypatch):
    monkeypatch.setattr(
        "xpushstream.x_adapter.TransactionIdGenerator", FakeTransactionGenerator
    )
