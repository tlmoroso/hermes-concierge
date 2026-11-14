"""The workspace and admission paths agree on scoped keys and disabled plugins."""
from pm.workspace import enabled_member_dirs


def test_scoped_members_respect_disabled_and_keep_other_profiles(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    current = home / "plugins" / "group" / "plugin"
    other_home = home / "profiles" / "other"
    other = other_home / "plugins" / "same"
    for member in (current, other):
        member.mkdir(parents=True)
        (member / "pyproject.toml").write_text('[project]\nname="test"\nversion="1"\ndependencies=["requests>=2"]\n')
    (home / "config.yaml").write_text('plugins:\n  enabled: [group/plugin]\n  disabled: []\n')
    (other_home / "config.yaml").write_text('plugins:\n  enabled: [same]\n')
    assert set(enabled_member_dirs()) == {current, other}
    (home / "config.yaml").write_text('plugins:\n  enabled: [group/plugin]\n  disabled: [group/plugin]\n')
    assert enabled_member_dirs() == [other]


def test_manifest_only_member_is_named_after_its_plugin_dir_and_stays_unique(tmp_path):
    """uv's conflict text names the workspace member (``hermes-plugin-<key> depends on …``);
    a bare path hash left the user with nothing to disable. Two same-named plugins from
    different homes must still be distinct members."""
    import tomllib
    from pm.workspace import _workspace_member

    members = []
    for home in ("home-a", "home-b"):
        plugin = tmp_path / home / "plugins" / "My Plugin!"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: my-plugin\npip_dependencies: [left-pad-py]\n", encoding="utf-8")
        root = tmp_path / f"gen-{home}"
        root.mkdir()
        members.append(_workspace_member(plugin, root, identity=plugin, core_name="hermes-agent"))
    names = [tomllib.loads((m / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] for m in members]
    assert all(name.startswith("hermes-plugin-my-plugin-") for name in names), names
    assert names[0] != names[1]
    assert members[0].name != members[1].name


def test_pyproject_member_is_renamed_by_its_key_and_stays_unique(tmp_path):
    """A plugin that ships its own pyproject declares its real [project].name, and
    uv identifies workspace members by that name — enabling the plugin in two
    profiles then fails ``uv lock`` with "Two workspace members are both named
    …". Rename metadata-only members by their unique key, like manifest-only
    ones; a buildable member keeps the name its package metadata reports."""
    import tomllib
    from pm.workspace import _workspace_member

    members = []
    for home in ("home-a", "home-b"):
        plugin = tmp_path / home / "plugins" / "hindsight"
        plugin.mkdir(parents=True)
        (plugin / "pyproject.toml").write_text(
            '[project]\nname = "hermes-plugin-hindsight"\nversion = "1.0.0"\n'
            'dependencies = ["hindsight-client>=0.10.1"]\n',
            encoding="utf-8",
        )
        root = tmp_path / f"gen-{home}"
        root.mkdir()
        members.append(_workspace_member(plugin, root, identity=plugin, core_name="hermes-agent"))
    names = [tomllib.loads((m / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] for m in members]
    assert all(name.startswith("hermes-plugin-hindsight-") for name in names), names
    assert names[0] != names[1]
    document = tomllib.loads((members[0] / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["version"] == "1.0.0"
    assert document["project"]["dependencies"] == ["hindsight-client>=0.10.1"]


def test_buildable_pyproject_member_keeps_its_declared_name(tmp_path):
    """uv verifies a buildable member's [project].name against the package metadata
    its backend produces, so renaming it breaks the build ("Package metadata name
    … does not match given name"); only metadata-only members may be renamed."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "replay"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "replay-plugin"\nversion = "1.0"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    assert (member / "pyproject.toml").read_text(encoding="utf-8") == (
        plugin / "pyproject.toml").read_text(encoding="utf-8")


def test_virtual_member_without_project_metadata_gets_a_version(tmp_path):
    """A metadata-only member whose pyproject declares no [project] (lint config
    only) is emitted as ``[project] name = …``; uv rejects a [project] table
    without ``version`` (or ``dynamic``) at ``uv lock`` time, aborting the whole
    update. Generated virtual members carry the same inert version plugin-deps
    use."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "hermes-lcm"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py311"\n', encoding="utf-8")
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.0.0"


def test_virtual_member_declaring_dynamic_version_is_left_alone(tmp_path):
    """PEP 621 forbids supplying ``project.version`` while it is listed under
    ``project.dynamic`` ("You cannot provide a value for project.version and
    list it under project.dynamic at the same time"). A metadata-only member
    may legitimately declare ``dynamic = ["version"]``; the generated member
    must not gain a static version in that case, or ``uv lock`` rejects the
    conflicted manifest."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "dynver"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "dynver"\ndynamic = ["version"]\n', encoding="utf-8"
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]


def test_buildable_member_without_a_version_gets_one_and_keeps_its_name(tmp_path):
    """uv rejects a [project] table with no version at parse time, buildable or not.
    A buildable member still keeps its declared name (uv checks it against its backend)."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "noversion"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "noversion-plugin"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project == {"name": "noversion-plugin", "version": "0.0.0"}


def test_member_dev_groups_are_dropped_and_core_references_get_a_workspace_source(tmp_path):
    """Regression: a plugin's dev/test groups reference pinned Hermes by name (the
    documented way to run its own unit suite, e.g. ``hermes-agent @ git+…``). In the
    generated workspace that name IS a member — the core root — so uv refused to
    even parse the member manifest, and the group's pins conflicted with core's own
    dev groups (honcho's pytest-asyncio vs core's); either way every ``uv lock``
    (install, enable, update, the left-core migration) aborted. Groups can never be
    installed from the workspace, so the copy drops them; a runtime dependency or
    extra naming core still resolves, via a tool.uv.sources workspace entry uv
    refuses to parse without."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "honcho"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "hermes-plugin-honcho"\nversion = "1.0.0"\n'
        'dependencies = ["honcho-ai>=2.2.0,<3"]\n'
        '[dependency-groups]\ntest = [\n'
        '    "hermes-agent @ git+https://example.invalid/hermes-agent.git@1e4952ddba1bc585416ad43438d60183380035cd",\n'
        '    "pytest==9.1.1",\n'
        ']\n'
        '[project.optional-dependencies]\ntype = ["hermes-agent[dev]>=0.21"]\n'
        '[tool.uv]\npackage = false\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    document = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    assert "dependency-groups" not in document and "dev-dependencies" not in document
    assert document["project"]["dependencies"] == ["honcho-ai>=2.2.0,<3"]
    assert document["project"]["optional-dependencies"] == {"type": ["hermes-agent[dev]>=0.21"]}
    assert document["tool"]["uv"]["sources"] == {"hermes-agent": {"workspace": True}}


def test_member_referencing_core_keeps_an_existing_source_entry(tmp_path):
    """A member that already maps the core name (e.g. a vendored path source
    rewritten elsewhere) is not clobbered with a workspace entry, and requirements
    for other names gain nothing: an unused ``tool.uv.sources`` entry makes uv
    reject the workspace too."""
    import tomllib
    from pm.workspace import _workspace_member

    plugin = tmp_path / "home" / "plugins" / "vendored"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        '[project]\nname = "vendored-plugin"\nversion = "1.0.0"\n'
        'dependencies = ["hermes-agent", "left-pad-py>=1"]\n'
        '[dependency-groups]\ntest = ["pytest"]\n'
        '[tool.uv]\npackage = false\n'
        '[tool.uv.sources]\nhermes-agent = { path = "vendor/hermes-agent" }\n',
        encoding="utf-8",
    )
    root = tmp_path / "gen"
    root.mkdir()
    member = _workspace_member(plugin, root, identity=plugin, core_name="hermes-agent")
    document = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    # The vendored tree travels with the member copy, so the path stays relative and
    # the workspace entry is NOT injected over it.
    assert document["tool"]["uv"]["sources"] == {"hermes-agent": {"path": "vendor/hermes-agent"}}
    assert "dependency-groups" not in document

