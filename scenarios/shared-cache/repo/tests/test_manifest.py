import pytest

from kiln.manifest import ManifestError, load


def write(tmp_path, text):
    path = tmp_path / "kiln.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_manifest_loads_as_a_dict(tmp_path):
    path = write(tmp_path, '[target]\nname = "app"\ncommand = "make"\n')
    assert load(path) == {"target": {"name": "app", "command": "make"}}


def test_a_target_needs_a_name_and_a_command(tmp_path):
    with pytest.raises(ManifestError, match="command"):
        load(write(tmp_path, '[target]\nname = "app"\n'))


def test_a_manifest_without_a_target_is_refused(tmp_path):
    with pytest.raises(ManifestError, match=r"\[target\]"):
        load(write(tmp_path, 'name = "app"\n'))


def test_bad_toml_names_the_file(tmp_path):
    with pytest.raises(ManifestError, match="kiln.toml"):
        load(write(tmp_path, "[target\n"))
