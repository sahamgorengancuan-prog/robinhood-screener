"""Interface classification from runtime bytecode.

Costs no RPC call of its own — the code is already fetched for the risk scan —
and answers a question the symbol cannot: is this contract a token at all?
"""

from __future__ import annotations

import pytest

from app.clients.rh_node import INTERFACE_SELECTORS, classify_interface


def test_uniswap_v2_pair_is_an_lp_share():
    # token0() and token1() both present
    assert classify_interface("0x6080...0dfe1681...d21220a7...0902f1ac") == "LP_SHARE"


def test_one_half_of_the_pair_interface_is_not_enough():
    """token0() alone appears in routers and helpers that are not pair tokens."""
    assert classify_interface("0x6080...0dfe1681...") == "PLAIN"


def test_erc4626_vault_is_a_vault_share():
    assert classify_interface("0x6080...38d52e0f...") == "VAULT_SHARE"


def test_plain_erc20_is_plain():
    assert classify_interface("0x6080...18160ddd...70a08231...95d89b41") == "PLAIN"


@pytest.mark.parametrize("value", ["", "0x", None])
def test_missing_code_does_not_crash_or_misclassify(value):
    assert classify_interface(value) == "PLAIN"


def test_classification_is_case_insensitive():
    assert classify_interface("0X6080...0DFE1681...D21220A7") == "LP_SHARE"


def test_selectors_are_the_documented_four_byte_signatures():
    """Pinned so a typo cannot silently stop matching every pair on the chain."""
    assert INTERFACE_SELECTORS["LP_SHARE"] == ("0dfe1681", "d21220a7")
    assert INTERFACE_SELECTORS["VAULT_SHARE"] == ("38d52e0f",)


def test_the_scan_reports_the_kind_into_contract_flags():
    """The gate reads the kind off contract_flags, so the scan must put it there."""
    import inspect

    from app.clients.rh_node import RobinhoodNodeClient

    src = inspect.getsource(RobinhoodNodeClient.contract_risk_flags)
    assert "classify_interface" in src
    assert "NOT_A_TRADEABLE_TOKEN" in src
