import struct
import unittest

from runtime.relay_bridge import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    DEFAULT_BASE_TREASURY,
    DEFAULT_SOLANA_WALLET,
    RELAY_SOLANA_DEPOSITORY_PROGRAM,
    SOLANA_USDC_MINT,
    validate_quote_safety,
    validate_solana_instructions,
)


class TestRelayBridgeSafety(unittest.TestCase):
    def _sample_instruction(
        self,
        amount_micro: int = 4_000_000,
        program_id: str = RELAY_SOLANA_DEPOSITORY_PROGRAM,
        signer: str = DEFAULT_SOLANA_WALLET,
        mint: str = SOLANA_USDC_MINT,
    ):
        raw = bytes.fromhex("0b9c60da27a3b413") + struct.pack("<Q", amount_micro) + b"\x11" * 32
        return {
            "programId": program_id,
            "keys": [
                {"pubkey": signer, "isSigner": True, "isWritable": True},
                {"pubkey": mint, "isSigner": False, "isWritable": False},
            ],
            "data": raw.hex(),
        }

    def _sample_quote(
        self,
        out_amount: str = "3.97356",
        recipient: str = DEFAULT_BASE_TREASURY,
        instructions=None,
    ):
        step = {"id": "deposit"}
        if instructions is not None:
            step["items"] = [
                {
                    "data": {
                        "instructions": instructions,
                        "addressLookupTableAddresses": ["Hm9fUgcn7qwDaiNTFiGh6pNtVATgnaRcmK6Bbx6EMZfP"],
                    },
                    "check": {"endpoint": "/intents/status?requestId=0xabc123", "method": "GET"},
                }
            ]
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
            "steps": [step],
        }

    def test_valid_quote_and_instructions_pass(self) -> None:
        quote = self._sample_quote(instructions=[self._sample_instruction(4_000_000)])
        summary = validate_quote_safety(quote, amount_usdc=4.0, max_fee_usd=0.15, require_instructions=True)
        self.assertTrue(summary["safe"])
        self.assertAlmostEqual(summary["implied_fee_usd"], 0.02644, places=4)
        self.assertEqual(summary["solana_instruction_check"]["verified_micro_usdc"], 4_000_000)
        self.assertEqual(summary["solana_instruction_check"]["request_id"], "0xabc123")

    def test_rejects_poisoned_recipient_and_excessive_fee(self) -> None:
        with self.assertRaises(ValueError):
            validate_quote_safety(
                self._sample_quote(recipient="0x000000000000000000000000000000000000dead"),
                amount_usdc=4.0,
            )
        with self.assertRaises(ValueError):
            validate_quote_safety(self._sample_quote(out_amount="3.50"), amount_usdc=4.0, max_fee_usd=0.15)

    def test_rejects_tampered_solana_instruction(self) -> None:
        # Tampered amount (10 USDC in bytes vs 4 USDC requested)
        q_amt = self._sample_quote(instructions=[self._sample_instruction(10_000_000)])
        with self.assertRaises(ValueError):
            validate_solana_instructions(q_amt, amount_usdc=4.0)

        # Disallowed programId (e.g. arbitrary SystemProgram transfer)
        q_prog = self._sample_quote(
            instructions=[
                self._sample_instruction(4_000_000),
                self._sample_instruction(4_000_000, program_id="11111111111111111111111111111111"),
            ]
        )
        with self.assertRaises(ValueError):
            validate_solana_instructions(q_prog, amount_usdc=4.0)

        # Missing expected signer
        q_signer = self._sample_quote(
            instructions=[self._sample_instruction(4_000_000, signer="11111111111111111111111111111111")]
        )
        with self.assertRaises(ValueError):
            validate_solana_instructions(q_signer, amount_usdc=4.0)


if __name__ == "__main__":
    unittest.main()

