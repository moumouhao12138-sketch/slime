from __future__ import annotations

import unittest

from slime_cairn.protocol.contracts import validate_reason_payload


class ReasonSourceValidationTests(unittest.TestCase):
    def test_hint_id_is_rejected_as_a_fact_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown Fact IDs"):
            validate_reason_payload(
                {
                    "accepted": True,
                    "data": {
                        "intents": [
                            {"from": ["hint_123"], "description": "continue from marker"}
                        ]
                    },
                },
                open_intents_empty=True,
                max_intents=2,
                known_fact_ids={"fact_123"},
            )

    def test_known_fact_sources_are_retained(self) -> None:
        kind, data = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [
                        {"from": ["fact_123"], "description": "continue from evidence"}
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
            known_fact_ids={"fact_123"},
        )
        self.assertEqual(kind, "intents")
        self.assertEqual(data[0]["from"], ["fact_123"])


if __name__ == "__main__":
    unittest.main()
