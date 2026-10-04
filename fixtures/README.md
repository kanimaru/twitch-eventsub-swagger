# Fixtures

EventSub notifications captured from Twitch, one JSON file per notification:

```json
{
  "subscription": { "type": "channel.hype_train.begin", "version": "2", "condition": { ... } },
  "event": { ... }
}
```

`validate.py` checks every file here against the generated schema, next to the example notifications of the docs
and the mock notifications of the Twitch CLI. Add a payload here when the docs and the CLI disagree, or when a
correction in `SCHEMA_CORRECTIONS` of `generator.py` needs a real payload behind it. Replace user names, IDs and
message texts of real people before committing.
