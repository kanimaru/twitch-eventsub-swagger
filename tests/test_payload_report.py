import unittest

from tests.schemas import notification, subscription_schemas
from validate import PayloadReport, known_error_key, payload_parts, source_kind


class SourceKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(source_kind("twitch-cli channel.follow v2"), "twitch-cli")
        self.assertEqual(source_kind("fixtures/follow.json"), "fixture")
        self.assertEqual(source_kind("https://dev.twitch.tv/docs/eventsub/reference/channel#channel-follow"), "docs")


class PayloadPartsTest(unittest.TestCase):
    def test_single_event(self):
        self.assertEqual(payload_parts(notification({"x": 1}, condition={"c": 2})),
                         [("condition", {"c": 2}), ("event", {"x": 1})])

    def test_batched_events_are_indexed(self):
        payload = {"subscription": {}, "events": [{"a": 1}, {"b": 2}]}
        self.assertEqual(payload_parts(payload), [("condition", None), ("events[0]", {"a": 1}), ("events[1]", {"b": 2})])

    def test_missing_event_is_none(self):
        self.assertEqual(payload_parts({"subscription": {"condition": {}}}), [("condition", {}), ("events[0]", None)])


class KnownErrorKeyTest(unittest.TestCase):
    def test_array_indices_are_collapsed(self):
        self.assertEqual(known_error_key("docs", ("channel.prediction.end", "1"), "event.outcomes[3].top_predictors[10].user_id"),
                         ("docs", "channel.prediction.end", "1", "event.outcomes[].top_predictors[].user_id"))


class PayloadReportTest(unittest.TestCase):
    DOCS = "https://dev.twitch.tv/docs/eventsub/reference/channel#example"
    CLI = "twitch-cli channel.example v1"

    def report(self, known_errors=None):
        return PayloadReport(subscription_schemas(), known_errors or {})

    def test_matching_payload_passes(self):
        report = self.report()
        report.validate(self.DOCS, notification({"broadcaster_user_id": "1", "viewers": 3, "is_live": True}))
        self.assertEqual(report.failures(), [])
        self.assertEqual(report.checked, 2)

    def test_mismatch_fails_with_source_component_and_path(self):
        report = self.report()
        report.validate(self.DOCS, notification({"viewers": "3"}))
        self.assertEqual(report.failures(),
                         [f"{self.DOCS} (ExampleEvent): channel.example v1 event.viewers is string, schema says integer"])

    def test_documented_key_overrides_the_payload_subscription(self):
        report = self.report()
        report.validate(self.DOCS, notification({"viewers": 1}, subscription_type="copied.from.neighbour"),
                        ("channel.example", "1"))
        self.assertEqual(report.failures(), [])

    def test_unknown_subscription_is_reported_once_per_part(self):
        report = self.report()
        for _ in range(2):
            report.validate(self.CLI, notification({"x": 1}, subscription_type="channel.unknown"))
        self.assertEqual(report.failures(), ["no schema for the payload of channel.unknown v1 condition",
                                             "no schema for the payload of channel.unknown v1 event"])
        self.assertEqual(report.checked, 0)

    def test_known_error_is_not_a_failure(self):
        known = {("twitch-cli", "channel.example", "1", "event.viewers"): "the CLI sends a string"}
        report = self.report(known)
        report.validate(self.CLI, notification({"viewers": "3"}))
        self.assertEqual(report.failures(), [])

    def test_known_error_only_covers_its_own_source(self):
        known = {("twitch-cli", "channel.example", "1", "event.viewers"): "the CLI sends a string"}
        report = self.report(known)
        report.validate(self.CLI, notification({"viewers": "3"}))
        report.validate(self.DOCS, notification({"viewers": "3"}))
        self.assertEqual(len(report.failures()), 1)
        self.assertTrue(report.failures()[0].startswith(self.DOCS))

    def test_known_error_matches_any_array_index(self):
        schemas = subscription_schemas(event={"items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}}}}})
        report = PayloadReport(schemas, {("docs", "channel.example", "1", "event.items[].id"): "docs say integer"})
        report.validate(self.DOCS, notification({"items": [{"id": "1"}, {"id": 2}, {"id": 3}]}))
        self.assertEqual(report.failures(), [])

    def test_known_error_that_no_longer_occurs_fails_the_run(self):
        # Regression: on 2026-11-25 Twitch fixed its docs and every KNOWN_PAYLOAD_ERRORS entry went stale.
        known = {("docs", "channel.example", "1", "event.viewers"): "docs send a string"}
        report = self.report(known)
        report.validate(self.DOCS, notification({"viewers": 3}))
        self.assertEqual(report.stale_known_errors(), list(known))
        self.assertEqual(report.failures(), [
            "KNOWN_PAYLOAD_ERRORS entry no longer occurs, remove it: "
            "('docs', 'channel.example', '1', 'event.viewers')",
        ])

    def test_failures_are_ordered_mismatches_unlinked_stale(self):
        known = {("docs", "channel.example", "1", "event.is_live"): "stale"}
        report = self.report(known)
        report.validate(self.DOCS, notification({"viewers": "3"}))
        report.validate(self.DOCS, notification({}, subscription_type="channel.unknown"))
        self.assertEqual([line.split(" ", 1)[0] for line in report.failures()],
                         [self.DOCS, "no", "no", "KNOWN_PAYLOAD_ERRORS"])


if __name__ == "__main__":
    unittest.main()
