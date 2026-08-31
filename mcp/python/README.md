# incant-mcp

MCP server for [Incant](https://github.com/cgkanchi/incant/blob/main/README.md) — lets AI agents author, test, review,
publish, and target prompts, doing everything the console can (minus deployment
administration) within the configured API key's roles.

## Setup

```json
{
  "mcpServers": {
    "incant": {
      "command": "uvx",
      "args": ["incant-mcp"],
      "env": {
        "INCANT_URL": "https://prompts.internal",
        "INCANT_API_KEY": "incant_sk_...",
        "INCANT_ENVIRONMENT": "staging"
      }
    }
  }
}
```

`INCANT_ENVIRONMENT` is optional (tools default to the deployment's default
environment and accept a per-call override). Add `--read-only` to the args to
register only the read/test tools.

## Permissions

The server adds nothing and hides nothing: every call runs under the API key's
roles, enforced by the deployment. A viewer key can explore and render-test but
not publish; a 403 surfaces the server's role explanation verbatim. Protected
environments additionally demand a `confirm` echo on mutations — the paired
skills instruct agents to relay that confirmation to a human first.

## Tools

- **Discover**: `list_prompts`, `get_prompt`, `list_rules`, `list_environments`,
  `get_publish_history`, `get_targeting_history`, `get_audit`
- **Test**: `render_prompt` (real serving path, with `pin` replay),
  `evaluate_targeting`, `diff_versions`
- **Author**: `create_prompt`, `edit_draft` (create/update/get/render/diff/discard),
  `commit_draft`, `review_draft`, `set_prompt_metadata`
- **Release & target**: `publish_prompt` (pointer + optional default promotion +
  rule archive, atomic), `rollback_pointer`, `upsert_rule`, `set_rule_status`,
  `upsert_segment`, `set_default`, `kill_switch`, `rollback_targeting`

## Skills

The [`skills/`](https://github.com/cgkanchi/incant/tree/main/skills) directory pairs this server with agent skills —
`incant-authoring` (draft → test → review → commit) and `incant-release`
(publish → target → roll out → roll back) — that encode the workflows and
guardrails. Install by copying into your agent's skills directory (e.g.
`.claude/skills/`).
