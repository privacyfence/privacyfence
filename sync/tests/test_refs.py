from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from privacyfence_sync.refs import RefError, expand_run_arg, lookup, substitute_value_refs

DOC = {"a": {"b": [10, {"c": "deep"}]}, "n": 3, "flag": True, "items": [1, 2], "map": {"k": 1}}


def read_json(ref: str) -> Any:
    assert ref == "pick.stdout"
    return DOC


def test_lookup_dict_keys_and_list_indexes() -> None:
    assert lookup(DOC, "n") == 3
    assert lookup(DOC, "a.b.0") == 10
    assert lookup(DOC, "a.b.1.c") == "deep"
    assert lookup({"x": {"0": "key"}}, "x.0") == "key"


@pytest.mark.parametrize("path", ["missing", "a.b.5", "a.b.x", "n.x", "a.b.1.c.d"])
def test_lookup_missing_raises(path: str) -> None:
    with pytest.raises(RefError, match=f"{path} not found"):
        lookup(DOC, path)


def test_whole_string_reference_keeps_json_type() -> None:
    assert substitute_value_refs("${pick.stdout:n}", read_json) == 3
    assert substitute_value_refs("${pick.stdout:items}", read_json) == [1, 2]
    assert substitute_value_refs("${pick.stdout:flag}", read_json) is True


def test_reference_inside_text_is_inserted_as_text() -> None:
    out = substitute_value_refs("n=${pick.stdout:n} l=${pick.stdout:items} m=${pick.stdout:map}", read_json)
    assert out == 'n=3 l=[1, 2] m={"k": 1}'


def test_walks_nested_lists_and_dicts() -> None:
    value = {"q": ["${pick.stdout:a.b.0}", {"x": "id-${pick.stdout:n}"}], "keep": 5, "none": None}
    assert substitute_value_refs(value, read_json) == {
        "q": [10, {"x": "id-3"}],
        "keep": 5,
        "none": None,
    }


def test_substitute_missing_path_raises() -> None:
    with pytest.raises(RefError):
        substitute_value_refs("${pick.stdout:nope}", read_json)


def test_expand_run_arg_each_placeholder(tmp_path: Path) -> None:
    kwargs: dict[str, Any] = {
        "files": {"report": tmp_path / "work" / "opps.csv"},
        "outputs": {"upsert": tmp_path / "work" / "plan" / "upsert.csv"},
        "work_dir": tmp_path / "work",
        "recipe_dir": tmp_path,
        "read_json": read_json,
    }
    assert expand_run_arg("{file:report}", **kwargs) == str(tmp_path / "work" / "opps.csv")
    assert expand_run_arg("--o={output:upsert}", **kwargs) == f"--o={tmp_path / 'work' / 'plan' / 'upsert.csv'}"
    assert expand_run_arg("{work_dir}/plan", **kwargs) == f"{tmp_path / 'work'}/plan"
    assert expand_run_arg("{recipe_dir}", **kwargs) == str(tmp_path)
    assert expand_run_arg('id=${pick.stdout:a.b.1.c} {"json": 1}', **kwargs) == 'id=deep {"json": 1}'


def test_expand_run_arg_makes_relative_paths_absolute() -> None:
    out = expand_run_arg(
        "{work_dir}",
        files={},
        outputs={},
        work_dir=Path("work"),
        recipe_dir=Path("."),
        read_json=read_json,
    )
    assert Path(out).is_absolute()


@pytest.mark.parametrize("arg", ["{file:nope}", "{output:nope}"])
def test_expand_run_arg_unknown_name_raises(arg: str, tmp_path: Path) -> None:
    with pytest.raises(RefError):
        expand_run_arg(arg, files={}, outputs={}, work_dir=tmp_path, recipe_dir=tmp_path, read_json=read_json)
