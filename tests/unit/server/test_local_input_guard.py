from pathlib import Path

from openviking.server.local_input_guard import resolve_uploaded_temp_file_id


def test_resolve_uploaded_temp_file_id_supports_wrapped_upload_directory(tmp_path: Path):
    upload_root = tmp_path / "upload"
    upload_root.mkdir()

    entry_dir = upload_root / "upload_123"
    entry_dir.mkdir()
    wrapped_file = entry_dir / "guide.md"
    wrapped_file.write_text("content", encoding="utf-8")

    resolved = resolve_uploaded_temp_file_id("upload_123", upload_root)
    assert resolved == str(wrapped_file.resolve())


def test_resolve_uploaded_temp_file_id_supports_legacy_plain_file(tmp_path: Path):
    upload_root = tmp_path / "upload"
    upload_root.mkdir()

    legacy_file = upload_root / "upload_legacy.md"
    legacy_file.write_text("legacy", encoding="utf-8")

    resolved = resolve_uploaded_temp_file_id("upload_legacy.md", upload_root)
    assert resolved == str(legacy_file.resolve())
