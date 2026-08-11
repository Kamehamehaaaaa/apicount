import redis
import os
from dotenv import load_dotenv

load_dotenv()

client = redis.Redis(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True,
    socket_timeout=5
)

if client.ping():
    print("Connected to Redis database")