from langgraph.checkpoint.redis import RedisSaver
from metro_agent.config import SHORT_TERM_MEMORY_REDIS_URL,SHORT_TERM_MEMORY_TTL_MINUTES
def build_redis_checkpointer():
    redis_url = SHORT_TERM_MEMORY_REDIS_URL
    ttl_minutes = SHORT_TERM_MEMORY_TTL_MINUTES
    checkpointer = RedisSaver(
        redis_url=redis_url, 
        ttl={
            "default_ttl": ttl_minutes,
            "refresh_on_read": True,
        })
    checkpointer.setup()
    return checkpointer