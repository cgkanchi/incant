# incant-sdk

Python client for [Incant](https://github.com/cgkanchi/incant/blob/main/README.md) — render prompts through targeting,
discover what to pass, and reproduce any render exactly.

```bash
pip install incant-sdk
```

## Render

```python
from incant_sdk import Incant

client = Incant()   # reads INCANT_URL, INCANT_API_KEY, INCANT_ENVIRONMENT
# or: Incant("https://prompts.internal", key="incant_sk_...", environment="prod")

r = client.render(
    "support/system",
    flags={"user_id": "u_42", "tier": "pro", "region": "us"},   # who's asking → targeting
    variables={"customer_name": "Acme", "history": []},         # template inputs
)
r.text            # the rendered prompt (str(r) works too)
r.version, r.sha  # exactly what was served
r.matched_rule    # "default", RuleMatch(scope="prompt", id="beta-gets-v3"), or
                  #   RuleMatch(scope="pin", id=None) on a pinned replay
```

## Reproduce exactly

Every result carries a `pin` — log it beside your LLM call, feed it back for a
byte-identical replay (same commits for the prompt and every included fragment,
same historical targeting):

```python
trace.log(prompt_pin=r.pin)
r2 = client.render("support/system", flags=f, variables=v, pin=r.pin)
```

## Discover

```python
for p in client.prompts():                 # everything this key can render
    print(p.id, p.description, p.default_version)

spec = client.prompt("support/system")     # what to pass
spec.variables   # [Var(name="customer_name", type="string", required=True, ...), ...]
spec.flags       # [Flag(name="tier", values=("enterprise", "pro")), ...]
spec.includes    # fragments this prompt pulls in
```

## Debug targeting

```python
client.evaluate("support/system", flags={"user_id": "u_42"})   # version, no render
client.evaluate_all(flags={"user_id": "u_42"})                 # every prompt at once
```

## Async

```python
from incant_sdk import AsyncIncant

async with AsyncIncant() as client:
    r = await client.render("support/system", flags=..., variables=...)
```

Same surface, same wire behavior — both clients share one request/parse core.

## Errors & resilience

Typed exceptions carry the server's message: `PromptNotFound` (distinguishes
unknown vs. not-yet-targeted-here), `MissingVariable` (`.variable` names it),
`NotAuthorized`, `RenderError`, and `IncantUnavailable`. Renders are pure reads,
so connection failures and 502/503/504 retry automatically (`retries=2` with
backoff, configurable) before raising `IncantUnavailable`.
