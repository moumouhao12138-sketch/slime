from __future__ import annotations

import unittest

from slime_cairn.protocol.contracts import (
    extract_explore_submissions,
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)


class CairnContractsTests(unittest.TestCase):
    def test_parser_accepts_fenced_json_inside_cli_noise(self) -> None:
        payload = parse_json_output('progress\n```json\n{"accepted": true, "data": {}}\n```\ndone')
        self.assertEqual(payload, {"accepted": True, "data": {}})

    def test_bootstrap_execute_accepts_wrapped_and_bare_forms(self) -> None:
        wrapped_kind, wrapped = validate_bootstrap_execute_payload(
            {
                "accepted": True,
                "data": {
                    "fact": {"description": "  verified entry point  "},
                    "complete": {"description": "  goal proven  "},
                },
            }
        )
        bare_kind, bare = validate_bootstrap_execute_payload(
            {
                "fact": {"description": "verified entry point"},
                "complete": {"description": "goal proven"},
            }
        )

        self.assertEqual(wrapped_kind, "complete")
        self.assertEqual(
            wrapped,
            {"fact_description": "verified entry point", "complete_description": "goal proven"},
        )
        self.assertEqual(bare_kind, "complete")
        self.assertEqual(bare, {"fact_description": "verified entry point", "complete_description": "goal proven"})

    def test_bootstrap_execute_rejects_partial_success(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete"):
            validate_bootstrap_execute_payload(
                {"accepted": True, "data": {"fact": {"description": "partial evidence"}}}
            )

    def test_rejected_contracts_normalize_without_writing_data(self) -> None:
        rejected = {"accepted": False, "reason": "model declined"}

        self.assertEqual(validate_bootstrap_execute_payload(rejected), ("rejected", None))
        self.assertEqual(validate_bootstrap_conclude_payload(rejected), ("rejected", None))
        self.assertEqual(validate_explore_payload(rejected), ("rejected", None))
        self.assertEqual(
            validate_reason_payload(rejected, open_intents_empty=False, max_intents=2),
            ("rejected", None),
        )

    def test_bootstrap_conclude_accepts_fact_and_ignores_optional_complete(self) -> None:
        kind, description = validate_bootstrap_conclude_payload(
            {
                "accepted": True,
                "data": {
                    "fact": {"description": "  confirmed partial result  "},
                    "complete": {"description": "must not complete here"},
                },
            }
        )

        self.assertEqual(kind, "fact")
        self.assertEqual(description, "confirmed partial result")

    def test_explore_accepts_wrapped_and_bare_fact_forms(self) -> None:
        self.assertEqual(
            validate_explore_payload({"accepted": True, "data": {"description": "  discovered proof  "}}),
            ("fact", "discovered proof"),
        )
        self.assertEqual(validate_explore_payload({"description": "discovered proof"}), ("fact", "discovered proof"))

    def test_explore_normalizes_explicit_candidates_without_parsing_description(self) -> None:
        payload = {
            "accepted": True,
            "data": {
                "description": "confirmed the exact benchmark value",
                "submissions": [" flag{one} ", "flag{one}", "flag{two}"],
            },
        }

        self.assertEqual(
            validate_explore_payload(payload),
            ("fact", "confirmed the exact benchmark value"),
        )
        self.assertEqual(extract_explore_submissions(payload), ["flag{one}", "flag{two}"])

    def test_explore_submissions_preserve_arbitrary_candidate_formats(self) -> None:
        payload = {
            "accepted": True,
            "data": {
                "description": "confirmed two exact benchmark values",
                "submissions": [
                    " TOKEN-42 ",
                    "88341566-f0b6-4b1a-bde8-e45ba8ed52f8",
                    "TOKEN-42",
                ],
            },
        }

        self.assertEqual(
            extract_explore_submissions(payload),
            ["TOKEN-42", "88341566-f0b6-4b1a-bde8-e45ba8ed52f8"],
        )

    def test_explore_rejects_non_contract_planning_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepted"):
            validate_explore_payload(parse_json_output('{"plan": "continue investigating"}'))

    def test_contracts_reject_example_placeholders_as_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            validate_explore_payload(
                {"accepted": True, "data": {"description": "latest confirmed incremental facts"}}
            )
        with self.assertRaisesRegex(ValueError, "placeholder"):
            validate_bootstrap_conclude_payload(
                {"accepted": True, "data": {"fact": {"description": "..."}}}
            )
        with self.assertRaisesRegex(ValueError, "placeholder"):
            validate_bootstrap_execute_payload(
                {
                    "accepted": True,
                    "data": {
                        "fact": {"description": "confirmed key objective results"},
                        "complete": {"description": "goal proven"},
                    },
                }
            )
        with self.assertRaisesRegex(ValueError, "placeholder"):
            validate_reason_payload(
                {
                    "accepted": True,
                    "data": {
                        "intents": [
                            {
                                "from": ["f001"],
                                "description": "independent high-value exploration direction",
                            }
                        ]
                    },
                },
                open_intents_empty=True,
                max_intents=2,
            )

    def test_reason_accepts_legacy_singular_intent_and_caps_intents(self) -> None:
        singular_kind, singular = validate_reason_payload(
            {"intent": {"from": ["f001"], "description": "legacy direction"}},
            open_intents_empty=True,
            max_intents=2,
        )
        capped_kind, capped = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [
                        {"from": ["f001"], "description": "one"},
                        {"from": ["f002"], "description": "two"},
                        {"from": ["f003"], "description": "three"},
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )

        self.assertEqual(singular_kind, "intents")
        self.assertEqual(singular, [{"from": ["f001"], "description": "legacy direction"}])
        self.assertEqual(capped_kind, "intents")
        self.assertEqual(capped, [{"from": ["f001"], "description": "one"}, {"from": ["f002"], "description": "two"}])

    def test_reason_accepts_objective_as_description_alias(self) -> None:
        kind, intents = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [
                        {
                            "from": ["f001"],
                            "objective": "  map the diagnostic panel  ",
                            "kind": "explore",
                            "target_entity": "panel",
                        }
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )

        self.assertEqual(kind, "intents")
        self.assertEqual(
            intents,
            [
                {
                    "from": ["f001"],
                    "objective": "  map the diagnostic panel  ",
                    "description": "map the diagnostic panel",
                    "kind": "explore",
                    "target_entity": "panel",
                }
            ],
        )

    def test_reason_description_takes_priority_over_objective(self) -> None:
        kind, intents = validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [
                        {
                            "from": ["f001"],
                            "description": "canonical direction",
                            "objective": "alias direction",
                        }
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )

        self.assertEqual(kind, "intents")
        self.assertEqual(intents[0]["description"], "canonical direction")

    def test_reason_rejects_invalid_objective_alias(self) -> None:
        for objective in ("   ", 42):
            with self.subTest(objective=objective):
                with self.assertRaisesRegex(ValueError, "intent\\[0\\]\\.description"):
                    validate_reason_payload(
                        {
                            "accepted": True,
                            "data": {"intents": [{"from": ["f001"], "objective": objective}]},
                        },
                        open_intents_empty=True,
                        max_intents=2,
                    )

    def test_reason_noop_requires_an_existing_open_intent(self) -> None:
        self.assertEqual(
            validate_reason_payload({"accepted": True, "data": {}}, open_intents_empty=False, max_intents=2),
            ("noop", None),
        )
        with self.assertRaisesRegex(ValueError, "intents is required"):
            validate_reason_payload({"accepted": True, "data": {}}, open_intents_empty=True, max_intents=2)

    def test_reason_complete_and_intents_cannot_coexist(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot coexist"):
            validate_reason_payload(
                {
                    "accepted": True,
                    "data": {
                        "complete": {"from": ["f001"], "description": "done"},
                        "intents": [{"from": ["f001"], "description": "should not be here"}],
                    },
                },
                open_intents_empty=False,
                max_intents=2,
            )


if __name__ == "__main__":
    unittest.main()
