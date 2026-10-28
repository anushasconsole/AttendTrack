"""
tests/test_cache.py — Unit tests for Redis cache helpers.
Redis calls are mocked — no real Redis connection needed.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import json
import pytest
from unittest.mock import AsyncMock, patch


class TestCacheGet:
    @pytest.mark.asyncio
    async def test_returns_none_when_redis_unavailable(self):
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = None
            from app.core.cache import cache_get
            result = await cache_get("some:key")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_get
            result = await cache_get("missing:key")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_parsed_json_on_hit(self):
        payload    = {"today_checkins": 42, "total_employees": 100}
        mock_redis = AsyncMock()
        mock_redis.get.return_value = json.dumps(payload)
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_get
            result = await cache_get("cache:hr:summary")
        assert result == payload
        assert result["today_checkins"] == 42

    @pytest.mark.asyncio
    async def test_returns_none_on_redis_exception(self):
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = Exception("connection reset")
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_get
            result = await cache_get("any:key")
        assert result is None


class TestCacheSet:
    @pytest.mark.asyncio
    async def test_calls_setex_with_correct_ttl(self):
        mock_redis = AsyncMock()
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_set
            await cache_set("cache:hr:summary", {"total": 5}, ttl=30)
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert args[0] == "cache:hr:summary"
        assert args[1] == 30
        assert json.loads(args[2])["total"] == 5

    @pytest.mark.asyncio
    async def test_no_op_when_redis_unavailable(self):
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = None
            from app.core.cache import cache_set
            # Should not raise
            await cache_set("key", {"x": 1}, ttl=60)

    @pytest.mark.asyncio
    async def test_silent_on_redis_exception(self):
        mock_redis = AsyncMock()
        mock_redis.setex.side_effect = Exception("write failed")
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_set
            # Should not raise
            await cache_set("key", {"x": 1}, ttl=60)


class TestCacheInvalidate:
    @pytest.mark.asyncio
    async def test_calls_delete_with_all_keys(self):
        mock_redis = AsyncMock()
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = mock_redis
            from app.core.cache import cache_invalidate
            await cache_invalidate("cache:hr:summary", "cache:mgr:stats:MGR001")
        mock_redis.delete.assert_called_once_with("cache:hr:summary", "cache:mgr:stats:MGR001")

    @pytest.mark.asyncio
    async def test_no_op_when_redis_unavailable(self):
        with patch("app.core.cache.get_redis", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = None
            from app.core.cache import cache_invalidate
            await cache_invalidate("key1", "key2")   # should not raise
