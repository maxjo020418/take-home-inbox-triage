"""Regenerate the schema appendix of ENGINEERING_LOG.md from the code itself.

    uv run python scripts/render_schema_notes.py

Pulls the Pydantic models, dataclasses, agent prompt and SQL schema straight
from source, so the documentation can't drift (tests/test_docs_sync.py fails if it does).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "ENGINEERING_LOG.md"
BEGIN = "<!-- BEGIN GENERATED: scripts/render_schema_notes.py (do not edit by hand; tests/test_docs_sync.py checks it) -->"
END = "<!-- END GENERATED -->"


def class_source(path: str, *names: str) -> str:
    src = (ROOT / path).read_text()
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in names:
            out.append(ast.get_source_segment(src, node))
    return "\n\n\n".join(out)


def function_source(path: str, name: str) -> str:
    src = (ROOT / path).read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise KeyError(name)


def render() -> str:
    from src.backend.agent import INSTRUCTIONS, TriageOutput, _render
    from src.triage_skill import ROUTING

    example = _render({"from": "dana@meridianparts.com", "subject": "Invoice #4471 charged twice",
                       "body": "We were billed twice..."},
                      "ok: SPF published, DMARC p=reject")
    schema = json.dumps(TriageOutput.model_json_schema()["properties"]["label"], indent=None)
    parts = [
        BEGIN,
        "",
        "### How the pieces fit",
        "`email (mock /inbox)` → `emails` row (queue) → worker: `check_sender()` + `TriageOutput` from the agent + "
        "`injection.detect()` → `plan_actions()` (code: kinds + destinations) → `actions` rows `pending` → "
        "human approves in the TUI → `execute()` with the write-scope client.",
        "",
        "### 1. Agent output — `src/backend/agent.py` (PydanticAI `output_type`)",
        "The label is a `Literal`, so anything outside the four labels fails validation and PydanticAI retries (`retries=2`). "
        f"Label JSON schema: `{schema}`",
        "```python",
        class_source("src/backend/agent.py", "TriageOutput"),
        "```",
        "",
        "### 2. Agent system prompt — `INSTRUCTIONS` (verbatim)",
        "```text",
        INSTRUCTIONS.rstrip(),
        "```",
        "",
        "### 3. Per-email user message — `_render()`",
        "The sender check is system-computed; the email is wrapped as untrusted JSON data.",
        "```python",
        function_source("src/backend/agent.py", "_render"),
        "```",
        "Example:",
        "```text",
        example,
        "```",
        "",
        "### 4. Routing & planned actions — `src/triage_skill.py` (dataclasses, pure)",
        f"`ROUTING = {ROUTING}`",
        "```python",
        class_source("src/triage_skill.py", "ProposedAction", "Drafts"),
        "```",
        "",
        "### 5. Sender-domain check — `src/backend/domain_check.py`",
        "```python",
        class_source("src/backend/domain_check.py", "DomainCheck"),
        "```",
        "",
        "### 6. Backend API models — `src/backend/api.py`",
        "```python",
        class_source("src/backend/api.py", "LoginIn", "LoginOut", "EditIn"),
        "```",
        "",
        "### 7. Mock email API models — `mock_api/server.py` (provided `Reply`/`Alert`/`Lead`; added `InjectedEmail`)",
        "```python",
        class_source("mock_api/server.py", "Reply", "Alert", "Lead", "InjectedEmail"),
        "```",
        "",
        "### 8. Database — `src/backend/schema.sql`",
        "```sql",
        (ROOT / "src/backend/schema.sql").read_text().rstrip(),
        "```",
        "",
        END,
    ]
    return "\n".join(parts)


def main() -> None:
    text = LOG.read_text()
    start, end = text.index(BEGIN), text.index(END) + len(END)
    LOG.write_text(text[:start] + render() + text[end:])
    print(f"updated {LOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
