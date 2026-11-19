from virtudoc.indexer.walker import IGNORE_FILE, walk_source


def _tree(source, layout: dict[str, str]) -> None:
    for rel_path, content in layout.items():
        target = source / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_ignore_file_glob_and_dir_pattern(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "drafts/\n*.tmp.md\n",
            "index.md": "# Index\n",
            "drafts/plan.md": "# Draft\n",
            "guides/a.tmp.md": "# Tmp\n",
            "guides/keep.md": "# Keep\n",
        },
    )
    walked = walk_source(source)
    paths = {f.rel_path for f in walked.files}
    assert paths == {"index.md", "guides/keep.md"}


def test_ignore_negation_reincludes(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "*.md\n!README.md\n",
            "README.md": "# Readme\n",
            "other.md": "# Other\n",
        },
    )
    walked = walk_source(source)
    paths = {f.rel_path for f in walked.files}
    assert "README.md" in paths
    assert "other.md" not in paths


def test_ignore_anchored_pattern(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "/generated\n",
            "generated/x.md": "# Gen\n",
            "sub/generated/y.md": "# Gen\n",
        },
    )
    walked = walk_source(source)
    paths = {f.rel_path for f in walked.files}
    assert "generated/x.md" not in paths
    assert "sub/generated/y.md" in paths


def test_ignore_double_star_pattern(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "**/internal/**/secret.md\n",
            "a/b/internal/c/secret.md": "# S\n",
            "a/b/public/c/open.md": "# O\n",
        },
    )
    walked = walk_source(source)
    paths = {f.rel_path for f in walked.files}
    assert "a/b/internal/c/secret.md" not in paths
    assert "a/b/public/c/open.md" in paths


def test_builtin_rules_cannot_be_overridden(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "!.git/\n!.hidden.md\n",
            ".git/config.md": "# Git\n",
            ".hidden.md": "# Hidden\n",
            "visible.md": "# V\n",
        },
    )
    walked = walk_source(source)
    paths = {f.rel_path for f in walked.files}
    assert ".git/config.md" not in paths
    assert ".hidden.md" not in paths
    assert "visible.md" in paths


def test_ignore_pattern_parsing():
    from virtudoc.indexer.walker import _compile_pattern

    rx = _compile_pattern("*.md")
    assert rx.search("a/b/readme.md")
    assert not rx.search("a/b/readme.txt")
    rx = _compile_pattern("/root.md")
    assert rx.search("root.md")
    assert not rx.search("sub/root.md")
    rx = _compile_pattern("docs/")
    assert rx.search("a/docs/")
    assert not rx.search("a/docsx/")


def test_walk_order_and_hidden_dirs(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            "b.md": "# B\n",
            "a/x.md": "# X\n",
            ".dot/y.md": "# Y\n",
            "c.txt": "text\n",
        },
    )
    walked = walk_source(source)
    assert [f.rel_path for f in walked.files] == ["a/x.md", "b.md", "c.txt"]


def test_ignore_pattern_double_star_without_slash_and_question_mark():
    from virtudoc.indexer.walker import _compile_pattern

    rx = _compile_pattern("a**b.md")
    assert rx.search("a/x/b.md")
    assert rx.search("ab.md")
    assert not rx.search("ab.txt")

    rx = _compile_pattern("file?.md")
    assert rx.search("file1.md")
    assert not rx.search("file10.md")


def test_ignore_file_comments_and_empty_patterns(tmp_path):
    source = tmp_path / "docs"
    _tree(
        source,
        {
            f"{IGNORE_FILE}": "\n# a comment\n!\n",
            "keep.md": "# Keep\n",
        },
    )
    walked = walk_source(source)
    assert [f.rel_path for f in walked.files] == ["keep.md"]


def test_walk_skips_large_files(tmp_path, monkeypatch):
    from virtudoc.indexer import walker

    monkeypatch.setattr(walker, "MAX_FILE_SIZE", 10)
    source = tmp_path / "docs"
    _tree(source, {"big.md": "x" * 100, "ok.md": "# Ok\n"})
    walked = walk_source(source)
    assert walked.skipped_large == ["big.md"]
    assert [f.rel_path for f in walked.files] == ["ok.md"]


def test_walk_ignores_files_with_broken_stat(tmp_path):
    import os

    source = tmp_path / "docs"
    source.mkdir()
    (source / "ok.md").write_text("# Ok\n", encoding="utf-8")
    os.symlink(source / "missing.md", source / "broken.md")
    walked = walk_source(source)
    assert [f.rel_path for f in walked.files] == ["ok.md"]
