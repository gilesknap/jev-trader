"""The schema examples in the charter and the docs must stay valid against the code (#77 audit):
a reader copying one should get a file `trader validate` accepts, and the field reference must
list exactly the fields the schema has."""

import re
from pathlib import Path

import pytest
import yaml

from trader import config
from trader import features as F
from trader.classifier import ClassifierSpec, EntryOrder, Question, ScaleOut, load_specs

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DOC = ROOT / "docs" / "reference" / "classifier-schema.md"
# Placeholder custom feature names the examples use to show where one goes.
EXAMPLE_CUSTOM_FEATURES = {"my_custom_feature"}


def yaml_blocks(path: Path) -> list[str]:
    return re.findall(r"^```yaml\n(.*?)^```", path.read_text(), flags=re.S | re.M)


def classifier_examples():
    for path in (ROOT / "CLAUDE.md", SCHEMA_DOC):
        blocks = [b for b in yaml_blocks(path) if "classifiers:" in b]
        assert blocks, f"no classifier example found in {path.name}"
        for i, block in enumerate(blocks):
            yield pytest.param(block, id=f"{path.name}#{i}")


@pytest.mark.parametrize("block", list(classifier_examples()))
def test_classifier_examples_validate(block, tmp_path):
    f = tmp_path / "classifiers.yaml"
    f.write_text(block)
    specs = load_specs(f, set(F.REGISTRY) | EXAMPLE_CUSTOM_FEATURES, set(config.universe()))
    assert specs
    for s in specs:
        assert s.family_problem() is None  # `trader validate` also requires an honest label


def test_mode_example_is_a_valid_mode():
    blocks = [b for b in yaml_blocks(ROOT / "docs" / "reference" / "configuration.md") if b.startswith("mode:")]
    assert blocks
    for b in blocks:
        assert yaml.safe_load(b)["mode"] in ("auto", "paper", "live")


def _documented_fields(table_heading: str) -> set[str]:
    """Field names in the first column of the table under `table_heading`."""
    text = SCHEMA_DOC.read_text().split(table_heading, 1)[1]
    names, in_table = set(), False
    for line in text.splitlines()[1:]:
        if line.startswith("|"):
            in_table = True
            m = re.match(r"\| `([a-z_]+)` \|", line)
            if m:
                names.add(m.group(1))
        elif in_table:
            break  # the table ended
    return names


def test_schema_reference_lists_every_field():
    top = _documented_fields("## Fields") | _documented_fields("## Execution toolkit")
    assert top == set(ClassifierSpec.model_fields)
    question = _documented_fields("A **question** (`entry`, `exit`) has:")
    assert question == set(Question.model_fields)
    # The nested toolkit fields are described inline in their row.
    toolkit = SCHEMA_DOC.read_text().split("## Execution toolkit", 1)[1]
    for model in (EntryOrder, ScaleOut):
        for name in model.model_fields:
            assert f"`{name}`" in toolkit, name
