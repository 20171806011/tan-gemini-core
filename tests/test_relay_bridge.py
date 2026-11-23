import unittest

from runtime.relay_bridge import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    DEFAULT_BASE_TREASURY,
    validate_quote_safety,
)


class TestRelayBridgeSafety(unittest.TestCase):
    def _sample_quote(self, out_amount: str = "3.97356", recipient: str = DEFAULT_BASE_TREASURY):
        return {
            "details": {
                "recipient": recipient,
                "timeEstimate": 1,
                "currencyOut": {
                    "amountFormatted": out_amount,
                    "currency": {
                        "chainId": BASE_CHAIN_ID,
                        "address": BASE_USDC_ADDRESS,
                    },
                },
            },
            "steps": [{"id": "deposit"}],
        }

    def test_valid_quote_passes(self) -> None:
        summary = validate_quote_safety(self._sample_quote(), amount_usdc=4.0, max_fee_usd=0.15)
        self.assertTrue(summary["safe"])
        self.assertAlmostEqual(summary["implied_fee_usd"], 0.02644, places=4)

    def test_rejects_poisoned_recipient_and_excessive_fee(self) -> None:
        with self.assertRaises(ValueError):
            validate_quote_safety(
                self._sample_quote(recipient="0x000000000000000000000000000000000000dead"),
                amount_usdc=4.0,
            )
        with self.assertRaises(ValueError):
            validate_quote_safety(self._sample_quote(out_amount="3.50"), amount_usdc=4.0, max_fee_usd=0.15)


if __name__ == "__main__":
    unittest.main()
