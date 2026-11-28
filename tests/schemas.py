"""Small in-memory schema sets for the tests, shaped like the components the generator writes."""


def subscription_schemas(subscription_type="channel.example", version="1", event=None, condition=None):
    """Event and condition components of one subscription, linked by x-subscription-type and -version."""
    link = {"x-subscription-type": subscription_type, "x-subscription-version": version}
    return {
        "ExampleEvent": {**link, "type": "object", "properties": event or {
            "broadcaster_user_id": {"type": "string"},
            "viewers": {"type": "integer"},
            "is_live": {"type": "boolean"},
        }},
        "ExampleCondition": {**link, "type": "object", "properties": condition or {
            "broadcaster_user_id": {"type": "string"},
        }},
    }


def notification(event, subscription_type="channel.example", version="1", condition=None):
    return {
        "subscription": {
            "type": subscription_type,
            "version": version,
            "condition": condition if condition is not None else {"broadcaster_user_id": "1"},
        },
        "event": event,
    }
