import pytest
from xdep.repositories import Repository, RepositoryStore


def test_roundtrip_selected_repository_and_missing_directory(tmp_path):
    store = RepositoryStore(tmp_path / "repositories.json")
    repos = [Repository("Site", str(tmp_path / "site")), Repository("API", str(tmp_path / "api"))]
    assert store.load() == ([], "")
    store.save(repos, "API")
    assert store.load() == (repos, "API")


def test_rejects_duplicates_without_overwriting_saved_data(tmp_path):
    store = RepositoryStore(tmp_path / "repositories.json")
    first = Repository("Site", str(tmp_path / "site"))
    store.save([first], first.name)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="unique"):
        store.save([first, Repository("SITE", str(tmp_path / "other"))], "Site")
    with pytest.raises(ValueError, match="already saved"):
        store.save([first, Repository("Other", first.directory)], "Site")
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("repo", [Repository("", "/absolute"), Repository("Name", "relative")])
def test_invalid_repository_not_saved(tmp_path, repo):
    store = RepositoryStore(tmp_path / "repositories.json")
    with pytest.raises(ValueError):
        store.save([repo], repo.name)
    assert not store.path.exists()


def test_corrupt_file_is_left_untouched(tmp_path):
    store = RepositoryStore(tmp_path / "repositories.json")
    store.path.write_text("not valid json")
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_text() == "not valid json"


def test_migration_preserves_legacy_and_existing_project_data(tmp_path):
    legacy = RepositoryStore(tmp_path / "old" / "repositories.json")
    repo = Repository("Old site", str(tmp_path / "site"))
    legacy.save([repo], repo.name)
    before = legacy.path.read_bytes()
    project = RepositoryStore(tmp_path / "project" / "repositories.json")
    project.migrate_from(legacy.path)
    assert project.load() == ([repo], repo.name)
    assert legacy.path.read_bytes() == before
    other = Repository("Project site", str(tmp_path / "other"))
    project.save([other], other.name)
    project.migrate_from(legacy.path)
    assert project.load() == ([other], other.name)


def test_corrupt_legacy_data_is_not_migrated(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text("broken")
    project = RepositoryStore(tmp_path / "project.json")
    with pytest.raises(ValueError):
        project.migrate_from(legacy)
    assert not project.path.exists()
    assert legacy.read_text() == "broken"
