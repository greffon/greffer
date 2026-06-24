"""Guard the restic sidecar pin against a regression below 0.17.

restore_instance runs ``restic restore ... --delete`` (clean overwrite). That
flag only landed in restic 0.17, so the prior 0.16.4 pin made RESTORE FAIL with
"unknown flag: --delete". The @backup_real e2e that would have caught it is
skipped in CI (needs a real repo), so this cheap unit guard stands in.
"""
import re

from app.settings import Settings


def test_restic_sidecar_image_supports_restore_delete():
    img = Settings.model_fields["restic_sidecar_image"].default
    m = re.search(r"restic/restic:(\d+)\.(\d+)", img)
    assert m, f"cannot parse a restic version from {img!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (0, 17), (
        f"restic {major}.{minor} lacks `restore --delete` -- restore_instance "
        "would fail; pin >= 0.17")
