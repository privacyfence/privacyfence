"""web/personal_settings_shim.py: the script text only. Whether it runs is proved by
tests/integration/test_settings_personal_transfer_browser.py in a real browser."""
from __future__ import annotations

import re

from privacyfence.web.personal_settings_shim import personal_transfer_shim


class TestPersonalTransferShim:
    def test_is_one_nonced_script_with_the_three_urls(self):
        shim = personal_transfer_shim(csrf="tok", nonce="n0nce")
        assert shim.startswith('<script nonce="n0nce">')
        assert shim.endswith("</script>")
        assert shim.count("<script") == 1
        for url in (
            "/api/settings/personal/export", "/api/settings/personal/preview", "/api/settings/personal/import",
        ):
            assert url in shim

    def test_builds_the_dom_with_text_only(self):
        shim = personal_transfer_shim(csrf="tok", nonce="n")
        assert "innerHTML" not in shim
        assert "insertAdjacentHTML" not in shim
        assert "document.write" not in shim
        assert "textContent" in shim

    def test_dialog_is_accessible(self):
        shim = personal_transfer_shim(csrf="tok", nonce="n")
        assert "'role', 'dialog'" in shim
        assert "'aria-modal', 'true'" in shim
        assert "'aria-label', 'Import your settings'" in shim
        assert "'Escape'" in shim

    def test_hostile_csrf_cannot_end_the_script(self):
        shim = personal_transfer_shim(csrf="</script><b>", nonce="n")
        assert "</script><b>" not in shim
        assert shim.count("</script>") == 1
        assert '"\\u003c/script\\u003e\\u003cb\\u003e"' in shim

    def test_csrf_is_a_json_string_literal(self):
        shim = personal_transfer_shim(csrf='a"b\\c', nonce="n")
        assert re.search(r'var CSRF = "a\\"b\\\\c";', shim)

    def test_nonce_is_attribute_escaped(self):
        shim = personal_transfer_shim(csrf="t", nonce='x"><script>')
        assert shim.startswith('<script nonce="x&quot;&gt;&lt;script&gt;">')

    def test_import_reposts_the_file_object_with_hash_and_confirmation(self):
        shim = personal_transfer_shim(csrf="tok", nonce="n")
        assert "fd.append('file', file)" in shim
        assert "sha256: sha256, confirmed: 'true'" in shim
        assert "webauthn_assertion" in shim
        assert "pfWebauthnGet" in shim
        assert "enroll_url" in shim
