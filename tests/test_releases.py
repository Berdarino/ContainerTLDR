from datetime import UTC, datetime

from containertldr.releases import Release, select_releases


def rel(tag, published, prerelease=False):
    return Release(tag, datetime.fromisoformat(published).replace(tzinfo=UTC), "", "", prerelease)


def tags(releases):
    return [r.tag for r in releases]


def select(releases, current, new, since="2026-01-01", until="2026-12-31", tag_prefix=""):
    since, until = (datetime.fromisoformat(d).replace(tzinfo=UTC) for d in (since, until))
    return tags(select_releases(releases, current, new, since, until, tag_prefix))


# Newest first, the way GitHub lists them
BAZARR = [
    rel("v1.6.3-beta.0", "2026-10-12", prerelease=True),
    rel("v1.6.2", "2026-10-10"),
    rel("v1.6.2-beta.3", "2026-10-05", prerelease=True),
    rel("v1.6.2-beta.1", "2026-09-28", prerelease=True),
    rel("v1.6.1", "2026-09-20"),
    rel("v1.6.0", "2026-09-01"),
]


def test_stable_image_skips_prereleases():
    assert select(BAZARR, "v1.6.1-ls364", "v1.6.2-ls365") == ["v1.6.2"]


def test_beta_image_gets_betas_in_between():
    assert select(BAZARR, "v1.6.2-beta.1-ls10", "v1.6.2-beta.3-ls11") == ["v1.6.2-beta.3"]
    assert select(BAZARR, "v1.6.2-beta.1", "v1.6.3-beta.0") == ["v1.6.2-beta.3", "v1.6.2", "v1.6.3-beta.0"]


def test_develop_build_counts_github_prerelease_flag():
    radarr = [
        rel("v6.4.5.10720", "2026-10-14", prerelease=True),
        rel("v6.4.5.10710", "2026-10-07", prerelease=True),
        rel("v6.4.4.10685", "2026-10-01"),
    ]
    assert select(radarr, "6.4.4.10685-ls318", "6.4.5.10720-ls320") == ["v6.4.5.10710", "v6.4.5.10720"]
    assert select(radarr, "6.4.3.10600-ls300", "6.4.4.10685-ls318") == ["v6.4.4.10685"]


def test_same_version_selects_nothing():
    assert select(BAZARR, "v1.6.1-ls364", "v1.6.1-ls365") == []


def test_parallel_branches_bounded_by_new_version():
    redis = [
        rel("8.10.2", "2026-10-08"),
        rel("8.8.3", "2026-10-08"),
        rel("7.4.9", "2026-10-07"),
        rel("8.10.1", "2026-09-15"),
        rel("8.8.2", "2026-09-15"),
    ]
    assert select(redis, "8.8.2", "8.8.3") == ["8.8.3"]


def test_monorepo_tag_prefix():
    papra = [
        rel("@papra/app@26.7.0", "2026-10-02"),
        rel("@papra/lecture@0.4.1", "2026-10-01"),
        rel("@papra/app@26.6.3", "2026-09-20"),
        rel("@papra/app@26.6.2", "2026-09-10"),
    ]
    assert select(papra, "26.6.2", "26.7.0", tag_prefix="@papra/app@") == ["@papra/app@26.6.3", "@papra/app@26.7.0"]


def test_unknown_versions_fall_back_to_build_dates():
    frigate = [
        rel("v0.17.1", "2026-10-20"),
        rel("v0.17.0", "2026-10-03"),
        rel("v0.17.0-rc2", "2026-09-25", prerelease=True),
        rel("v0.16.4", "2026-09-17"),
        rel("v0.16.3", "2026-08-30"),
    ]
    assert select(frigate, None, None, since="2026-09-17", until="2026-10-17") == ["v0.17.0"]
    assert select(frigate, "main", "main", since="2026-08-01", until="2026-09-18") == ["v0.16.3", "v0.16.4"]
