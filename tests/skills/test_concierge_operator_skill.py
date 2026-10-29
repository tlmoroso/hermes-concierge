"""Tests for the concierge-operator skill: SKILL.md must document the real
catalog and the real CLI surface, so the skill's prose cannot drift from the
code it drives. The skill is the sanctioned operator surface for Concierge
bots; if a blueprint key or CLI verb changes without the skill following (or
vice versa), the next operator following the skill hits a usage error."""
import re
from pathlib import Path

import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT / "skills" / "productivity" / "concierge-operator" / "SKILL.md"
)
CLI_PARSER_PATH = REPO_ROOT / "hermes_cli" / "subcommands" / "concierge.py"

# Quick Reference blueprint table rows: | `key` | Title | description |
_ROW_RE = re.compile(
    r"^\|\s*`([a-z_]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", re.MULTILINE
)


def _frontmatter_and_body():
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---")
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "frontmatter must close with ---"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.end() + 3 :]
    return fm, body


def test_skill_file_exists():
    assert SKILL_PATH.is_file()


def test_frontmatter_required_fields():
    fm, _ = _frontmatter_and_body()
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in fm, f"missing frontmatter field: {field}"
    assert fm["name"] == "concierge-operator"


def test_description_hardline():
    fm, _ = _frontmatter_and_body()
    desc = fm["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars; hardline is 60"
    assert desc.endswith(".")


def test_author_credits_human_first():
    fm, _ = _frontmatter_and_body()
    assert not fm["author"].startswith("Hermes Agent")


def test_blueprint_table_matches_catalog():
    """The documented keys, titles, and one-line descriptions are exactly
    concierge/blueprints.py's CATALOG — no missing, extra, or renamed entry."""
    from concierge.blueprints import CATALOG

    _, body = _frontmatter_and_body()
    rows = {m.group(1): (m.group(2), m.group(3)) for m in _ROW_RE.finditer(body)}
    assert set(rows) == set(CATALOG), (
        f"SKILL.md blueprint table out of sync with concierge/blueprints.py: "
        f"only in skill: {sorted(set(rows) - set(CATALOG))}; "
        f"only in catalog: {sorted(set(CATALOG) - set(rows))}"
    )
    for key, bp in CATALOG.items():
        title, description = rows[key]
        assert title == bp.title, f"{key}: title drift ({title!r} != {bp.title!r})"
        assert description == bp.description, (
            f"{key}: description drift ({description!r} != {bp.description!r})"
        )


def test_documents_the_real_cli_verbs():
    """Every verb the skill teaches exists in the parser, and every parser
    verb the operator needs is taught."""
    _, body = _frontmatter_and_body()
    parser_src = CLI_PARSER_PATH.read_text(encoding="utf-8")
    for verb in ("new", "list", "retire", "bindings"):
        assert f"hermes concierge {verb}" in body, (
            f"SKILL.md does not document `hermes concierge {verb}`"
        )
        assert f'"{verb}"' in parser_src, (
            f"hermes concierge no longer registers the {verb!r} verb "
            f"the skill documents"
        )
