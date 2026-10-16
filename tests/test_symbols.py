"""Tests for symbol normalization and the no-data routing sentinel."""

import unittest

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import crypto_base, futures_base, normalize_symbol


@pytest.mark.unit
class TestNormalizeSymbol(unittest.TestCase):
    def test_plain_equities_unchanged(self):
        for sym in ("AAPL", "MSFT", "TSM", "BRK.B", "0700.HK", "^GSPC", "GC=F"):
            self.assertEqual(normalize_symbol(sym), sym)

    def test_lowercases_are_upper(self):
        self.assertEqual(normalize_symbol("aapl"), "AAPL")
        self.assertEqual(normalize_symbol("  msft  "), "MSFT")

    def test_metal_aliases_map_to_futures(self):
        self.assertEqual(normalize_symbol("XAUUSD"), "GC=F")
        self.assertEqual(normalize_symbol("XAUUSD+"), "GC=F")   # broker CFD suffix
        self.assertEqual(normalize_symbol("xauusd+"), "GC=F")
        self.assertEqual(normalize_symbol("GOLD"), "GC=F")
        self.assertEqual(normalize_symbol("XAGUSD"), "SI=F")

    def test_energy_and_index_aliases(self):
        self.assertEqual(normalize_symbol("USOIL"), "CL=F")
        self.assertEqual(normalize_symbol("SPX500"), "^GSPC")
        self.assertEqual(normalize_symbol("NAS100"), "^NDX")
        self.assertEqual(normalize_symbol("US30"), "^DJI")

    def test_forex_pairs_get_x_suffix(self):
        self.assertEqual(normalize_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(normalize_symbol("GBPJPY"), "GBPJPY=X")
        self.assertEqual(normalize_symbol("eurusd"), "EURUSD=X")

    def test_crypto_pairs_get_dash_usd(self):
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(normalize_symbol("ETHUSD"), "ETH-USD")

    def test_crypto_bases_yahoo_disambiguates(self):
        # Yahoo does not publish these bases as BASE-USD: a colliding equity
        # ticker forces a numeric id, and TAO only lists wrapped. Every row is
        # the ticker whose Yahoo name matches the coin (see symbols.py).
        self.assertEqual(normalize_symbol("PEPE-USD"), "PEPE24478-USD")
        self.assertEqual(normalize_symbol("pepeusdt"), "PEPE24478-USD")
        self.assertEqual(normalize_symbol("UNI-USD"), "UNI7083-USD")
        self.assertEqual(normalize_symbol("SUI-USD"), "SUI20947-USD")
        self.assertEqual(normalize_symbol("HYPE-USD"), "HYPE32196-USD")
        self.assertEqual(normalize_symbol("PUMP-USD"), "PUMP33991-USD")
        self.assertEqual(normalize_symbol("GTC-USD"), "GTC10052-USD")
        self.assertEqual(normalize_symbol("ZRO-USD"), "ZRO26997-USD")
        self.assertEqual(normalize_symbol("TAO-USD"), "WTAO-USD")

    def test_crypto_disambiguation_is_idempotent(self):
        # A resolved ticker must survive a second pass, and a liquid base
        # Yahoo already lists plainly must not be redirected.
        self.assertEqual(normalize_symbol("PEPE24478-USD"), "PEPE24478-USD")
        self.assertEqual(normalize_symbol("WTAO-USD"), "WTAO-USD")
        self.assertEqual(normalize_symbol("MOVR-USD"), "MOVR-USD")
        self.assertEqual(normalize_symbol("NEAR-USD"), "NEAR-USD")

    def test_six_letter_non_currency_left_alone(self):
        # GOOGLE-style 6-letter tickers that aren't two currency codes
        # must not be mangled into a fake forex pair.
        self.assertEqual(normalize_symbol("ABCDEF"), "ABCDEF")

    def test_empty_input_passthrough(self):
        self.assertEqual(normalize_symbol(""), "")

    def test_hk_five_digit_code_repadded_to_four(self):
        # HKEX lists up to 5-digit codes; Yahoo only accepts 4 (#957).
        self.assertEqual(normalize_symbol("09992.HK"), "9992.HK")
        self.assertEqual(normalize_symbol("00700.HK"), "0700.HK")
        self.assertEqual(normalize_symbol("00001.HK"), "0001.HK")

    def test_hk_four_digit_code_unchanged(self):
        self.assertEqual(normalize_symbol("0700.HK"), "0700.HK")
        self.assertEqual(normalize_symbol("9992.HK"), "9992.HK")
        self.assertEqual(normalize_symbol("80737.HK"), "80737.HK")

    def test_hk_short_code_padded_to_four(self):
        self.assertEqual(normalize_symbol("700.HK"), "0700.HK")

    def test_hk_code_case_insensitive_suffix(self):
        self.assertEqual(normalize_symbol("09992.hk"), "9992.HK")

    def test_shanghai_sh_suffix_maps_to_yahoo_ss(self):
        self.assertEqual(normalize_symbol("600519.sh"), "600519.SS")
        self.assertEqual(normalize_symbol("600519.SS"), "600519.SS")


@pytest.mark.unit
class TestNoMarketDataError(unittest.TestCase):
    def test_message_includes_resolution(self):
        err = NoMarketDataError("XAUUSD+", "GC=F", "no rows")
        self.assertIn("XAUUSD+", str(err))
        self.assertIn("GC=F", str(err))
        self.assertEqual(err.symbol, "XAUUSD+")
        self.assertEqual(err.canonical, "GC=F")

    def test_canonical_defaults_to_symbol(self):
        err = NoMarketDataError("FOOBAR")
        self.assertEqual(err.canonical, "FOOBAR")


@pytest.mark.unit
class TestCryptoBase(unittest.TestCase):
    def test_resolves_known_crypto_forms(self):
        for raw in ("BTC-USD", "BTCUSD", "btc-usdt", "BTC-USDC", "BTCUSD+"):
            self.assertEqual(crypto_base(raw), "BTC")
        self.assertEqual(crypto_base("ETH-USD"), "ETH")
        self.assertEqual(crypto_base("sol-usd"), "SOL")

    def test_non_crypto_returns_none(self):
        # Plain equities, class shares, and real tickers that alias elsewhere
        # (GOLD -> gold future on the Yahoo path) must NOT read as crypto.
        for raw in ("AAPL", "BRK-B", "GOLD", "XYZ-USD", "EURUSD", "", None):
            self.assertIsNone(crypto_base(raw))

    def test_futures_base_accepts_generic_contract_tickers(self):
        for raw in ("NEAR-USD", "SUI-USD", "QNT-USD", "abc-usdt", "BTC-USDC"):
            self.assertEqual(futures_base(raw), raw.strip().upper().split("-")[0])

    def test_futures_base_rejects_malformed_tickers(self):
        for raw in ("NEAR", "NEAR/USD", "NEAR-", "-USD", "NEAR-EUR", "NEAR-USD+"):
            self.assertIsNone(futures_base(raw))


if __name__ == "__main__":
    unittest.main()
