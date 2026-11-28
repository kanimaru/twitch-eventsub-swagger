import unittest

from validate import Validator, type_name


class TypeNameTest(unittest.TestCase):
    def test_bool_is_not_reported_as_integer(self):
        self.assertEqual(type_name(True), "boolean")

    def test_names_every_json_type(self):
        cases = {1: "integer", 1.5: "number", "a": "string", None: "null", (): "null"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(type_name(value), expected)
        self.assertEqual(type_name([]), "array")
        self.assertEqual(type_name({}), "object")


class ValidatorTest(unittest.TestCase):
    SCHEMAS = {
        "Event": {"type": "object", "properties": {
            "id": {"type": "string"},
            "count": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "user": {"$ref": "#/components/schemas/User"},
            "maybe_user": {"allOf": [{"$ref": "#/components/schemas/User"}], "nullable": True},
            "free_form": {"type": "object"},
        }},
        "User": {"type": "object", "properties": {"user_id": {"type": "string"}}},
    }

    def check(self, value):
        validator = Validator(self.SCHEMAS)
        validator.check(value, {"$ref": "#/components/schemas/Event"}, "event")
        return validator.errors

    def test_valid_payload_has_no_errors(self):
        self.assertEqual(self.check({
            "id": "1", "count": 2, "tags": ["a"], "user": {"user_id": "3"}, "maybe_user": {"user_id": "4"},
            "free_form": {"anything": [1, 2]},
        }), [])

    def test_unknown_field_fails(self):
        self.assertEqual(self.check({"extra": 1}), [("event.extra", "is not in the schema")])

    def test_wrong_primitive_fails(self):
        self.assertEqual(self.check({"count": "2"}), [("event.count", "is string, schema says integer")])

    def test_array_documented_as_object_fails_both_ways(self):
        self.assertEqual(self.check({"user": [{"user_id": "1"}]}),
                         [("event.user", "is array, schema says object")])
        self.assertEqual(self.check({"tags": "a"}), [("event.tags", "is string, schema says array")])

    def test_array_items_are_checked_with_their_index(self):
        self.assertEqual(self.check({"tags": ["a", 1]}), [("event.tags[1]", "is integer, schema says string")])

    def test_refs_are_followed_into_nested_objects(self):
        self.assertEqual(self.check({"user": {"user_id": 5}}),
                         [("event.user.user_id", "is integer, schema says string")])

    def test_nullable_allof_wrapper_is_followed(self):
        self.assertEqual(self.check({"maybe_user": {"login": "x"}}),
                         [("event.maybe_user.login", "is not in the schema")])

    def test_null_never_fails(self):
        self.assertEqual(self.check({"id": None, "user": None, "tags": None}), [])

    def test_resolve_keeps_the_outer_nullable_flag(self):
        schema, nullable = Validator(self.SCHEMAS).resolve(self.SCHEMAS["Event"]["properties"]["maybe_user"])
        self.assertIs(schema, self.SCHEMAS["User"])
        self.assertTrue(nullable)


if __name__ == "__main__":
    unittest.main()
