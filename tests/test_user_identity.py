"""用户身份校验测试（零依赖，覆盖 user_identity 与 memory 兜底）"""
import pytest

from src.user_identity import (
    DEFAULT_USER_ID,
    is_valid_user_id,
    resolve_user_id,
)
from src.memory import sanitize_user_id


class TestIsValid:
    @pytest.mark.parametrize("uid", [
        "u1", "123456", "wechat_abc-xyz", "A" * 64,
    ])
    def test_valid(self, uid):
        assert is_valid_user_id(uid)

    @pytest.mark.parametrize("uid", [
        "", "a" * 65, "..", "../..", "a/b", "a\\b", "a b",
        "../../etc/passwd", "中文用户", "a;DROP", None, 123,
    ])
    def test_invalid(self, uid):
        assert not is_valid_user_id(uid)


class TestResolve:
    def test_missing_falls_back_to_guest(self):
        assert resolve_user_id(None) == DEFAULT_USER_ID
        assert resolve_user_id("") == DEFAULT_USER_ID
        assert resolve_user_id("   ") == DEFAULT_USER_ID

    def test_valid_passthrough(self):
        assert resolve_user_id("wechat_abc") == "wechat_abc"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            resolve_user_id("../..")
        with pytest.raises(ValueError):
            resolve_user_id("a" * 65)


class TestSanitize:
    def test_valid_passthrough(self):
        assert sanitize_user_id("u_1") == "u_1"

    def test_invalid_downgrades(self):
        assert sanitize_user_id("../..") == "guest"
        assert sanitize_user_id("") == "guest"

    def test_custom_default(self):
        assert sanitize_user_id("a/b", default="anon") == "anon"